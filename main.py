import discord 
from discord.ext import commands
import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import hashlib
import time

# ===============================================
# IMPORT KEEP-ALIVE
# ===============================================
try:
    from keep_alive import keep_alive
    KEEP_ALIVE_AVAILABLE = True
except ImportError:
    KEEP_ALIVE_AVAILABLE = False
    print("⚠️ keep_alive.py not found")

# ===============================================
# PRODUCTION CONFIG
# ===============================================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    help_command=None,
    max_messages=1000,
    chunk_guilds_at_startup=False
)

# ===============================================
# RATE LIMITING & QUEUE CONFIG
# ===============================================
class RateLimiter:
    def __init__(self, max_requests=50, window=60):
        self.max_requests = max_requests
        self.window = window
        self.requests = defaultdict(deque)
    
    def can_request(self, key):
        now = time.time()
        queue = self.requests[key]
        
        while queue and queue[0] < now - self.window:
            queue.popleft()
        
        if len(queue) >= self.max_requests:
            return False
        
        queue.append(now)
        return True
    
    def get_wait_time(self, key):
        now = time.time()
        queue = self.requests[key]
        
        if not queue or len(queue) < self.max_requests:
            return 0
        
        oldest = queue[0]
        return max(0, self.window - (now - oldest))

user_limiter = RateLimiter(max_requests=10, window=60)
guild_limiter = RateLimiter(max_requests=50, window=60)
global_limiter = RateLimiter(max_requests=200, window=60)

# ===============================================
# TRANSLATION CACHE
# ===============================================
class TranslationCache:
    def __init__(self, max_size=1000, ttl=3600):
        self.cache = {}
        self.access_times = {}
        self.max_size = max_size
        self.ttl = ttl
    
    def _make_key(self, text, target_lang):
        content = f"{text}:{target_lang}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def get(self, text, target_lang):
        key = self._make_key(text, target_lang)
        
        if key not in self.cache:
            return None
        
        if time.time() - self.access_times[key] > self.ttl:
            del self.cache[key]
            del self.access_times[key]
            return None
        
        self.access_times[key] = time.time()
        return self.cache[key]
    
    def set(self, text, target_lang, result):
        if len(self.cache) >= self.max_size:
            oldest_key = min(self.access_times, key=self.access_times.get)
            del self.cache[oldest_key]
            del self.access_times[oldest_key]
        
        key = self._make_key(text, target_lang)
        self.cache[key] = result
        self.access_times[key] = time.time()

cache = TranslationCache()

# ===============================================
# REQUEST QUEUE
# ===============================================
class TranslationQueue:
    def __init__(self, max_concurrent=5):
        self.queue = asyncio.Queue()
        self.processing = 0
        self.max_concurrent = max_concurrent
        self.workers_started = False
    
    async def add(self, task):
        await self.queue.put(task)
    
    async def worker(self):
        while True:
            try:
                task = await self.queue.get()
                self.processing += 1
                
                try:
                    await task()
                except Exception as e:
                    print(f"Queue worker error: {e}")
                finally:
                    self.processing -= 1
                    self.queue.task_done()
                    
            except Exception as e:
                print(f"Queue worker fatal error: {e}")
                await asyncio.sleep(1)
    
    async def start_workers(self):
        if not self.workers_started:
            self.workers_started = True
            for _ in range(self.max_concurrent):
                asyncio.create_task(self.worker())

translation_queue = TranslationQueue(max_concurrent=5)

# ===============================================
# DEBOUNCING
# ===============================================
class Debouncer:
    def __init__(self, delay=2.0):
        self.delay = delay
        self.pending = {}
    
    async def debounce(self, key, coro):
        if key in self.pending:
            self.pending[key].cancel()
        
        async def delayed():
            await asyncio.sleep(self.delay)
            await coro
            if key in self.pending:
                del self.pending[key]
        
        self.pending[key] = asyncio.create_task(delayed())

debouncer = Debouncer(delay=1.5)

