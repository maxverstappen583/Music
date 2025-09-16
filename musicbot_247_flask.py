# music_bot.py
import os
import asyncio
import math
import textwrap
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

import discord
from discord.ext import commands, tasks
import wavelink
import aiohttp
from aiohttp import ClientSession
from dotenv import load_dotenv
import aiosqlite
from flask import Flask, request, render_template_string, redirect
import threading
import lyricsgenius
import yt_dlp

# -------------------------
# Load environment & defaults
# -------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", 2333))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").lower() == "true"
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "secret")
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "!")
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "50"))
DEFAULT_AUTOPLAY = os.getenv("DEFAULT_AUTOPLAY", "false").lower() == "true"
DEFAULT_LOOP = os.getenv("DEFAULT_LOOP", "off")  # off | track | queue
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

DB_PATH = "guild_settings.db"

# -------------------------
# Enums & dataclasses
# -------------------------
class LoopMode(str, Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

@dataclass
class GuildSettings:
    guild_id: int
    prefix: str = DEFAULT_PREFIX
    default_volume: int = DEFAULT_VOLUME
    autoplay: bool = DEFAULT_AUTOPLAY
    loop_mode: LoopMode = field(default_factory=lambda: LoopMode(DEFAULT_LOOP))

@dataclass
class QueueItem:
    title: str
    uri: str
    length: int  # seconds
    requester: discord.Member
    thumbnail: Optional[str] = None
    source: Optional[str] = None  # youtube/spotify/soundcloud/etc

# -------------------------
# Database helpers
# -------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id INTEGER PRIMARY KEY,
            prefix TEXT,
            default_volume INTEGER,
            autoplay INTEGER,
            loop_mode TEXT
        )
        """)
        await db.commit()

async def get_guild_settings(guild_id: int) -> GuildSettings:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT prefix, default_volume, autoplay, loop_mode FROM guilds WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        if row:
            prefix, vol, autoplay, loop_mode = row
            return GuildSettings(guild_id, prefix, vol, bool(autoplay), LoopMode(loop_mode))
        else:
            # create defaults
            gs = GuildSettings(guild_id)
            await db.execute("INSERT OR REPLACE INTO guilds (guild_id, prefix, default_volume, autoplay, loop_mode) VALUES (?, ?, ?, ?, ?)",
                             (guild_id, gs.prefix, gs.default_volume, int(gs.autoplay), gs.loop_mode.value))
            await db.commit()
            return gs

async def set_guild_setting(guild_id: int, **kwargs):
    gs = await get_guild_settings(guild_id)  # ensure row exists
    prefix = kwargs.get("prefix", gs.prefix)
    default_volume = kwargs.get("default_volume", gs.default_volume)
    autoplay = kwargs.get("autoplay", gs.autoplay)
    loop_mode = kwargs.get("loop_mode", gs.loop_mode.value if isinstance(gs.loop_mode, LoopMode) else gs.loop_mode)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE guilds SET prefix = ?, default_volume = ?, autoplay = ?, loop_mode = ? WHERE guild_id = ?",
                         (prefix, default_volume, int(autoplay), loop_mode, guild_id))
        await db.commit()

# -------------------------
# Music player state per guild
# -------------------------
class GuildPlayer:
    def __init__(self, guild: discord.Guild, node: wavelink.Node):
        self.guild = guild
        self.node = node
        self.queue: List[QueueItem] = []
        self.current: Optional[QueueItem] = None
        self.loop_mode: LoopMode = LoopMode.OFF
        self.autoplay: bool = False
        self.volume: int = DEFAULT_VOLUME
        self.history: List[discord.Message] = []  # store now-playing panel messages
        self.vc_player: Optional[wavelink.Player] = None

    def enqueue(self, item: QueueItem):
        self.queue.append(item)

    def dequeue(self) -> Optional[QueueItem]:
        if not self.queue:
            return None
        return self.queue.pop(0)

    def clear(self):
        self.queue.clear()
        self.current = None

# -------------------------
# Bot & Wavelink setup
# -------------------------
intents = discord.Intents.all()
# We'll use a minimal commands.Bot so prefix is dynamic per guild
bot = commands.Bot(command_prefix=DEFAULT_PREFIX, intents=intents, help_command=None)

# store players per guild
GUILD_PLAYERS: Dict[int, GuildPlayer] = {}
WAVELINK_NODE: Optional[wavelink.Node] = None

# Genius
genius_client = lyricsgenius.Genius(GENIUS_TOKEN) if GENIUS_TOKEN else None

# yt-dlp for info extraction when needed
ydl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "skip_download": True,
}
ydl = yt_dlp.YoutubeDL(ydl_opts)

# -------------------------
# Utility helpers
# -------------------------
def seconds_to_hhmmss(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"

async def fetch_track_info_from_url(url: str) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    return info

async def search_youtube(query: str) -> Optional[Dict]:
    # Use yt-dlp to search youtube
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch:{query}", download=False))
    entries = info.get("entries")
    if entries:
        return entries[0]
    return None

def ensure_guild_player(guild: discord.Guild) -> GuildPlayer:
    gp = GUILD_PLAYERS.get(guild.id)
    if not gp:
        # pass node later; temporarily create with None and set node on connect
        gp = GuildPlayer(guild, WAVELINK_NODE)
        GUILD_PLAYERS[guild.id] = gp
    return gp

# -------------------------
# Wavelink event handlers & player controls
# -------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    await init_db()
    # Connect wavelink node
    await ensure_node()
    bot.loop.create_task(background_player_state_sync())

async def ensure_node():
    global WAVELINK_NODE
    if WAVELINK_NODE and WAVELINK_NODE.is_available():
        return WAVELINK_NODE
    # connect to wavelink node
    try:
        ws = wavelink.NodePool
        node = wavelink.Node(uri=f"{LAVALINK_HOST}:{LAVALINK_PORT}", password=LAVALINK_PASSWORD, secure=LAVALINK_SECURE)
        await wavelink.NodePool.connect(client=bot, nodes=[node])
        WAVELINK_NODE = node
        print("Connected to Lavalink node.")
        return node
    except Exception as e:
        print("Failed to connect to Lavalink:", e)
        WAVELINK_NODE = None
        return None

@wavelink.NodePool.listener()
async def on_node_ready(node: wavelink.Node):
    print(f"Wavelink node ready: {node}")

@wavelink.Player.listener()
async def on_track_end(player: wavelink.Player, track: wavelink.Track, reason):
    guild_id = player.guild_id
    gp = GUILD_PLAYERS.get(guild_id)
    if not gp:
        return
    # handle loop and autoplay
    if gp.loop_mode == LoopMode.TRACK:
        await player.play(track)  # replay same track
        return
    if gp.loop_mode == LoopMode.QUEUE and gp.current:
        # re-enqueue current at end
        gp.queue.append(gp.current)
    # advance to next
    gp.current = None
    next_item = gp.dequeue()
    if next_item:
        await play_queue_item(player, gp, next_item)
    else:
        # queue empty: handle autoplay if enabled
        if gp.autoplay and gp.current and GENIUS_TOKEN is not None:
            # try to fetch recommended from YouTube (best-effort via search on title)
            # simple approach: search YouTube for artist + track name
            try:
                q = gp.current.title
                info = await search_youtube(q + " audio")
                if info:
                    qi = QueueItem(title=info.get("title"), uri=info.get("webpage_url"), length=int(info.get("duration") or 0),
                                   requester=None, thumbnail=info.get("thumbnail"))
                    await play_queue_item(player, gp, qi)
                    return
            except Exception:
                pass
        # nothing to play: disconnect after a short delay
        await asyncio.sleep(5)
        try:
            await player.disconnect()
        except Exception:
            pass

async def play_queue_item(player: wavelink.Player, gp: GuildPlayer, item: QueueItem):
    gp.current = item
    # use NodePool to get track
    try:
        # prefer loading by url
        tr = await wavelink.YouTubeTrack.search(item.uri, return_first=True) if item.uri.startswith("http") else None
    except Exception:
        tr = None
    if not tr:
        # fallback: search by title
        tr = await wavelink.YouTubeTrack.search(item.title, return_first=True)
    await player.play(tr)
    # send now playing panel
    channel = None
    # try to find the last text channel the bot can send in the guild
    for c in gp.guild.text_channels:
        if c.permissions_for(gp.guild.me).send_messages:
            channel = c
            break
    if channel:
        embed = create_now_playing_embed(gp, item)
        view = ControlPanelView(gp)
        msg = await channel.send(embed=embed, view=view)
        gp.history.append(msg)
        # keep max 50 history messages
        if len(gp.history) > 50:
            gp.history.pop(0)

# -------------------------
# Discord UI: Buttons and Modal
# -------------------------
class VolumeModal(discord.ui.Modal, title="Set Volume (0-100%)"):
    volume = discord.ui.TextInput(label="Volume (0-100)", placeholder="50", required=True, max_length=3)

    def __init__(self, gp: GuildPlayer):
        super().__init__()
        self.gp = gp

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = int(self.volume.value)
            v = max(0, min(100, v))
        except ValueError:
            await interaction.response.send_message("Invalid number.", ephemeral=True)
            return
        self.gp.volume = v
        if self.gp.vc_player:
            await self.gp.vc_player.set_volume(v)
        await interaction.response.send_message(f"Volume set to {v}%", ephemeral=True)

class ControlPanelView(discord.ui.View):
    def __init__(self, gp: GuildPlayer, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.gp = gp

    @discord.ui.button(label="Clear Queue", style=discord.ButtonStyle.primary, custom_id="clear_queue")
    async def clear_queue(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.gp.clear()
        if self.gp.vc_player:
            await self.gp.vc_player.stop()
        await interaction.response.send_message("Queue cleared.", ephemeral=True)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message("Previous track placeholder (not implemented).", ephemeral=True)

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.success, custom_id="pause_resume")
    async def pause_resume(self, button: discord.ui.Button, interaction: discord.Interaction):
        player = self.gp.vc_player
        if not player:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        if player.is_paused():
            await player.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await player.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, custom_id="skip")
    async def skip(self, button: discord.ui.Button, interaction: discord.Interaction):
        player = self.gp.vc_player
        if not player:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        await player.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, custom_id="shuffle")
    async def shuffle(self, button: discord.ui.Button, interaction: discord.Interaction):
        import random
        random.shuffle(self.gp.queue)
        await interaction.response.send_message("Queue shuffled.", ephemeral=True)

    @discord.ui.button(label="Volume", style=discord.ButtonStyle.primary, custom_id="volume")
    async def volume_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        modal = VolumeModal(self.gp)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, custom_id="loop")
    async def loop_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # cycle off -> track -> queue
        m = self.gp.loop_mode
        if m == LoopMode.OFF:
            self.gp.loop_mode = LoopMode.TRACK
        elif m == LoopMode.TRACK:
            self.gp.loop_mode = LoopMode.QUEUE
        else:
            self.gp.loop_mode = LoopMode.OFF
        await interaction.response.send_message(f"Loop mode: {self.gp.loop_mode.value}", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self.gp.vc_player:
            await self.gp.vc_player.disconnect()
        self.gp.clear()
        await interaction.response.send_message("Stopped playback and cleared queue.", ephemeral=True)

# -------------------------
# Embeds & UI helpers
# -------------------------
def create_now_playing_embed(gp: GuildPlayer, item: QueueItem) -> discord.Embed:
    em = discord.Embed(title=item.title, description=f"Requested by: {item.requester.mention if item.requester else 'Unknown'}", color=0x3498db)
    em.add_field(name="Duration", value=seconds_to_hhmmss(item.length))
    em.add_field(name="Queue Length", value=str(len(gp.queue)))
    em.add_field(name="Autoplay", value=str(gp.autoplay))
    em.add_field(name="Loop Mode", value=gp.loop_mode.value)
    em.add_field(name="Volume", value=f"{gp.volume}%")
    if item.thumbnail:
        em.set_thumbnail(url=item.thumbnail)
    return em

# -------------------------
# Commands: play, queue, skip, pause etc.
# -------------------------
@bot.command(name="join")
async def join(ctx: commands.Context):
    if not ctx.author.voice:
        return await ctx.send("You are not connected to a voice channel.")
    channel = ctx.author.voice.channel
    node = await ensure_node()
    gp = ensure_guild_player(ctx.guild)
    # connect player
    player: wavelink.Player = await node.get_player(ctx.guild.id, cls=wavelink.Player, endpoint=LAVALINK_HOST)
    await player.connect(channel.id)
    gp.vc_player = player
    gp.node = node
    settings = await get_guild_settings(ctx.guild.id)
    gp.volume = settings.default_volume
    gp.autoplay = settings.autoplay
    gp.loop_mode = settings.loop_mode
    await player.set_volume(gp.volume)
    await ctx.send(f"Connected to **{channel.name}**.")

@bot.command(name="leave")
async def leave(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    if gp.vc_player:
        await gp.vc_player.disconnect()
    gp.clear()
    await ctx.send("Disconnected and cleared queue.")

@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str):
    """Play from link or search (YouTube, Spotify links supported; Spotify will be searched on YouTube)."""
    if not ctx.author.voice:
        return await ctx.send("You must be connected to a voice channel first.")
    gp = ensure_guild_player(ctx.guild)
    node = await ensure_node()
    # ensure player connected
    player: wavelink.Player
    if gp.vc_player:
        player = gp.vc_player
    else:
        # connect
        player = await node.get_player(ctx.guild.id, cls=wavelink.Player, endpoint=LAVALINK_HOST)
        await player.connect(ctx.author.voice.channel.id)
        gp.vc_player = player
    # detect playlist link
    if "playlist" in query and ("youtube" in query or "list=" in query):
        # try yt-dlp to extract playlist entries
        try:
            info = await fetch_track_info_from_url(query)
            entries = info.get("entries", [])
            for e in entries:
                qi = QueueItem(title=e.get("title"), uri=e.get("webpage_url"), length=int(e.get("duration") or 0),
                               requester=ctx.author, thumbnail=e.get("thumbnail"))
                gp.enqueue(qi)
            await ctx.send(f"Queued playlist with {len(entries)} items.")
            # if nothing playing, start
            if not gp.current:
                next_item = gp.dequeue()
                if next_item:
                    await play_queue_item(player, gp, next_item)
            return
        except Exception as e:
            print("Playlist load error:", e)
    # single link or search
    track_info = None
    try:
        if query.startswith("http"):
            # attempt to get metadata
            info = await fetch_track_info_from_url(query)
            if "entries" in info:
                info = info["entries"][0]
            track_info = info
            url = info.get("webpage_url") or query
            title = info.get("title") or url
            length = int(info.get("duration") or 0)
            thumb = info.get("thumbnail")
        else:
            # search YouTube
            info = await search_youtube(query)
            if info:
                track_info = info
                url = info.get("webpage_url")
                title = info.get("title")
                length = int(info.get("duration") or 0)
                thumb = info.get("thumbnail")
            else:
                return await ctx.send("No results found.")
    except Exception as e:
        print("Search error:", e)
        return await ctx.send("Failed to search for the query.")
    item = QueueItem(title=title, uri=url, length=length, requester=ctx.author, thumbnail=thumb)
    gp.enqueue(item)
    await ctx.send(f"Added **{title}** to the queue.")
    if not gp.current:
        next_item = gp.dequeue()
        if next_item:
            await play_queue_item(player, gp, next_item)

@bot.command(name="queue")
async def show_queue(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    lines = []
    for i, it in enumerate(gp.queue[:10], start=1):
        lines.append(f"**{i}.** {it.title} — `{seconds_to_hhmmss(it.length)}` — requested by {it.requester.mention if it.requester else 'Unknown'}")
    if not lines:
        return await ctx.send("Queue is empty.")
    embed = discord.Embed(title="Queue (next up to 10)", description="\n".join(lines), color=0x2ecc71)
    view = ControlPanelView(gp)
    await ctx.send(embed=embed, view=view)

@bot.command(name="skip")
async def cmd_skip(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    if gp.vc_player:
        await gp.vc_player.stop()
        await ctx.send("Skipped current track.")
    else:
        await ctx.send("Not connected.")

@bot.command(name="pause")
async def cmd_pause(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    if gp.vc_player:
        await gp.vc_player.pause()
        await ctx.send("Paused.")
    else:
        await ctx.send("Not connected.")

@bot.command(name="resume")
async def cmd_resume(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    if gp.vc_player:
        await gp.vc_player.resume()
        await ctx.send("Resumed.")
    else:
        await ctx.send("Not connected.")

@bot.command(name="volume")
async def cmd_volume(ctx: commands.Context, volume: int):
    gp = ensure_guild_player(ctx.guild)
    v = max(0, min(100, volume))
    gp.volume = v
    if gp.vc_player:
        await gp.vc_player.set_volume(v)
    await ctx.send(f"Volume set to {v}%")

@bot.command(name="loop")
async def cmd_loop(ctx: commands.Context, mode: str = None):
    gp = ensure_guild_player(ctx.guild)
    if not mode:
        return await ctx.send(f"Current loop mode: {gp.loop_mode.value}")
    mode = mode.lower()
    if mode not in ("off", "track", "queue"):
        return await ctx.send("Invalid loop mode. Choose off | track | queue.")
    gp.loop_mode = LoopMode(mode)
    await ctx.send(f"Loop mode set to {gp.loop_mode.value}")

@bot.command(name="autoplay")
async def cmd_autoplay(ctx: commands.Context, toggle: Optional[str] = None):
    gp = ensure_guild_player(ctx.guild)
    if toggle is None:
        return await ctx.send(f"Autoplay is {'on' if gp.autoplay else 'off'}.")
    if toggle.lower() in ("on", "true", "1"):
        gp.autoplay = True
    elif toggle.lower() in ("off", "false", "0"):
        gp.autoplay = False
    else:
        return await ctx.send("Use on/off")
    await ctx.send(f"Autoplay set to {gp.autoplay}")

@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    gp = ensure_guild_player(ctx.guild)
    if gp.vc_player:
        await gp.vc_player.disconnect()
    gp.clear()
    await ctx.send("Stopped playback and cleared the queue.")

@bot.command(name="lyrics")
async def cmd_lyrics(ctx: commands.Context, *, query: Optional[str] = None):
    if genius_client is None:
        return await ctx.send("Genius token not configured.")
    gp = ensure_guild_player(ctx.guild)
    if not query:
        if gp.current:
            query = gp.current.title
        else:
            return await ctx.send("No song is playing and no query provided.")
    await ctx.send(f"Searching lyrics for: {query}")
    try:
        song = genius_client.search_song(query)
        if not song or not song.lyrics:
            return await ctx.send("Lyrics not found.")
        lyrics = song.lyrics.strip()
        # split into 1900-char chunks to fit in discord messages
        chunks = [lyrics[i:i+1900] for i in range(0, len(lyrics), 1900)]
        for ch in chunks:
            await ctx.send(f"```\n{ch}\n```")
    except Exception as e:
        print("Lyrics error:", e)
        await ctx.send("Failed to fetch lyrics.")

# -------------------------
# Guild settings commands (prefix + slash)
# -------------------------
@bot.command(name="setprefix")
@commands.has_guild_permissions(administrator=True)
async def set_prefix_cmd(ctx: commands.Context, new_prefix: str):
    await set_guild_setting(ctx.guild.id, prefix=new_prefix)
    await ctx.send(f"Prefix set to `{new_prefix}`")

@bot.command(name="setvolume")
@commands.has_guild_permissions(administrator=True)
async def set_default_volume(ctx: commands.Context, vol: int):
    v = max(0, min(100, vol))
    await set_guild_setting(ctx.guild.id, default_volume=v)
    await ctx.send(f"Default volume for this guild set to {v}%")

@bot.command(name="setautoplay")
@commands.has_guild_permissions(administrator=True)
async def set_autoplay_cmd(ctx: commands.Context, toggle: str):
    t = toggle.lower() in ("on", "true", "1")
    await set_guild_setting(ctx.guild.id, autoplay=t)
    await ctx.send(f"Autoplay for this guild set to {t}")

@bot.command(name="setloop")
@commands.has_guild_permissions(administrator=True)
async def set_loop_cmd(ctx: commands.Context, mode: str):
    if mode.lower() not in ("off", "track", "queue"):
        return await ctx.send("Invalid loop mode (off|track|queue).")
    await set_guild_setting(ctx.guild.id, loop_mode=mode.lower())
    await ctx.send(f"Default loop mode set to {mode.lower()}")

# -------------------------
# Dynamic prefix handling
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return
    gs = await get_guild_settings(message.guild.id)
    prefix = gs.prefix
    # process both prefix and bot mention
    invoked = False
    if message.content.startswith(prefix):
        invoked = True
        ctx_message = message
    elif message.content.startswith(f"<@!{bot.user.id}>") or message.content.startswith(f"<@{bot.user.id}>"):
        invoked = True
        ctx_message = message
    if invoked:
        await bot.process_commands(message)
    # Also allow slash commands (discord handles them)
    # Do not suppress anything else

# -------------------------
# Background tasks
# -------------------------
async def background_player_state_sync():
    # keep guild players' node reference updated once node is available
    while True:
        if WAVELINK_NODE:
            for gp in list(GUILD_PLAYERS.values()):
                gp.node = WAVELINK_NODE
        await asyncio.sleep(10)

# -------------------------
# Flask Dashboard (simple)
# -------------------------
app = Flask("music_bot_dashboard")

DASHBOARD_TEMPLATE = """
<!doctype html>
<title>Music Bot Dashboard</title>
<h2>Music Bot - Guild Settings</h2>
<form method="get">
<input type="hidden" name="secret" value="{{secret}}">
</form>
<a href="/?secret={{secret}}">Refresh</a>
{% for g in guilds %}
<hr>
<h3>Guild ID: {{g.guild_id}}</h3>
<form method="post" action="/update?secret={{secret}}">
<input type="hidden" name="guild_id" value="{{g.guild_id}}">
Prefix: <input name="prefix" value="{{g.prefix}}"><br>
Default Volume: <input name="default_volume" value="{{g.default_volume}}"><br>
Autoplay: <select name="autoplay">
  <option value="1" {% if g.autoplay %}selected{% endif %}>On</option>
  <option value="0" {% if not g.autoplay %}selected{% endif %}>Off</option>
