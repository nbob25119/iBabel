import discord 
from discord.ext import commands, tasks
import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from collections import deque, defaultdict
from functools import lru_cache
import hashlib
import time
import logging

# ===============================================
# LOGGING SETUP
# ===============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===============================================
# IMPORT KEEP-ALIVE
# ===============================================
try:
    from keep_alive import keep_alive
    KEEP_ALIVE_AVAILABLE = True
except ImportError:
    KEEP_ALIVE_AVAILABLE = False
    logger.warning("⚠️ keep_alive.py not found")

# ===============================================
# BOT CONFIG
# ===============================================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
bot = commands.Bot(
    command_prefix='!', 
    intents=intents,
    max_messages=1000  # Giảm memory usage
)

# ===============================================
# TRANSLATION APIs
# ===============================================
FALLBACK_APIS = [
    "https://libretranslate.com/translate",
    "https://translate.argosopentech.com/translate",
    "https://translate.terraprint.co/translate"
]

session = None

# ===============================================
# FLAG → LANGUAGE MAPPING
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
# ADVANCED CACHING SYSTEM
# ===============================================
class TranslationCache:
    """LRU Cache với TTL cho translations"""
    
    def __init__(self, max_size=1000, ttl_seconds=3600):
        self.cache = {}
        self.access_order = deque()
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.hits = 0
        self.misses = 0
    
    def _make_key(self, text: str, target_lang: str) -> str:
        """Tạo cache key từ text + language"""
        content = f"{text}|{target_lang}".encode('utf-8')
        return hashlib.md5(content).hexdigest()
    
    def get(self, text: str, target_lang: str):
        """Lấy từ cache nếu còn valid"""
        key = self._make_key(text, target_lang)
        
        if key in self.cache:
            entry = self.cache[key]
            # Check TTL
            if time.time() - entry['timestamp'] < self.ttl:
                self.hits += 1
                # Update LRU
                self.access_order.remove(key)
                self.access_order.append(key)
                return entry['value']
            else:
                # Expired
                del self.cache[key]
                self.access_order.remove(key)
        
        self.misses += 1
        return None
    
    def set(self, text: str, target_lang: str, value: dict):
        """Lưu vào cache"""
        key = self._make_key(text, target_lang)
        
        # Evict oldest if full
        if len(self.cache) >= self.max_size and key not in self.cache:
            oldest = self.access_order.popleft()
            del self.cache[oldest]
        
        # Add/update
        if key in self.access_order:
            self.access_order.remove(key)
        
        self.access_order.append(key)
        self.cache[key] = {
            'value': value,
            'timestamp': time.time()
        }
    
    def get_stats(self):
        """Cache statistics"""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            'size': len(self.cache),
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': f"{hit_rate:.1f}%"
        }

# Global cache instance
translation_cache = TranslationCache(max_size=1000, ttl_seconds=3600)

# ===============================================
# RATE LIMITER
# ===============================================
class RateLimiter:
    """Token bucket rate limiter per user & server"""
    
    def __init__(self, requests_per_minute=10, burst=15):
        self.rpm = requests_per_minute
        self.burst = burst
        self.buckets = defaultdict(lambda: {
            'tokens': burst,
            'last_update': time.time()
        })
    
    def _refill_bucket(self, bucket):
        """Refill tokens based on time elapsed"""
        now = time.time()
        elapsed = now - bucket['last_update']
        refill = elapsed * (self.rpm / 60.0)
        bucket['tokens'] = min(self.burst, bucket['tokens'] + refill)
        bucket['last_update'] = now
    
    def check(self, user_id: int) -> bool:
        """Check if request is allowed"""
        bucket = self.buckets[user_id]
        self._refill_bucket(bucket)
        
        if bucket['tokens'] >= 1:
            bucket['tokens'] -= 1
            return True
        return False
    
    def get_wait_time(self, user_id: int) -> float:
        """Get seconds to wait before next request"""
        bucket = self.buckets[user_id]
        self._refill_bucket(bucket)
        if bucket['tokens'] >= 1:
            return 0
        return (1 - bucket['tokens']) * (60.0 / self.rpm)

# Per-user rate limiter (10 req/min, burst 15)
user_rate_limiter = RateLimiter(requests_per_minute=10, burst=15)

# Per-server rate limiter (100 req/min, burst 120)
server_rate_limiter = RateLimiter(requests_per_minute=100, burst=120)

