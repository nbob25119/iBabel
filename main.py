import discord 
from discord.ext import commands
from flask import Flask
import asyncio
import aiohttp
import os
from datetime import datetime

# ===============================================
# IMPORT KEEP-ALIVE (cho Replit/Render/Glitch)
# ===============================================
try:
    from keep_alive import keep_alive
    KEEP_ALIVE_AVAILABLE = True
except ImportError:
    KEEP_ALIVE_AVAILABLE = False
    print("⚠️ keep_alive.py not found - bot sẽ không auto-restart")

# ===============================================
# CẤU HÌNH BOT
# ===============================================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ===============================================
# API DỊCH - LIBRETRANSLATE
# ===============================================
# Option 1: Public API (miễn phí, có rate limit)
TRANSLATE_API = "https://libretranslate.com/translate"

# Option 2: Fallback APIs (nếu API chính bị down)
FALLBACK_APIS = [
    "https://libretranslate.com/translate",
    "https://translate.argosopentech.com/translate",
    "https://translate.terraprint.co/translate"
]

# Session cho async HTTP requests
session = None

# ===============================================
# DATABASE CÀI ĐẶT SERVER
# ===============================================
server_settings = {}

def get_server_settings(guild_id):
    """Lấy/tạo cài đặt cho server"""
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "auto_delete": True,
            "delete_time": 30,
            "total_translations": 0
        }
    return server_settings[guild_id]

# ===============================================
# MAPPING FLAG → LANGUAGE CODE (70+ ngôn ngữ)
# ===============================================
FLAG_TO_LANG = {
    # Châu Á
    '🇻🇳': 'vi', '🇨🇳': 'zh', '🇯🇵': 'ja', '🇰🇷': 'ko', '🇹🇭': 'th',
    '🇮🇩': 'id', '🇵🇭': 'tl', '🇲🇾': 'ms', '🇸🇬': 'en', '🇮🇳': 'hi',
    '🇵🇰': 'ur', '🇧🇩': 'bn', '🇱🇰': 'si', '🇲🇲': 'my', '🇰🇭': 'km',
    '🇱🇦': 'lo', '🇹🇼': 'zh', '🇭🇰': 'zh', '🇲🇴': 'zh',
    
    # Châu Âu
    '🇬🇧': 'en', '🇺🇸': 'en', '🇫🇷': 'fr', '🇩🇪': 'de', '🇪🇸': 'es',
    '🇮🇹': 'it', '🇵🇹': 'pt', '🇷🇺': 'ru', '🇵🇱': 'pl', '🇳🇱': 'nl',
    '🇸🇪': 'sv', '🇳🇴': 'no', '🇩🇰': 'da', '🇫🇮': 'fi', '🇬🇷': 'el',
    '🇹🇷': 'tr', '🇨🇿': 'cs', '🇭🇺': 'hu', '🇷🇴': 'ro', '🇧🇬': 'bg',
    '🇭🇷': 'hr', '🇸🇰': 'sk', '🇺🇦': 'uk',
    
    # Châu Mỹ
    '🇧🇷': 'pt', '🇲🇽': 'es', '🇦🇷': 'es', '🇨🇱': 'es', '🇨🇴': 'es',
    '🇵🇪': 'es', '🇨🇦': 'en',
    
    # Trung Đông
    '🇸🇦': 'ar', '🇦🇪': 'ar', '🇮🇷': 'fa', '🇮🇱': 'he', '🇪🇬': 'ar',
    
    # Châu Phi
    '🇿🇦': 'af', '🇳🇬': 'en', '🇰🇪': 'sw',
    
    # Châu Đại Dương
    '🇦🇺': 'en', '🇳🇿': 'en',
}

# ===============================================
# HÀM DỊCH VĂN BẢN (CÓ FALLBACK)
# ===============================================
async def translate_text(text: str, target_lang: str, source_lang: str = 'auto'):
    """
    Dịch văn bản với fallback API
    Tự động thử API khác nếu API chính fail
    """
    global session
    
    if session is None:
        session = aiohttp.ClientSession()
    
    payload = {
        "q": text,
        "source": source_lang,
        "target": target_lang,
        "format": "text"
    }
    
    # Thử từng API cho đến khi thành công
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
            print(f"⚠️ API {api_url} failed: {e}")
            continue
    
    # Tất cả APIs đều fail
    return None

