import discord 
from discord.ext import commands
import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import hashlib
import time
import json

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
    max_messages=500,  # Reduced for free tier
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

user_limiter = RateLimiter(max_requests=5, window=60)  # 5 per minute
guild_limiter = RateLimiter(max_requests=20, window=60)  # 20 per minute
global_limiter = RateLimiter(max_requests=50, window=60)  # 50 per minute total

# ===============================================
# TRANSLATION CACHE
# ===============================================
class TranslationCache:
    def __init__(self, max_size=500, ttl=3600):  # Reduced for free tier
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
    def __init__(self, max_concurrent=3):  # Reduced for free tier
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

translation_queue = TranslationQueue(max_concurrent=3)

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

debouncer = Debouncer(delay=3.0)

# ===============================================
# CIRCUIT BREAKER
# ===============================================
class CircuitBreaker:
    def __init__(self, failure_threshold=3, timeout=30):  # More aggressive for free APIs
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = defaultdict(int)
        self.last_failure = defaultdict(float)
        self.open_circuits = set()
    
    def record_failure(self, api_url):
        self.failures[api_url] += 1
        self.last_failure[api_url] = time.time()
        
        if self.failures[api_url] >= self.failure_threshold:
            self.open_circuits.add(api_url)
            print(f"🔴 Circuit breaker opened for {api_url}")
    
    def record_success(self, api_url):
        self.failures[api_url] = 0
        if api_url in self.open_circuits:
            self.open_circuits.remove(api_url)
            print(f"🟢 Circuit breaker closed for {api_url}")
    
    def can_request(self, api_url):
        if api_url not in self.open_circuits:
            return True
        
        if time.time() - self.last_failure[api_url] > self.timeout:
            self.open_circuits.remove(api_url)
            self.failures[api_url] = 0
            return True
        
        return False

circuit_breaker = CircuitBreaker()

# ===============================================
# MULTI-API TRANSLATION WITH FALLBACKS
# ===============================================
session = None

# MyMemory API - Free, stable, no API key needed
async def translate_mymemory(text: str, target_lang: str, source_lang: str = 'auto'):
    global session
    
    try:
        url = "https://api.mymemory.translated.net/get"
        params = {
            "q": text[:500],  # Limit to 500 chars
            "langpair": f"{source_lang}|{target_lang}"
        }
        
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status == 200:
                data = await response.json()
                if data.get('responseStatus') == 200:
                    return {
                        'text': data['responseData']['translatedText'],
                        'source': source_lang,
                        'api': 'MyMemory'
                    }
    except Exception as e:
        print(f"MyMemory error: {e}")
    return None

# LibreTranslate - Multiple free instances
async def translate_libretranslate(text: str, target_lang: str, source_lang: str = 'auto', instance_url: str = None):
    global session
    
    instances = [
        "https://translate.astian.org/translate",
        "https://translate.fedilab.app/translate",
        "https://translate.argosopentech.com/translate",
    ] if not instance_url else [instance_url]
    
    for url in instances:
        if not circuit_breaker.can_request(url):
            continue
            
        try:
            payload = {
                "q": text,
                "source": source_lang,
                "target": target_lang,
                "format": "text"
            }
            
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    circuit_breaker.record_success(url)
                    return {
                        'text': data.get('translatedText', text),
                        'source': source_lang,
                        'api': 'LibreTranslate'
                    }
                elif response.status == 429:
                    circuit_breaker.record_failure(url)
                    continue
                    
        except asyncio.TimeoutError:
            circuit_breaker.record_failure(url)
            continue
        except Exception as e:
            circuit_breaker.record_failure(url)
            continue
    
    return None