# ===============================================
# CIRCUIT BREAKER (SIMPLIFIED)
# ===============================================
class CircuitBreaker:
    def __init__(self, failure_threshold=3, timeout=30):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = defaultdict(int)
        self.last_failure = defaultdict(float)
        self.open_circuits = set()
    
    def record_failure(self, api_url):
        self.failures[api_url] += 1
        self.last_failure[api_url] = time.time()
        
        if self.failures[api_url] >= self.failure_threshold:
            if api_url not in self.open_circuits:
                self.open_circuits.add(api_url)
                print(f"🔴 Circuit opened: {api_url}")
    
    def record_success(self, api_url):
        if self.failures[api_url] > 0 or api_url in self.open_circuits:
            print(f"🟢 Circuit closed: {api_url}")
        self.failures[api_url] = 0
        if api_url in self.open_circuits:
            self.open_circuits.remove(api_url)
    
    def can_request(self, api_url):
        if api_url not in self.open_circuits:
            return True
        
        # Auto-reset after timeout
        if time.time() - self.last_failure[api_url] > self.timeout:
            print(f"⚡ Circuit auto-reset: {api_url}")
            self.open_circuits.remove(api_url)
            self.failures[api_url] = 0
            return True
        
        return False

circuit_breaker = CircuitBreaker()

# ===============================================
# API CONFIG
# ===============================================
FALLBACK_APIS = [
    "https://libretranslate.com/translate",
    "https://translate.argosopentech.com/translate",
    "https://translate.terraprint.co/translate"
]

session = None

# ===============================================
# SERVER SETTINGS
# ===============================================
server_settings = {}

def get_server_settings(guild_id):
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "auto_delete": True,
            "delete_time": 30,
            "total_translations": 0,
            "enabled": True,
            "max_length": 2000
        }
    return server_settings[guild_id]

# ===============================================
# FLAG MAPPING
# ===============================================
FLAG_TO_LANG = {
    '🇻🇳': 'vi', '🇨🇳': 'zh', '🇯🇵': 'ja', '🇰🇷': 'ko', '🇹🇭': 'th',
    '🇮🇩': 'id', '🇵🇭': 'tl', '🇲🇾': 'ms', '🇸🇬': 'en', '🇮🇳': 'hi',
    '🇵🇰': 'ur', '🇧🇩': 'bn', '🇱🇰': 'si', '🇲🇲': 'my', '🇰🇭': 'km',
    '🇱🇦': 'lo', '🇹🇼': 'zh', '🇭🇰': 'zh', '🇲🇴': 'zh',
    '🇬🇧': 'en', '🇺🇸': 'en', '🇫🇷': 'fr', '🇩🇪': 'de', '🇪🇸': 'es',
    '🇮🇹': 'it', '🇵🇹': 'pt', '🇷🇺': 'ru', '🇵🇱': 'pl', '🇳🇱': 'nl',
    '🇸🇪': 'sv', '🇳🇴': 'no', '🇩🇰': 'da', '🇫🇮': 'fi', '🇬🇷': 'el',
    '🇹🇷': 'tr', '🇨🇿': 'cs', '🇭🇺': 'hu', '🇷🇴': 'ro', '🇧🇬': 'bg',
    '🇭🇷': 'hr', '🇸🇰': 'sk', '🇺🇦': 'uk',
    '🇧🇷': 'pt', '🇲🇽': 'es', '🇦🇷': 'es', '🇨🇱': 'es', '🇨🇴': 'es',
    '🇵🇪': 'es', '🇨🇦': 'en',
    '🇸🇦': 'ar', '🇦🇪': 'ar', '🇮🇷': 'fa', '🇮🇱': 'he', '🇪🇬': 'ar',
    '🇿🇦': 'af', '🇳🇬': 'en', '🇰🇪': 'sw',
    '🇦🇺': 'en', '🇳🇿': 'en',
}

