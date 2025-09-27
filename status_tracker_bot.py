import os
import json
import logging
import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# --- Load .env file ---
load_dotenv()

# --- Config ---
DATA_FILE = "status_tracker_data.json"
PREFIX = "!"
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set")
# ---------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("status-tracker")

# -----------------------
# Persistence helpers
# -----------------------
def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

db = load_data()

# -----------------------
# Bot setup
# -----------------------
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
tree = bot.tree

# -----------------------
# Utility helpers
# -----------------------
def ensure_guild_record(gid: str):
    if gid not in db:
        db[gid] = {"announce_channel": None, "tracked": {}}
        save_data(db)
    return db[gid]

def parse_user_id(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw.replace("<@", "").replace(">", "").replace("!", "")
    try:
        int(raw)
        return raw
    except ValueError:
        return None

def is_guild_admin(ctx_or_interaction) -> bool:
    if isinstance(ctx_or_interaction, discord.Interaction):
        perms = ctx_or_interaction.user.guild_permissions
    else:
        perms = ctx_or_interaction.author.guild_permissions
    return bool(perms.manage_guild or perms.administrator)

async def announce_status(guild: discord.Guild, user_id_str: str, status: str):
    rec = db.get(str(guild.id))
    if not rec:
        return
    channel_id = rec.get("announce_channel")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        rec["announce_channel"] = None
        save_data(db)
        return

    unix_ts = int(time.time())
    msg = f":bell: User <@{user_id_str}> is now **{status.upper()}** (<t:{unix_ts}:R>)"
    try:
        await channel.send(msg)
    except Exception:
        logger.exception("Failed to send announce message")

# -----------------------
# Events
# -----------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception:
        logger.exception("Failed to sync app commands")

    activity = discord.Game(name="Tracking statuses")
    await bot.change_presence(status=discord.Status.online, activity=activity)

    logger.info(f"Bot ready: {bot.user} (id: {bot.user.id})")

@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.bot or not after.guild:
        return

    gid = str(after.guild.id)
    rec = db.get(gid)
    if not rec:
        return

    tracked = rec.get("tracked", {})
    user_id_str = str(after.id)
    if user_id_str not in tracked:
        return

    prev_status = tracked.get(user_id_str)
    curr_status = str(after.status)

    if prev_status == curr_status:
        return

    if curr_status not in ["online", "offline", "idle", "dnd"]:
        return

    rec["tracked"][user_id_str] = curr_status
    save_data(db)
    await announce_status(after.guild, user_id_str, curr_status)

# -----------------------
# Shared help text
# -----------------------
HELP_TEXT = (
    "**Status Tracker Bot commands**\n"
    "!ping | /ping\n"
    "!setstatuschannel #channel | /setstatuschannel\n"
    "!trackstatus <user_id> | /trackstatus\n"
    "!untrackstatus <user_id> | /untrackstatus\n"
    "!listtracked | /listtracked\n"
    "!laststatus <user_id> | /laststatus\n"
)

# -----------------------
# Prefix Commands
# -----------------------
@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send("✅ Bot is running!")

@bot.command(name="help")
async def _help(ctx: commands.Context):
    await ctx.send(HELP_TEXT)

@bot.command(name="setstatuschannel")
async def setstatuschannel_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not is_guild_admin(ctx):
        await ctx.send("You need Manage Server permissions.")
        return
    gid = str(ctx.guild.id)
    rec = ensure_guild_record(gid)
    rec["announce_channel"] = channel.id
    save_data(db)
    await ctx.send(f"Status announcements will be posted in {channel.mention}.")

@bot.command(name="trackstatus")
async def trackstatus_cmd(ctx: commands.Context, user_id: str):
    if not is_guild_admin(ctx):
        await ctx.send("You need Manage Server permissions.")
        return
    parsed = parse_user_id(user_id)
    if not parsed:
        await ctx.send("Please provide a valid user ID or mention.")
        return
    gid = str(ctx.guild.id)
    rec = ensure_guild_record(gid)
    member = ctx.guild.get_member(int(parsed))
    current_status = str(member.status) if member else "unknown"
    rec["tracked"][parsed] = current_status
    save_data(db)
    await ctx.send(f"Now tracking user ID `{parsed}` (current status: **{current_status}**).")
    if current_status in ["online", "offline", "idle", "dnd"]:
        await announce_status(ctx.guild, parsed, current_status)

@bot.command(name="untrackstatus")
async def untrackstatus_cmd(ctx: commands.Context, user_id: str):
    if not is_guild_admin(ctx):
        await ctx.send("You need Manage Server permissions.")
        return
    parsed = parse_user_id(user_id)
    if not parsed:
        await ctx.send("Please provide a valid user ID or mention.")
        return
    gid = str(ctx.guild.id)
    rec = ensure_guild_record(gid)
    if parsed in rec.get("tracked", {}):
        rec["tracked"].pop(parsed, None)
        save_data(db)
        await ctx.send(f"Stopped tracking `{parsed}`.")
    else:
        await ctx.send("That user is not tracked in this server.")

@bot.command(name="listtracked")
async def listtracked_cmd(ctx: commands.Context):
    gid = str(ctx.guild.id)
    rec = db.get(gid)
    if not rec or not rec.get("tracked"):
        await ctx.send("No users are tracked in this server.")
        return
    lines = [f"<@{uid}> — last recorded: {last or 'unknown'}" for uid, last in rec["tracked"].items()]
    await ctx.send("\n".join(lines))

@bot.command(name="laststatus")
async def laststatus_cmd(ctx: commands.Context, user_id: str):
    parsed = parse_user_id(user_id)
    if not parsed:
        await ctx.send("Please provide a valid user ID or mention.")
        return
    gid = str(ctx.guild.id)
    rec = db.get(gid)
    last = rec.get("tracked", {}).get(parsed) if rec else None
    if not last:
        await ctx.send("No recorded status for that user.")
        return
    await ctx.send(f"User <@{parsed}> last recorded as **{last}**.")

# -----------------------
# Slash Commands
# -----------------------
@tree.command(name="ping", description="Check if the bot is running")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Bot is running ✅", ephemeral=True)

@tree.command(name="help", description="Show help for status tracker bot")
async def _help_slash(interaction: discord.Interaction):
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)