# Lingva Translate - Free, no API key
async def translate_lingva(text: str, target_lang: str, source_lang: str = 'auto'):
    global session
    
    instances = [
        "https://lingva.ml/api/v1",
        "https://translate.plausibility.cloud/api/v1",
    ]
    
    for base_url in instances:
        if not circuit_breaker.can_request(base_url):
            continue
            
        try:
            url = f"{base_url}/{source_lang}/{target_lang}/{text[:500]}"
            
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status == 200:
                    data = await response.json()
                    circuit_breaker.record_success(base_url)
                    return {
                        'text': data.get('translation', text),
                        'source': source_lang,
                        'api': 'Lingva'
                    }
        except Exception as e:
            circuit_breaker.record_failure(base_url)
            continue
    
    return None

# SimplytTranslate - Free alternative
async def translate_simplytranslate(text: str, target_lang: str, source_lang: str = 'auto'):
    global session
    
    instances = [
        "https://simplytranslate.org/api/translate",
    ]
    
    for url in instances:
        if not circuit_breaker.can_request(url):
            continue
            
        try:
            params = {
                "engine": "google",
                "text": text[:500],
                "sl": source_lang,
                "tl": target_lang
            }
            
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status == 200:
                    data = await response.json()
                    circuit_breaker.record_success(url)
                    return {
                        'text': data.get('translated-text', text),
                        'source': source_lang,
                        'api': 'SimplyTranslate'
                    }
        except Exception as e:
            circuit_breaker.record_failure(url)
            continue
    
    return None

# Main translation function with cascading fallbacks
async def translate_text(text: str, target_lang: str, source_lang: str = 'auto'):
    global session
    
    # Check cache first
    cached = cache.get(text, target_lang)
    if cached:
        return cached
    
    # Initialize session if needed
    if session is None:
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
        session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    
    # Try APIs in order of reliability
    translation_functions = [
        translate_mymemory,
        translate_lingva,
        translate_libretranslate,
        translate_simplytranslate,
    ]
    
    for translate_func in translation_functions:
        try:
            result = await translate_func(text, target_lang, source_lang)
            if result and result['text'] and result['text'] != text:
                cache.set(text, target_lang, result)
                return result
        except Exception as e:
            print(f"Translation function {translate_func.__name__} error: {e}")
            continue
    
    return None

# ===============================================
# SERVER SETTINGS
# ===============================================
server_settings = {}