# ===============================================
# TRANSLATION FUNCTION (FIXED)
# ===============================================
async def translate_text(text: str, target_lang: str, source_lang: str = 'auto'):
    global session
    
    # Check cache first
    cached = cache.get(text, target_lang)
    if cached:
        return cached
    
    # Create session if needed
    if session is None:
        timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
        session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0'
            }
        )
    
    payload = {
        "q": text,
        "source": source_lang,
        "target": target_lang,
        "format": "text"
    }
    
    last_error = None
    
    # Try each API
    for api_url in FALLBACK_APIS:
        # Skip if circuit is open
        if not circuit_breaker.can_request(api_url):
            print(f"⚠️ Skipping {api_url} (circuit open)")
            continue
        
        try:
            print(f"🔄 Trying API: {api_url}")
            
            async with session.post(api_url, json=payload) as response:
                print(f"📡 Response status: {response.status}")
                
                if response.status == 200:
                    data = await response.json()
                    translated = data.get('translatedText', text)
                    
                    result = {
                        'text': translated,
                        'source': source_lang if source_lang != 'auto' else 'auto'
                    }
                    
                    # Cache and mark success
                    cache.set(text, target_lang, result)
                    circuit_breaker.record_success(api_url)
                    
                    print(f"✅ Translation successful via {api_url}")
                    return result
                    
                elif response.status == 429:
                    print(f"⚠️ Rate limited by {api_url}")
                    circuit_breaker.record_failure(api_url)
                    last_error = "Rate limited"
                    await asyncio.sleep(1)
                    
                else:
                    error_text = await response.text()
                    print(f"❌ API error {response.status}: {error_text[:200]}")
                    circuit_breaker.record_failure(api_url)
                    last_error = f"HTTP {response.status}"
                    
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout: {api_url}")
            circuit_breaker.record_failure(api_url)
            last_error = "Timeout"
            
        except aiohttp.ClientError as e:
            print(f"🔌 Connection error {api_url}: {str(e)[:100]}")
            circuit_breaker.record_failure(api_url)
            last_error = f"Connection: {str(e)[:50]}"
            
        except Exception as e:
            print(f"💥 Unexpected error {api_url}: {str(e)[:100]}")
            circuit_breaker.record_failure(api_url)
            last_error = f"Error: {str(e)[:50]}"
    
    # All APIs failed
    print(f"❌ All APIs failed. Last error: {last_error}")
    return None

