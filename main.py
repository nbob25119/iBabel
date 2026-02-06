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
    max_messages=300,  # Giảm thêm để tiết kiệm RAM trên free tier
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
    def __init__(self, max_size=500, ttl=1800):  # Giảm ttl để tiết kiệm RAM
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
    def __init__(self, max_concurrent=3):  # Giữ 3 để an toàn, tăng lên 4 nếu cần
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

debouncer = Debouncer(delay=1.5)

# ===============================================
# CIRCUIT BREAKER
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
            "q": text[:500],
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

# SimplyTranslate - Free alternative
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
            "auto_delete": True,
            "delete_time": 30,
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
    
    # Task loop để monitor 24/7 (print queue size mỗi 5p)
    async def monitor_loop():
        while True:
            print(f"📊 Queue size: {translation_queue.queue.qsize()} | Processing: {translation_queue.processing}")
            await asyncio.sleep(300)  # 5 phút
    asyncio.create_task(monitor_loop())

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
                    f"❌ {user.mention} Translation failed! All APIs unavailable. Please try again.",
                    delete_after=8
                )
                return
            
            embed = discord.Embed(
                description=result['text'][:4000],  # Chỉ show bản dịch
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=f"{emoji} → {target_lang.upper()}")  # Tiêu đề gọn đẹp
            
            footer_text = f"By {user.name} • API: {result.get('api', 'Unknown')}"
            if settings["auto_delete"]:
                footer_text += f" • ⏱️ {settings['delete_time']}s"
            
            embed.set_footer(text=footer_text, icon_url=user.display_avatar.url)
            
            # Reply trực tiếp vào message gốc
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
            await ctx.send("❌ Translation failed! All APIs unavailable.")
            return
        
        embed = discord.Embed(
            description=result['text'][:4000],  # Chỉ show bản dịch
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"🌐 → {lang.upper()}")  # Tiêu đề gọn
        
        embed.set_footer(text=f"API: {result.get('api', 'Unknown')}")
        
        # Reply trực tiếp vào tin nhắn lệnh
        await ctx.message.reply(embed=embed, mention_author=False)

# Các command khác giữ nguyên (autodelete, deletetime, v.v.) vì không ảnh hưởng

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