# ===============================================
# DEBOUNCER (Prevent spam reactions)
# ===============================================
class Debouncer:
    """Debounce reactions - chặn spam reactions"""
    
    def __init__(self, cooldown_seconds=3):
        self.cooldown = cooldown_seconds
        self.last_requests = {}
    
    def check(self, user_id: int, message_id: int, emoji: str) -> bool:
        """Check if request should be processed"""
        key = f"{user_id}:{message_id}:{emoji}"
        now = time.time()
        
        if key in self.last_requests:
            if now - self.last_requests[key] < self.cooldown:
                return False
        
        self.last_requests[key] = now
        return True
    
    def cleanup(self):
        """Remove old entries (>1 hour)"""
        now = time.time()
        self.last_requests = {
            k: v for k, v in self.last_requests.items() 
            if now - v < 3600
        }

debouncer = Debouncer(cooldown_seconds=3)

# ===============================================
# TRANSLATION QUEUE + WORKER
# ===============================================
class TranslationQueue:
    """Async queue với worker pool"""
    
    def __init__(self, max_workers=5, max_queue_size=100):
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.workers = []
        self.max_workers = max_workers
        self.processing = 0
        self.processed = 0
        self.failed = 0
    
    async def add_task(self, task_data):
        """Thêm task vào queue"""
        try:
            await asyncio.wait_for(
                self.queue.put(task_data), 
                timeout=5
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("⚠️ Queue full - task rejected")
            return False
    
    async def worker(self, worker_id: int):
        """Worker process tasks"""
        logger.info(f"✅ Worker {worker_id} started")
        
        while True:
            try:
                task = await self.queue.get()
                self.processing += 1
                
                await self.process_translation(task)
                
                self.queue.task_done()
                self.processing -= 1
                self.processed += 1
                
            except Exception as e:
                logger.error(f"❌ Worker {worker_id} error: {e}")
                self.failed += 1
                self.processing -= 1
    
    async def process_translation(self, task):
        """Process single translation task"""
        message = task['message']
        user = task['user']
        target_lang = task['target_lang']
        emoji = task['emoji']
        settings = task['settings']
        
        try:
            # Check cache first
            cached = translation_cache.get(message.content, target_lang)
            
            if cached:
                result = cached
                logger.info(f"💾 Cache hit: {target_lang}")
            else:
                # Translate with API
                async with message.channel.typing():
                    result = await translate_text(message.content, target_lang)
                
                if not result:
                    await message.channel.send(
                        f"❌ {user.mention} Translation failed - all APIs busy",
                        delete_after=5
                    )
                    return
                
                # Save to cache
                translation_cache.set(message.content, target_lang, result)
            
            # Send response
            embed = discord.Embed(
                title=f"🌐 {emoji} Translation → {target_lang.upper()}",
                description=result['text'],
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            if len(message.content) <= 400:
                embed.add_field(
                    name="📝 Original",
                    value=f"```{message.content[:400]}```",
                    inline=False
                )
            
            footer_text = f"Requested by {user.name}"
            if settings["auto_delete"]:
                footer_text += f" • Deletes in {settings['delete_time']}s"
            
            embed.set_footer(text=footer_text, icon_url=user.display_avatar.url)
            
            translation_msg = await message.channel.send(
                f"💬 {user.mention}",
                embed=embed
            )
            
            settings["total_translations"] += 1
            
            # Auto-delete
            if settings["auto_delete"]:
                await asyncio.sleep(settings["delete_time"])
                try:
                    await translation_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                    
        except Exception as e:
            logger.error(f"❌ Translation error: {e}")
    
    async def start_workers(self):
        """Start worker pool"""
        for i in range(self.max_workers):
            worker = asyncio.create_task(self.worker(i))
            self.workers.append(worker)
    
    def get_stats(self):
        """Queue statistics"""
        return {
            'queue_size': self.queue.qsize(),
            'processing': self.processing,
            'processed': self.processed,
            'failed': self.failed
        }

# Global queue instance
translation_queue = TranslationQueue(max_workers=5, max_queue_size=100)

# ===============================================
# SERVER SETTINGS DATABASE
# ===============================================
server_settings = {}

def get_server_settings(guild_id):
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "auto_delete": True,
            "delete_time": 30,
            "total_translations": 0
        }
    return server_settings[guild_id]

# ===============================================
# TRANSLATION FUNCTION
# ===============================================
async def translate_text(text: str, target_lang: str, source_lang: str = 'auto'):
    """Translate with fallback APIs"""
    global session
    
    if session is None:
        session = aiohttp.ClientSession()
    
    payload = {
        "q": text,
        "source": source_lang,
        "target": target_lang,
        "format": "text"
    }
    
    for api_url in FALLBACK_APIS:
        try:
            async with session.post(
                api_url, 
                json=payload, 
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        'text': data.get('translatedText', text),
                        'source': source_lang if source_lang != 'auto' else 'auto'
                    }
        except Exception as e:
            logger.warning(f"⚠️ API {api_url} failed: {e}")
            continue
    
    return None

# ===============================================
# METRICS TASK
# ===============================================
@tasks.loop(minutes=5)
async def log_metrics():
    """Log system metrics every 5 minutes"""
    cache_stats = translation_cache.get_stats()
    queue_stats = translation_queue.get_stats()
    
    logger.info("=" * 60)
    logger.info("📊 SYSTEM METRICS")
    logger.info(f"🏢 Servers: {len(bot.guilds)}")
    logger.info(f"👥 Users: {sum(g.member_count for g in bot.guilds)}")
    logger.info(f"💾 Cache: {cache_stats['size']}/{translation_cache.max_size} | Hit rate: {cache_stats['hit_rate']}")
    logger.info(f"📦 Queue: {queue_stats['queue_size']} waiting | {queue_stats['processing']} processing")
    logger.info(f"✅ Processed: {queue_stats['processed']} | ❌ Failed: {queue_stats['failed']}")
    logger.info("=" * 60)

# ===============================================
# CLEANUP TASK
# ===============================================
@tasks.loop(hours=1)
async def cleanup_task():
    """Cleanup old data"""
    debouncer.cleanup()
    logger.info("🧹 Cleanup completed")

# ===============================================
# BOT EVENTS
# ===============================================
@bot.event
async def on_ready():
    logger.info('=' * 70)
    logger.info(f'✅ Bot: {bot.user.name} ONLINE!')
    logger.info(f'🆔 ID: {bot.user.id}')
    logger.info(f'📊 Servers: {len(bot.guilds)}')
    logger.info(f'👥 Users: {sum(g.member_count for g in bot.guilds)}')
    logger.info(f'🌍 Languages: {len(FLAG_TO_LANG)} flags')
    logger.info(f'🔧 API: LibreTranslate (Multi-instance)')
    logger.info(f'⚙️ Workers: {translation_queue.max_workers}')
    logger.info(f'💾 Cache: {translation_cache.max_size} items')
    logger.info('=' * 70)
    
    # Start workers
    await translation_queue.start_workers()
    
    # Start background tasks
    if not log_metrics.is_running():
        log_metrics.start()
    if not cleanup_task.is_running():
        cleanup_task.start()
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(FLAG_TO_LANG)} flags 🌐 | !help"
        )
    )