# ===============================================
# BOT EVENTS
# ===============================================
@bot.event
async def on_ready():
    print('=' * 70)
    print(f'✅ Bot: {bot.user.name} ONLINE!')
    print(f'🆔 ID: {bot.user.id}')
    print(f'📊 Servers: {len(bot.guilds)}')
    print(f'👥 Users: {sum(g.member_count for g in bot.guilds)}')
    print(f'🌍 Languages: {len(FLAG_TO_LANG)} flags')
    print(f'🔧 Production Mode: Rate Limited + Queued + Cached')
    print(f'⚡ Max: 50 req/min per server, 10 req/min per user')
    if KEEP_ALIVE_AVAILABLE:
        print('✅ Keep-Alive: ENABLED')
    print('=' * 70)
    
    await translation_queue.start_workers()
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(FLAG_TO_LANG)} flags 🌐 | !help"
        )
    )

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    
    emoji = str(reaction.emoji)
    
    if emoji not in FLAG_TO_LANG:
        return
    
    message = reaction.message
    
    if not message.content or message.content.strip() == "":
        return
    
    settings = get_server_settings(message.guild.id)
    
    if not settings["enabled"]:
        return
    
    if len(message.content) > settings["max_length"]:
        await message.channel.send(
            f"❌ {user.mention} Text too long! (Max {settings['max_length']})",
            delete_after=5
        )
        return
    
    user_key = f"user:{user.id}"
    guild_key = f"guild:{message.guild.id}"
    
    if not user_limiter.can_request(user_key):
        wait = user_limiter.get_wait_time(user_key)
        await message.channel.send(
            f"⏱️ {user.mention} Slow down! Wait {int(wait)}s",
            delete_after=5
        )
        return
    
    if not guild_limiter.can_request(guild_key):
        return
    
    if not global_limiter.can_request("global"):
        return
    
    debounce_key = f"{message.id}:{emoji}:{user.id}"
    
    async def process_translation():
        async with message.channel.typing():
            target_lang = FLAG_TO_LANG[emoji]
            result = await translate_text(message.content, target_lang)
            
            if not result:
                await message.channel.send(
                    f"❌ {user.mention} Translation failed! All APIs unavailable.",
                    delete_after=8
                )
                return
            
            embed = discord.Embed(
                title=f"🌐 {emoji} Translation → {target_lang.upper()}",
                description=result['text'][:4000],
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            footer_text = f"Requested by {user.name}"
            if settings["auto_delete"]:
                footer_text += f" • ⏱️ {settings['delete_time']}s"
            
            embed.set_footer(text=footer_text, icon_url=user.display_avatar.url)
            
            translation_msg = await message.reply(embed=embed, mention_author=False)
            
            settings["total_translations"] += 1
            
            if settings["auto_delete"]:
                await asyncio.sleep(settings["delete_time"])
                try:
                    await translation_msg.delete()
                except:
                    pass
    
    await debouncer.debounce(debounce_key, process_translation())

# ===============================================
# COMMANDS
# ===============================================
@bot.command(name='translate', aliases=['tr', 't'])
@commands.cooldown(1, 3, commands.BucketType.user)
async def translate_command(ctx, lang: str = None, *, text: str = None):
    if not lang or not text:
        await ctx.send(
            "❌ **Usage:** `!translate <language> <text>`\n"
            "**Example:** `!translate vi Hello world`"
        )
        return
    
    settings = get_server_settings(ctx.guild.id)
    
    if len(text) > settings["max_length"]:
        await ctx.send(f"❌ Text too long! (Max {settings['max_length']})")
        return
    
    user_key = f"user:{ctx.author.id}"
    if not user_limiter.can_request(user_key):
        wait = user_limiter.get_wait_time(user_key)
        await ctx.send(f"⏱️ Slow down! Wait {int(wait)}s")
        return
    
    async with ctx.typing():
        result = await translate_text(text, lang)
        
        if not result:
            await ctx.send("❌ Translation failed! Please try again later.")
            return
        
        embed = discord.Embed(
            title=f"🌐 Translation → {lang.upper()}",
            description=result['text'][:4000],
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        
        if len(text) <= 300:
            embed.add_field(name="📝 Original", value=f"```{text[:1000]}```", inline=False)
        
        await ctx.send(embed=embed)

@bot.command(name='autodelete', aliases=['ad'])
@commands.has_permissions(manage_messages=True)
async def auto_delete_toggle(ctx, mode: str = None):
    settings = get_server_settings(ctx.guild.id)
    
    if mode is None:
        status = "✅ ON" if settings["auto_delete"] else "❌ OFF"
        await ctx.send(
            f"**Auto-delete:** {status}\n"
            f"**Delete after:** {settings['delete_time']}s\n"
            f"Use: `!autodelete on/off`"
        )
        return
    
    if mode.lower() in ['on', 'enable', '1', 'yes']:
        settings["auto_delete"] = True
        await ctx.send(f"✅ Auto-delete: **ON** ({settings['delete_time']}s)")
    elif mode.lower() in ['off', 'disable', '0', 'no']:
        settings["auto_delete"] = False
        await ctx.send("✅ Auto-delete: **OFF**")
    else:
        await ctx.send("❌ Use: `!autodelete on/off`")

@bot.command(name='deletetime', aliases=['dt'])
@commands.has_permissions(manage_messages=True)
async def delete_time(ctx, seconds: int = None):
    settings = get_server_settings(ctx.guild.id)
    
    if seconds is None:
        await ctx.send(
            f"⏱️ **Current:** {settings['delete_time']}s\n"
            f"**Use:** `!deletetime <seconds>`"
        )
        return
    
    if seconds < 5 or seconds > 600:
        await ctx.send("❌ Range: 5-600 seconds")
        return
    
    settings["delete_time"] = seconds
    await ctx.send(f"✅ Delete time: **{seconds}s**")

@bot.command(name='maxlength')
@commands.has_permissions(manage_guild=True)
async def max_length(ctx, length: int = None):
    settings = get_server_settings(ctx.guild.id)
    
    if length is None:
        await ctx.send(f"📏 Current max: **{settings['max_length']}** chars")
        return
    
    if length < 100 or length > 3000:
        await ctx.send("❌ Range: 100-3000")
        return
    
    settings["max_length"] = length
    await ctx.send(f"✅ Max length: **{length}** chars")

@bot.command(name='toggle')
@commands.has_permissions(manage_guild=True)
async def toggle_bot(ctx, mode: str = None):
    settings = get_server_settings(ctx.guild.id)
    
    if mode is None:
        status = "✅ ENABLED" if settings["enabled"] else "❌ DISABLED"
        await ctx.send(f"Bot status: {status}")
        return
    
    if mode.lower() in ['on', 'enable']:
        settings["enabled"] = True
        await ctx.send("✅ Bot: **ENABLED**")
    elif mode.lower() in ['off', 'disable']:
        settings["enabled"] = False
        await ctx.send("❌ Bot: **DISABLED**")

@bot.command(name='flags', aliases=['languages'])
async def flags_list(ctx):
    embed = discord.Embed(
        title=f"🌍 Supported Flags ({len(FLAG_TO_LANG)} languages)",
        description="React with flag to translate!",
        color=discord.Color.purple()
    )
    
    flags = list(FLAG_TO_LANG.items())
    col_size = len(flags) // 3
    
    col1 = "\n".join([f"{e} `{c}`" for e, c in flags[:col_size]])
    col2 = "\n".join([f"{e} `{c}`" for e, c in flags[col_size:col_size*2]])
    col3 = "\n".join([f"{e} `{c}`" for e, c in flags[col_size*2:]])
    
    if col1: embed.add_field(name="Asia & Europe", value=col1, inline=True)
    if col2: embed.add_field(name="Americas", value=col2, inline=True)
    if col3: embed.add_field(name="Others", value=col3, inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='help', aliases=['h'])
async def help_command(ctx):
    embed = discord.Embed(
        title="🤖 Translation Bot Help",
        description=f"Production-ready • {len(FLAG_TO_LANG)} languages • Rate limited",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🌐 Auto Translation",
        value="React with flag (🇻🇳 🇺🇸 🇯🇵...) to translate message!",
        inline=False
    )
    
    embed.add_field(
        name="⚡ Commands",
        value=(
            "`!translate <code> <text>` - Manual translate\n"
            "`!flags` - List all flags\n"
            "`!stats` - View statistics\n"
            "`!settings` - View settings"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Admin Commands",
        value=(
            "`!autodelete on/off` - Toggle auto-delete\n"
            "`!deletetime <sec>` - Set delete timer\n"
            "`!maxlength <chars>` - Set max text length\n"
            "`!toggle on/off` - Enable/disable bot"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚡ Rate Limits",
        value="10 translations/min per user\n50 translations/min per server",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='settings')
async def view_settings(ctx):
    settings = get_server_settings(ctx.guild.id)
    
    embed = discord.Embed(
        title=f"⚙️ Server Settings",
        color=discord.Color.gold()
    )
    
    embed.add_field(name="🔌 Status", 
                    value="✅ ON" if settings["enabled"] else "❌ OFF", 
                    inline=True)
    embed.add_field(name="🗑️ Auto-delete", 
                    value="✅ ON" if settings["auto_delete"] else "❌ OFF", 
                    inline=True)
    embed.add_field(name="⏱️ Delete time", 
                    value=f"{settings['delete_time']}s", 
                    inline=True)
    embed.add_field(name="📏 Max length", 
                    value=f"{settings['max_length']} chars", 
                    inline=True)
    embed.add_field(name="📊 Translations", 
                    value=f"{settings['total_translations']}", 
                    inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='stats')
async def stats_command(ctx):
    settings = get_server_settings(ctx.guild.id)
    
    embed = discord.Embed(
        title="📊 Bot Statistics",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="🌐 Languages", value=len(FLAG_TO_LANG), inline=True)
    embed.add_field(name="💾 Cache size", value=len(cache.cache), inline=True)
    embed.add_field(name="📝 Queue", value=translation_queue.queue.qsize(), inline=True)
    embed.add_field(name="⚡ Processing", value=translation_queue.processing, inline=True)
    embed.add_field(name="📊 Server translations", value=settings['total_translations'], inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='apitest')
@commands.has_permissions(administrator=True)
async def api_test(ctx):
    """Test API connectivity"""
    embed = discord.Embed(title="🔧 API Test", color=discord.Color.blue())
    
    test_text = "Hello"
    test_lang = "vi"
    
    for api_url in FALLBACK_APIS:
        status = "🟢" if circuit_breaker.can_request(api_url) else "🔴"
        failures = circuit_breaker.failures.get(api_url, 0)
        embed.add_field(
            name=f"{status} {api_url.split('//')[1].split('/')[0]}",
            value=f"Failures: {failures}",
            inline=False
        )
    
    await ctx.send(embed=embed)
    
    # Try translation
    async with ctx.typing():
        result = await translate_text(test_text, test_lang)
        if result:
            await ctx.send(f"✅ Test successful: `{test_text}` → `{result['text']}`")
        else:
            await ctx.send("❌ Test failed - check logs")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏱️ Cooldown! Wait {error.retry_after:.1f}s", delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument! Use `!help`", delete_after=5)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ No permission!", delete_after=5)
    else:
        print(f"Error: {error}")

@bot.event
async def on_close():
    global session
    if session:
        await session.close()

# ===============================================
# MAIN
# ===============================================
if __name__ == "__main__":
    if KEEP_ALIVE_AVAILABLE:
        keep_alive()
    
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        print("=" * 70)
        print("❌ DISCORD_TOKEN not found!")
        print("=" * 70)
        exit(1)
    
    print("🚀 Starting Production Translation Bot...")
    print("⚡ Features: Rate Limiting + Queue + Cache + Circuit Breaker")
    print("🔒 Protection: 10 req/min/user, 50 req/min/server, 200 req/min/global")
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid token!")
    except Exception as e:
        print(f"❌ Error: {e}")
