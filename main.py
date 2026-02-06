import discord
from discord.ext import commands
import asyncio
import aiohttp
import os
from datetime import datetime

# IMPORT KEEP-ALIVE
try:
    from keep_alive import keep_alive
    KEEP_ALIVE_AVAILABLE = True
except ImportError:
    KEEP_ALIVE_AVAILABLE = False

# BOT CONFIG (TẮT help command mặc định)
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# TRANSLATION API
FALLBACK_APIS = [
    "https://libretranslate.com/translate",
    "https://translate.argosopentech.com/translate",
    "https://translate.terraprint.co/translate"
]
session = None

# SERVER SETTINGS
server_settings = {}

def get_server_settings(guild_id):
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "auto_delete": True,
            "delete_time": 30,
            "total_translations": 0
        }
    return server_settings[guild_id]

# FLAG TO LANGUAGE MAPPING
FLAG_TO_LANG = {
    '🇻🇳': 'vi', '🇨🇳': 'zh', '🇯🇵': 'ja', '🇰🇷': 'ko', '🇹🇭': 'th',
    '🇮🇩': 'id', '🇵🇭': 'tl', '🇲🇾': 'ms', '🇸🇬': 'en', '🇮🇳': 'hi',
    '🇵🇰': 'ur', '🇧🇩': 'bn', '🇱🇰': 'si', '🇲🇲': 'my', '🇰🇭': 'km',
    '🇱🇦': 'lo', '🇹🇼': 'zh', '🇭🇰': 'zh', '🇲🇴': 'zh',
    '🇬🇧': 'en', '🇺🇸': 'en', '🇫🇷': 'fr', '🇩🇪': 'de', '🇪🇸': 'es',
    '🇮🇹': 'it', '🇵🇹': 'pt', '🇷🇺': 'ru', '🇵🇱': 'pl', '🇳🇱': 'nl',
    '🇸🇪': 'sv', '🇳🇴': 'no', '🇩🇰': 'da', '🇫🇮': 'fi', '🇬🇷': 'el',
    '🇹🇷': 'tr', '🇨🇿': 'cs', '🇭🇺': 'hu', '🇷🇴': 'ro', '🇧🇬': 'bg',
    '🇭🇷': 'hr', '🇸🇰': 'sk', '🇺🇦': 'uk', '🇧🇷': 'pt', '🇲🇽': 'es',
    '🇦🇷': 'es', '🇨🇱': 'es', '🇨🇴': 'es', '🇵🇪': 'es', '🇨🇦': 'en',
    '🇸🇦': 'ar', '🇦🇪': 'ar', '🇮🇷': 'fa', '🇮🇱': 'he', '🇪🇬': 'ar',
    '🇿🇦': 'af', '🇳🇬': 'en', '🇰🇪': 'sw', '🇦🇺': 'en', '🇳🇿': 'en',
}

# TRANSLATE FUNCTION
async def translate_text(text, target_lang, source_lang='auto'):
    global session
    if session is None:
        session = aiohttp.ClientSession()
    
    payload = {"q": text, "source": source_lang, "target": target_lang, "format": "text"}
    
    for api_url in FALLBACK_APIS:
        try:
            async with session.post(api_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    return {'text': data.get('translatedText', text), 'source': source_lang}
        except Exception as e:
            print(f"API {api_url} failed: {e}")
            continue
    return None

# EVENT: BOT READY
@bot.event
async def on_ready():
    print('=' * 70)
    print(f'✅ Bot: {bot.user.name} ONLINE!')
    print(f'🆔 ID: {bot.user.id}')
    print(f'📊 Servers: {len(bot.guilds)}')
    print(f'👥 Users: {sum(g.member_count for g in bot.guilds)}')
    print(f'🌍 Languages: {len(FLAG_TO_LANG)}')
    if KEEP_ALIVE_AVAILABLE:
        print('✅ Keep-Alive: ENABLED')
    print('=' * 70)
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(FLAG_TO_LANG)} flags 🌐 | !help"
        )
    )