def get_server_settings(guild_id):
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "total_translations": 0,
            "enabled": True,
            "max_length": 1000
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
    print(f'🔧 Render Free Tier Optimized')
    print(f'⚡ Multi-API: MyMemory + Lingva + LibreTranslate + SimplyTranslate')
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
        return  # Silent fail to avoid rate limit
    
    user_key = f"user:{user.id}"
    guild_key = f"guild:{message.guild.id}"
    
    if not user_limiter.can_request(user_key):
        return  # Silent fail
    
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
                return  # Silent fail
            
            embed = discord.Embed(
                description=result['text'][:4000],
                color=discord.Color.blue()
            )
            
            embed.set_footer(text=f"{user.name}", icon_url=user.display_avatar.url)
            
            # Retry logic for Discord API
            for attempt in range(3):
                try:
                    translation_msg = await message.channel.send(embed=embed)
                    settings["total_translations"] += 1
                    
                    # Auto-delete after 30 seconds
                    await asyncio.sleep(30)
                    try:
                        await translation_msg.delete()
                    except:
                        pass
                    break
                except discord.HTTPException as e:
                    if "429" in str(e):
                        wait_time = (2 ** attempt) * 2  # Exponential backoff: 2s, 4s, 8s
                        print(f"⚠️ Rate limited, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"Discord API error: {e}")
                        break
                except Exception as e:
                    print(f"Error sending message: {e}")
                    break
    
    await debouncer.debounce(debounce_key, process_translation())

# ===============================================
# COMMANDS (ADMIN ONLY - NO TRANSLATION COMMANDS)
# ===============================================

@bot.command(name='maxlength')
@commands.has_permissions(manage_guild=True)
async def max_length(ctx, length: int = None):
    settings = get_server_settings(ctx.guild.id)
    
    if length is None:
        await ctx.send(f"📏 Current max: **{settings['max_length']}** chars")
        return
    
    if length < 100 or length > 2000:
        await ctx.send("❌ Range: 100-2000")
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
        title="🤖 Translation Bot",
        description=f"React emoji để dịch • Auto-delete sau 30s",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🌐 Cách dùng",
        value="React flag emoji (🇻🇳 🇺🇸 🇯🇵...) vào tin nhắn để dịch!",
        inline=False
    )
    
    embed.add_field(
        name="⚡ Commands",
        value=(
            "`!flags` - Xem tất cả flag\n"
            "`!stats` - Thống kê\n"
            "`!settings` - Cài đặt server"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Admin",
        value=(
            "`!toggle on/off` - Bật/tắt bot\n"
            "`!maxlength <số>` - Giới hạn độ dài"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚡ Giới hạn",
        value="5 lần/phút mỗi user • 20 lần/phút mỗi server",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='settings')
async def view_settings(ctx):
    settings = get_server_settings(ctx.guild.id)
    
    embed = discord.Embed(
        title=f"⚙️ Cài đặt Server",
        color=discord.Color.gold()
    )
    
    embed.add_field(name="🔌 Trạng thái", 
                    value="✅ BẬT" if settings["enabled"] else "❌ TẮT", 
                    inline=True)
    embed.add_field(name="🗑️ Auto-delete", 
                    value="30 giây (cố định)", 
                    inline=True)
    embed.add_field(name="📏 Độ dài tối đa", 
                    value=f"{settings['max_length']} ký tự", 
                    inline=True)
    embed.add_field(name="📊 Đã dịch", 
                    value=f"{settings['total_translations']} lần", 
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
async def api_test(ctx):
    """Test all translation APIs"""
    test_text = "Hello world"
    test_lang = "vi"
    
    embed = discord.Embed(
        title="🔧 API Status Test",
        description=f"Testing: '{test_text}' → {test_lang}",
        color=discord.Color.orange()
    )
    
    async with ctx.typing():
        # Test MyMemory
        result1 = await translate_mymemory(test_text, test_lang)
        status1 = "✅ OK" if result1 else "❌ FAIL"
        
        # Test Lingva
        result2 = await translate_lingva(test_text, test_lang)
        status2 = "✅ OK" if result2 else "❌ FAIL"
        
        # Test LibreTranslate
        result3 = await translate_libretranslate(test_text, test_lang)
        status3 = "✅ OK" if result3 else "❌ FAIL"
        
        # Test SimplyTranslate
        result4 = await translate_simplytranslate(test_text, test_lang)
        status4 = "✅ OK" if result4 else "❌ FAIL"
        
        embed.add_field(name="MyMemory", value=status1, inline=True)
        embed.add_field(name="Lingva", value=status2, inline=True)
        embed.add_field(name="LibreTranslate", value=status3, inline=True)
        embed.add_field(name="SimplyTranslate", value=status4, inline=True)
        
        working_count = sum([bool(r) for r in [result1, result2, result3, result4]])
        embed.set_footer(text=f"Working APIs: {working_count}/4")
        
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.CommandOnCooldown):
        return  # Silent fail to avoid more API calls
    elif isinstance(error, commands.MissingRequiredArgument):
        return  # Silent fail
    elif isinstance(error, commands.MissingPermissions):
        return  # Silent fail
    elif "429" in str(error) or "Too Many Requests" in str(error):
        print(f"⚠️ Discord Rate Limited! Backing off...")
        await asyncio.sleep(10)  # Wait 10 seconds
        return
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
    
    print("🚀 Starting Translation Bot (Render Free Tier)")
    print("⚡ Multi-API: MyMemory + Lingva + LibreTranslate + SimplyTranslate")
    print("🔒 Protection: Rate limiting + Queue + Cache + Circuit Breaker")
    print("💾 Optimized for 512MB RAM")
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid token!")
    except Exception as e:
        print(f"❌ Error: {e}")