@bot.event
async def on_reaction_add(reaction, user):
    """Handle translation reactions with full protection"""
    
    if user.bot:
        return
    
    emoji = str(reaction.emoji)
    
    if emoji not in FLAG_TO_LANG:
        return
    
    message = reaction.message
    
    # Validation
    if not message.content or message.content.strip() == "":
        return
    
    if len(message.content) > 3000:
        await message.channel.send(
            f"❌ {user.mention} Message too long! (Max 3000 chars)",
            delete_after=5
        )
        return
    
    # Debounce check
    if not debouncer.check(user.id, message.id, emoji):
        logger.info(f"🚫 Debounced: {user.name} on message {message.id}")
        return
    
    # User rate limit
    if not user_rate_limiter.check(user.id):
        wait_time = user_rate_limiter.get_wait_time(user.id)
        await message.channel.send(
            f"⏳ {user.mention} Slow down! Wait {wait_time:.1f}s",
            delete_after=5
        )
        return
    
    # Server rate limit
    if not server_rate_limiter.check(message.guild.id):
        await message.channel.send(
            f"⚠️ Server rate limit reached! Try again in a moment.",
            delete_after=5
        )
        return
    
    # Add to queue
    settings = get_server_settings(message.guild.id)
    
    task_data = {
        'message': message,
        'user': user,
        'target_lang': FLAG_TO_LANG[emoji],
        'emoji': emoji,
        'settings': settings
    }
    
    success = await translation_queue.add_task(task_data)
    
    if not success:
        await message.channel.send(
            f"⚠️ {user.mention} Queue full! Please wait.",
            delete_after=5
        )