# ===============================================
# EVENT: BOT READY
# ===============================================
@bot.event
async def on_ready():
    print('=' * 70)
    print(f'✅ Bot: {bot.user.name} ONLINE!')
    print(f'🆔 ID: {bot.user.id}')
    print(f'📊 Servers: {len(bot.guilds)}')
    print(f'👥 Users: {sum(g.member_count for g in bot.guilds)}')
    print(f'🌍 Languages: {len(FLAG_TO_LANG)} flags')
    print(f'🔧 API: LibreTranslate (Multi-instance)')
    if KEEP_ALIVE_AVAILABLE:
        print('✅ Keep-Alive: ENABLED')
    print('=' * 70)
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(FLAG_TO_LANG)} flags 🌐 | React to translate!"
        )
    )

# ===============================================
# EVENT: REACTION ADD (DỊCH TỰ ĐỘNG)
# ===============================================
@bot.event
async def on_reaction_add(reaction, user):
    """Xử lý khi user react emoji flag vào tin nhắn"""
    
    if user.bot:
        return
    
    emoji = str(reaction.emoji)
    
    if emoji not in FLAG_TO_LANG:
        return
    
    message = reaction.message
    
    if not message.content or message.content.strip() == "":
        await message.channel.send(
            f"❌ {user.mention} Tin nhắn trống không thể dịch!",
            delete_after=5
        )
        return
    
    if len(message.content) > 3000:
        await message.channel.send(
            f"❌ {user.mention} Tin nhắn quá dài! (Tối đa 3000 ký tự)",
            delete_after=5
        )
        return
    
    settings = get_server_settings(message.guild.id)
    
    async with message.channel.typing():
        target_lang = FLAG_TO_LANG[emoji]
        result = await translate_text(message.content, target_lang)
        
        if not result:
            await message.channel.send(
                f"❌ {user.mention} Lỗi dịch! Tất cả APIs đang bận.",
                delete_after=5
            )
            return
        
        embed = discord.Embed(
            title=f"🌐 {emoji} Translation → {target_lang.upper()}",
            description=result['text'],
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        if len(message.content) <= 400:
            embed.add_field(
                name="📝 Original",
                value=f"```{message.content}```",
                inline=False
            )
        
        footer_text = f"Requested by {user.name}"
        if settings["auto_delete"]:
            footer_text += f" • Deletes in {settings['delete_time']}s"
        
        embed.set_footer(
            text=footer_text,
            icon_url=user.display_avatar.url
        )
        
        translation_msg = await message.channel.send(
            f"💬 {user.mention}",
            embed=embed
        )
        
        settings["total_translations"] += 1
        
        if settings["auto_delete"]:
            await asyncio.sleep(settings["delete_time"])
            try:
                await translation_msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                print("⚠️ Không có quyền xóa tin nhắn")

# ===============================================
# COMMANDS (giữ nguyên như bản cũ)
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
        await ctx.send("❌ Văn bản quá dài! (Max 3000 ký tự)")
        return
    
    async with ctx.typing():
        result = await translate_text(text, lang)
        
        if not result:
            await ctx.send("❌ Lỗi dịch!")
            return
        
        embed = discord.Embed(
            title=f"🌐 Translation → {lang.upper()}",
            description=result['text'],
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        
        if len(text) <= 400:
            embed.add_field(name="📝 Original", value=f"```{text}```", inline=False)
        
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
        description="React với flag để dịch tin nhắn!",
        color=discord.Color.purple()
    )
    
    # Chia thành 3 cột
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
        description=f"Support {len(FLAG_TO_LANG)} languages!",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🌐 Auto Translation",
        value="React with flag emoji (🇻🇳 🇺🇸 🇯🇵...) to translate!",
        inline=False
    )
    
    embed.add_field(
        name="Commands",
        value=(
            "`!translate <code> <text>` - Manual translate\n"
            "`!flags` - List all flags\n"
            "`!autodelete on/off` - Toggle auto-delete\n"
            "`!deletetime <sec>` - Set delete timer\n"
            "`!settings` - View settings"
        ),
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
        print(f"Error: {error}")

@bot.event
async def on_close():
    global session
    if session:
        await session.close()

# ===============================================
# MAIN - CHẠY BOT
# ===============================================
if __name__ == "__main__":
    # Bật keep-alive nếu có (cho Replit/Render)
    if KEEP_ALIVE_AVAILABLE:
        keep_alive()
    
    # Lấy token
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        print("=" * 70)
        print("❌ DISCORD_TOKEN not found!")
        print("Setup:")
        print("1. Replit: Add to Secrets")
        print("2. Render: Add to Environment Variables")
        print("3. Local: export DISCORD_TOKEN='your_token'")
        print("=" * 70)
        exit(1)
    
    print("🚀 Starting Discord Translation Bot...")
    print("🌐 Using LibreTranslate API (multi-instance fallback)")
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid token!")
    except Exception as e:
        print(f"❌ Error: {e}")