@tree.command(name="setstatuschannel", description="Set the channel for status announcements")
async def setstatuschannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_guild_admin(interaction):
        await interaction.response.send_message("You need Manage Server permissions.", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    rec = ensure_guild_record(gid)
    rec["announce_channel"] = channel.id
    save_data(db)
    await interaction.response.send_message(f"Status announcements will be posted in {channel.mention}.")

@tree.command(name="trackstatus", description="Track a user's status changes")
async def trackstatus_slash(interaction: discord.Interaction, user_id: str):
    if not is_guild_admin(interaction):
        await interaction.response.send_message("You need Manage Server permissions.", ephemeral=True)
        return
    parsed = parse_user_id(user_id)
    if not parsed:
        await interaction.response.send_message("Please provide a valid user ID or mention.", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    rec = ensure_guild_record(gid)
    member = interaction.guild.get_member(int(parsed))
    current_status = str(member.status) if member else "unknown"
    rec["tracked"][parsed] = current_status
    save_data(db)
    await interaction.response.send_message(f"Now tracking user ID `{parsed}` (current status: **{current_status}**).")
    if current_status in ["online", "offline", "idle", "dnd"]:
        await announce_status(interaction.guild, parsed, current_status)

@tree.command(name="untrackstatus", description="Stop tracking a user's status changes")
async def untrackstatus_slash(interaction: discord.Interaction, user_id: str):
    if not is_guild_admin(interaction):
        await interaction.response.send_message("You need Manage Server permissions.", ephemeral=True)
        return
    parsed = parse_user_id(user_id)
    if not parsed:
        await interaction.response.send_message("Please provide a valid user ID or mention.", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    rec = ensure_guild_record(gid)
    if parsed in rec.get("tracked", {}):
        rec["tracked"].pop(parsed, None)
        save_data(db)
        await interaction.response.send_message(f"Stopped tracking `{parsed}`.")
    else:
        await interaction.response.send_message("That user is not tracked in this server.", ephemeral=True)

@tree.command(name="listtracked", description="List all tracked users")
async def listtracked_slash(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    rec = db.get(gid)
    if not rec or not rec.get("tracked"):
        await interaction.response.send_message("No users are tracked in this server.")
        return
    lines = [f"<@{uid}> — last recorded: {last or 'unknown'}" for uid, last in rec["tracked"].items()]
    await interaction.response.send_message("\n".join(lines))

@tree.command(name="laststatus", description="Show last recorded status for a user")
async def laststatus_slash(interaction: discord.Interaction, user_id: str):
    parsed = parse_user_id(user_id)
    if not parsed:
        await interaction.response.send_message("Please provide a valid user ID or mention.", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    rec = db.get(gid)
    last = rec.get("tracked", {}).get(parsed) if rec else None
    if not last:
        await interaction.response.send_message("No recorded status for that user.")
        return
    await interaction.response.send_message(f"User <@{parsed}> last recorded as **{last}**.")

# -----------------------
# Bot startup
# -----------------------
async def _start_bot():
    try:
        await bot.start(TOKEN)
    except asyncio.CancelledError:
        raise
    finally:
        if not bot.is_closed():
            await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(_start_bot())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received; shutting down")
        try:
            asyncio.run(bot.close())
        except Exception:
            pass