# ===============================================
# COMMANDS
# ===============================================
@bot.command(name='translate', aliases=['tr', 't'])
async def translate_command(ctx, lang: str = None, *, text: str = None):
    if not lang or not text:
        await ctx.send(
            "❌ **Usage:** `!translate <language> <text>`\n"
            "**Example:** `!translate vi Hello world`"
        )
        return
    
    if len(text) > 3000:
        await ctx.send("❌ Text too long! (Max 3000 chars)")
        return
    
    # Rate limit check
    if not user_rate_limiter.check(ctx.author.id):
        wait_time = user_rate_limiter.get_wait_time(ctx.author.id)
        await ctx.send(f"⏳ Slow down! Wait {wait_time:.1f}s")
        return
    
    # Check cache
    cached = translation_cache.get(text, lang)
    
    if cached:
        result = cached
        logger.info(f"💾 Cache hit: {lang}")
    else:
        async with ctx.typing():
            result = await translate_text(text, lang)
        
        if not result:
            await ctx.send("❌ Translation failed!")
            return
        
        translation_cache.set(text, lang, result)
    
    embed = discord.Embed(
        title=f"🌐 Translation → {lang.upper()}",
        description=result['text'],
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    
    if len(text) <= 400:
        embed.add_field(name="📝 Original", value=f"```{text}```", inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='stats', aliases=['metrics'])
async def stats_command(ctx):
    """Show bot statistics"""
    cache_stats = translation_cache.get_stats()
    queue_stats = translation_queue.get_stats()
    
    embed = discord.Embed(
        title="📊 Bot Statistics",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    
    embed.add_field(
        name="🏢 Servers",
        value=f"{len(bot.guilds)} servers\n{sum(g.member_count for g in bot.guilds)} users",
        inline=True
    )
    
    embed.add_field(
        name="💾 Cache",
        value=f"{cache_stats['size']}/{translation_cache.max_size}\nHit rate: {cache_stats['hit_rate']}",
        inline=True
    )
    
    embed.add_field(
        name="📦 Queue",
        value=f"Waiting: {queue_stats['queue_size']}\nProcessing: {queue_stats['processing']}",
        inline=True
    )
    
    embed.add_field(
        name="✅ Processed",
        value=f"{queue_stats['processed']} requests",
        inline=True
    )
    
    embed.add_field(
        name="❌ Failed",
        value=f"{queue_stats['failed']} requests",
        inline=True
    )
    
    settings = get_server_settings(ctx.guild.id)
    embed.add_field(
        name="📝 This Server",
        value=f"{settings['total_translations']} translations",
        inline=True
    )
    
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

@bot.command(name='flags', aliases=['languages'])
async def flags_list(ctx):
    embed = discord.Embed(
        title=f"🌍 Supported Flags ({len(FLAG_TO_LANG)} languages)",
        description="React with flag to translate messages!",
        color=discord.Color.purple()
    )
    
    flags = list(FLAG_TO_LANG.items())
    col_size = len(flags) // 3
    
    col1 = "\n".join([f"{e} `{c}`" for e, c in flags[:col_size]])
    col2 = "\n".join([f"{e} `{c}`" for e, c in flags[col_size:col_size*2]])
    col3 = "\n".join([f"{e} `{c}`" for e, c in flags[col_size*2:]])
    
    if col1: embed.add_field(name="1️⃣", value=col1, inline=True)
    if col2: embed.add_field(name="2️⃣", value=col2, inline=True)
    if col3: embed.add_field(name="3️⃣", value=col3, inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='help', aliases=['h'])
async def help_command(ctx):
    embed = discord.Embed(
        title="🤖 Translation Bot Help",
        description=f"Support {len(FLAG_TO_LANG)} languages with advanced caching!",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🌐 Auto Translation",
        value="React with flag emoji (🇻🇳 🇺🇸 🇯🇵...) to translate!",
        inline=False
    )
    
    embed.add_field(
        name="📝 Commands",
        value=(
            "`!translate <code> <text>` - Manual translate\n"
            "`!flags` - List all flags\n"
            "`!stats` - View bot statistics\n"
            "`!autodelete on/off` - Toggle auto-delete\n"
            "`!deletetime <sec>` - Set delete timer\n"
            "`!settings` - View server settings"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚡ Rate Limits",
        value="10 requests/minute per user\n100 requests/minute per server",
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
    
    embed.add_field(name="🗑️ Auto-delete", 
                    value="✅ ON" if settings["auto_delete"] else "❌ OFF", 
                    inline=True)
    embed.add_field(name="⏱️ Delete time", 
                    value=f"{settings['delete_time']}s", 
                    inline=True)
    embed.add_field(name="📊 Translations", 
                    value=f"{settings['total_translations']}", 
                    inline=True)
    
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument! Use `!help`")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ No permission! (Need: Manage Messages)")
    else:
        logger.error(f"Command error: {error}")

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
        logger.error("=" * 70)
        logger.error("❌ DISCORD_TOKEN not found!")
        logger.error("Setup:")
        logger.error("1. Replit: Add to Secrets")
        logger.error("2. Render: Add to Environment Variables")
        logger.error("3. Local: export DISCORD_TOKEN='your_token'")
        logger.error("=" * 70)
        exit(1)
    
    logger.info("🚀 Starting Discord Translation Bot...")
    logger.info("🌐 Using LibreTranslate API (multi-instance fallback)")
    logger.info("⚡ Production mode: Queue + Cache + Rate limiting")
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid token!")
    except discord.HTTPException as e:
        if e.status == 429:
            logger.error("❌ Rate limited by Discord - wait 10 minutes before restarting")
            logger.error("💡 Solution: Stop restarting so frequently on Render")
        else:
            logger.error(f"❌ HTTP Error: {e}")
    except Exception as e:
        logger.error(f"❌ Error: {e}")