# EVENT: REACTION ADD
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    
    emoji = str(reaction.emoji)
    if emoji not in FLAG_TO_LANG:
        return
    
    message = reaction.message
    if not message.content or message.content.strip() == "":
        await message.channel.send(f"❌ {user.mention} Empty message!", delete_after=5)
        return
    
    if len(message.content) > 3000:
        await message.channel.send(f"❌ {user.mention} Message too long!", delete_after=5)
        return
    
    settings = get_server_settings(message.guild.id)
    
    async with message.channel.typing():
        target_lang = FLAG_TO_LANG[emoji]
        result = await translate_text(message.content, target_lang)
        
        if not result:
            await message.channel.send(f"❌ {user.mention} Translation failed!", delete_after=5)
            return
        
        embed = discord.Embed(
            title=f"🌐 {emoji} Translation → {target_lang.upper()}",
            description=result['text'],
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        if len(message.content) <= 400:
            embed.add_field(name="📝 Original", value=f"```{message.content}```", inline=False)
        
        footer_text = f"Requested by {user.name}"
        if settings["auto_delete"]:
            footer_text += f" • Deletes in {settings['delete_time']}s"
        
        embed.set_footer(text=footer_text, icon_url=user.display_avatar.url)
        
        translation_msg = await message.channel.send(f"💬 {user.mention}", embed=embed)
        settings["total_translations"] += 1
        
        if settings["auto_delete"]:
            await asyncio.sleep(settings["delete_time"])
            try:
                await translation_msg.delete()
            except:
                pass

# COMMANDS
@bot.command(name='translate', aliases=['tr', 't'])
async def translate_command(ctx, lang: str = None, *, text: str = None):
    if not lang or not text:
        await ctx.send("❌ **Usage:** `!translate <lang> <text>`\n**Example:** `!translate vi Hello`")
        return
    
    async with ctx.typing():
        result = await translate_text(text, lang)
        if not result:
            await ctx.send("❌ Translation failed!")
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
    
    if mode.lower() in ['on', 'enable', '1']:
        settings["auto_delete"] = True
        await ctx.send(f"✅ Auto-delete: ON ({settings['delete_time']}s)")
    elif mode.lower() in ['off', 'disable', '0']:
        settings["auto_delete"] = False
        await ctx.send("✅ Auto-delete: OFF")

@bot.command(name='deletetime', aliases=['dt'])
@commands.has_permissions(manage_messages=True)
async def delete_time(ctx, seconds: int = None):
    settings = get_server_settings(ctx.guild.id)
    
    if seconds is None:
        await ctx.send(f"⏱️ **Current:** {settings['delete_time']}s")
        return
    
    if 5 <= seconds <= 600:
        settings["delete_time"] = seconds
        await ctx.send(f"✅ Delete time: {seconds}s")
    else:
        await ctx.send("❌ Range: 5-600 seconds")

@bot.command(name='flags', aliases=['languages'])
async def flags_list(ctx):
    embed = discord.Embed(
        title=f"🌍 Supported Flags ({len(FLAG_TO_LANG)} languages)",
        description="React with flag emoji to translate!",
        color=discord.Color.purple()
    )
    
    flags = list(FLAG_TO_LANG.items())
    col_size = len(flags) // 3
    
    for i in range(3):
        start = i * col_size
        end = start + col_size if i < 2 else len(flags)
        col = flags[start:end]
        if col:
            value = "\n".join([f"{e} `{c}`" for e, c in col])
            embed.add_field(name=f"Col {i+1}", value=value, inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='settings')
async def view_settings(ctx):
    settings = get_server_settings(ctx.guild.id)
    
    embed = discord.Embed(title="⚙️ Server Settings", color=discord.Color.gold())
    embed.add_field(name="🗑️ Auto-delete", value="✅ ON" if settings["auto_delete"] else "❌ OFF", inline=True)
    embed.add_field(name="⏱️ Delete time", value=f"{settings['delete_time']}s", inline=True)
    embed.add_field(name="📊 Translations", value=str(settings['total_translations']), inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='help', aliases=['h'])
async def help_command(ctx):
    settings = get_server_settings(ctx.guild.id)
    
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
        name="⌨️ Commands",
        value=(
            "`!translate <lang> <text>` - Manual translate\n"
            "`!flags` - List all flags\n"
            "`!autodelete on/off` - Toggle auto-delete\n"
            "`!deletetime <sec>` - Set delete time\n"
            "`!settings` - View settings"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚙️ Current Settings",
        value=(
            f"**Auto-delete:** {'✅ ON' if settings['auto_delete'] else '❌ OFF'}\n"
            f"**Delete after:** {settings['delete_time']}s\n"
            f"**Translations:** {settings['total_translations']}"
        ),
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument! Use `!help`")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No permission! (Need: Manage Messages)")
    else:
        print(f"Error: {error}")

@bot.event
async def on_close():
    global session
    if session:
        await session.close()

# MAIN
if __name__ == "__main__":
    if KEEP_ALIVE_AVAILABLE:
        keep_alive()
    
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        print("=" * 70)
        print("❌ DISCORD_TOKEN not found!")
        print("Add DISCORD_TOKEN to Environment Variables")
        print("=" * 70)
        exit(1)
    
    print("🚀 Starting bot...")
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid token!")
    except Exception as e:
        print(f"❌ Error: {e}")