</select><br>
Loop Mode: <select name="loop_mode">
  <option value="off" {% if g.loop_mode == 'off' %}selected{% endif %}>Off</option>
  <option value="track" {% if g.loop_mode == 'track' %}selected{% endif %}>Track</option>
  <option value="queue" {% if g.loop_mode == 'queue' %}selected{% endif %}>Queue</option>
</select><br>
<button type="submit">Update</button>
</form>
{% endfor %}
"""

@app.route("/", methods=["GET"])
def dashboard_index():
    secret = request.args.get("secret", "")
    if secret != ADMIN_SECRET:
        return "Unauthorized. Provide ?secret=YOUR_ADMIN_SECRET", 401
    # read all guilds from DB
    async def load():
        rows = []
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT guild_id, prefix, default_volume, autoplay, loop_mode FROM guilds") as cur:
                async for row in cur:
                    gid, prefix, dv, autoplay, loop_mode = row
                    rows.append({
                        "guild_id": gid,
                        "prefix": prefix,
                        "default_volume": dv,
                        "autoplay": bool(autoplay),
                        "loop_mode": loop_mode
                    })
        return rows
    guilds = asyncio.run(load())
    return render_template_string(DASHBOARD_TEMPLATE, guilds=guilds, secret=secret)

@app.route("/update", methods=["POST"])
def dashboard_update():
    secret = request.args.get("secret", "")
    if secret != ADMIN_SECRET:
        return "Unauthorized.", 401
    guild_id = int(request.form.get("guild_id"))
    prefix = request.form.get("prefix")
    default_volume = int(request.form.get("default_volume"))
    autoplay = bool(int(request.form.get("autoplay")))
    loop_mode = request.form.get("loop_mode")
    asyncio.run(set_guild_setting(guild_id, prefix=prefix, default_volume=default_volume, autoplay=autoplay, loop_mode=loop_mode))
    return redirect(f"/?secret={secret}")

def run_flask():
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)

# -------------------------
# Startup
# -------------------------
def start():
    # start flask in thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    start()
