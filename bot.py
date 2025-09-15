# musicbot_247_flask.py
# Full-featured music bot (prefix + slash) with join/leave, play, queue, controls, lyrics, earrape, 24/7, Flask keepalive.

import os
import discord
from discord.ext import commands
import wavelink
import asyncio
import random
import json
import threading
import lyricsgenius
from flask import Flask, jsonify

# ----------------------------
# Configuration (env)
# ----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or ""
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN") or os.getenv("GENIUS_API_TOKEN", "")
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "localhost")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", os.getenv("LAVALINK_PASS", "youshallnotpass"))
PREFIX = os.getenv("BOT_PREFIX", "?")
FLASK_PORT = int(os.getenv("FLASK_PORT", os.getenv("PORT", "8080")))

EARRAPE_DEFAULT_SECONDS = 8
EARRAPE_MAX_SECONDS = 30
EARRAPE_VOLUME = 400  # best-effort; node may clamp

SETTINGS_FILE = "settings.json"

# ----------------------------
# Intents & bot init
# ----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
genius = lyricsgenius.Genius(GENIUS_TOKEN) if GENIUS_TOKEN else None

# ----------------------------
# Persistent settings helpers
# ----------------------------
def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"guilds": {}}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"guilds": {}}

def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("Failed to save settings:", e)

settings = load_settings()

def ensure_guild_settings(guild_id):
    gid = str(guild_id)
    if gid not in settings["guilds"]:
        settings["guilds"][gid] = {"247": False}
        save_settings(settings)

def is_247_enabled(guild_id):
    gid = str(guild_id)
    return settings.get("guilds", {}).get(gid, {}).get("247", False)

# ----------------------------
# Flask keepalive (for Render)
# ----------------------------
flask_app = Flask("musicbot_keepalive")

@flask_app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "bot": str(bot.user) if bot.is_ready() else "starting"}), 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT)

# ----------------------------
# Utility: message sending (works for Context or Interaction)
# ----------------------------
async def safe_send(dest, content=None, embed=None, view=None, ephemeral=False):
    try:
        if isinstance(dest, commands.Context):
            return await dest.send(content=content, embed=embed, view=view)
        else:
            # Interaction
            if ephemeral:
                return await dest.response.send_message(content=content, embed=embed, view=view, ephemeral=True)
            else:
                if dest.response.is_done():
                    return await dest.followup.send(content=content, embed=embed, view=view)
                else:
                    return await dest.response.send_message(content=content, embed=embed, view=view)
    except Exception:
        try:
            if isinstance(dest, commands.Context):
                await dest.send(content=content, embed=embed)
            else:
                await dest.channel.send(content=content, embed=embed)
        except Exception:
            pass

# ----------------------------
# Lavalink node connect
# ----------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    if not wavelink.NodePool.nodes:
        try:
            await wavelink.NodePool.create_node(
                bot=bot,
                host=LAVALINK_HOST,
                port=LAVALINK_PORT,
                password=LAVALINK_PASSWORD,
                https=False
            )
            print("✅ Connected to Lavalink node.")
        except Exception as e:
            print("❌ Lavalink connect error:", e)
    # sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"🔧 Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync failed:", e)

# ----------------------------
# Connect player helper
# ----------------------------
async def connect_player_for(ctx_or_inter):
    user = ctx_or_inter.author if isinstance(ctx_or_inter, commands.Context) else ctx_or_inter.user
    if not user.voice or not user.voice.channel:
        await safe_send(ctx_or_inter, "❌ You must be in a voice channel first.", ephemeral=isinstance(ctx_or_inter, discord.Interaction))
        return None

    guild = ctx_or_inter.guild
    player: wavelink.Player = guild.voice_client
    if not player:
        try:
            player = await user.voice.channel.connect(cls=wavelink.Player)
        except Exception as e:
            await safe_send(ctx_or_inter, "⚠️ Failed to connect to voice channel.", ephemeral=isinstance(ctx_or_inter, discord.Interaction))
            print("Connect error:", e)
            return None
        player.queue = wavelink.Queue()
        player.loop = False
        player.custom_volume = 100
        player.text_channel = None
        player._disconnect_task = None
        player._earrape_prev_volume = getattr(player, "custom_volume", 100)
    # set text channel to the context channel for now-playing messages
    player.text_channel = ctx_or_inter.channel
    return player

# ----------------------------
# Auto-disconnect scheduler (2 minutes)
# ----------------------------
async def schedule_auto_disconnect(player: wavelink.Player, guild_id: int, delay: int = 120):
    try:
        if getattr(player, "_disconnect_task", None):
            player._disconnect_task.cancel()
    except Exception:
        pass

    async def _task():
        try:
            await asyncio.sleep(delay)
            if player.is_playing() or not player.is_connected():
                return
            if is_247_enabled(guild_id):
                return
            if getattr(player, "queue", None) and not player.queue.is_empty:
                return
            try:
                await player.disconnect()
                ch = getattr(player, "text_channel", None)
                if ch:
                    await ch.send("⏹️ Inactive for 2 minutes — disconnected.")
            except Exception:
                pass
        except asyncio.CancelledError:
            return

    player._disconnect_task = asyncio.create_task(_task())

# ----------------------------
# EARRAPE Modal + Controls
# ----------------------------
class EarrapeConfirmModal(discord.ui.Modal, title="EARRAPE CONFIRMATION (DANGEROUS)"):
    confirm_text = discord.ui.TextInput(label="Type 'I AGREE' to confirm", style=discord.TextStyle.short, required=True, max_length=30, placeholder="I AGREE")
    duration = discord.ui.TextInput(label=f"Duration seconds (max {EARRAPE_MAX_SECONDS})", style=discord.TextStyle.short, required=True, default=str(EARRAPE_DEFAULT_SECONDS))

    def __init__(self, player, requester_id):
        super().__init__()
        self.player = player
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ You are not the original requester.", ephemeral=True)
            return
        if self.confirm_text.value.strip().upper() != "I AGREE":
            await interaction.response.send_message("❌ Confirmation text did not match ('I AGREE'). Aborting.", ephemeral=True)
            return
        try:
            dur = int(self.duration.value.strip())
        except Exception:
            await interaction.response.send_message("❌ Duration invalid. Please send a number.", ephemeral=True)
            return
        if dur <= 0:
            await interaction.response.send_message("❌ Duration must be positive.", ephemeral=True)
            return
        if dur > EARRAPE_MAX_SECONDS:
            dur = EARRAPE_MAX_SECONDS

        prev_volume = getattr(self.player, "custom_volume", 100)
        ear_vol = min(EARRAPE_VOLUME, 1000)
        # try to set high volume; fallbacks handled
        try:
            await self.player.set_volume(ear_vol)
        except Exception:
            try:
                await self.player.set_volume(min(400, ear_vol))
            except Exception:
                try:
                    await self.player.set_volume(100)
                except Exception:
                    pass
        self.player._earrape_prev_volume = prev_volume

        embed = discord.Embed(title="💥 EARRAPE ACTIVATED", description=f"Activated by {interaction.user.mention} — blasting volume for **{dur}**s.\n**Warning:** This can be very loud. You confirmed consent.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

        async def revert_after(seconds, player, channel):
            await asyncio.sleep(seconds)
            prev = getattr(player, "_earrape_prev_volume", 100)
            try:
                await player.set_volume(prev)
                player.custom_volume = prev
            except Exception:
                try:
                    await player.set_volume(100)
                    player.custom_volume = 100
                except Exception:
                    pass
            try:
                await channel.send(f"🔈 EARRAPE ended — volume restored to {prev}%.")
            except Exception:
                pass

        asyncio.create_task(revert_after(dur, self.player, interaction.channel))

class MusicControls(discord.ui.View):
    def __init__(self, player: wavelink.Player, requester_id: int):
        super().__init__(timeout=None)
        self.player = player
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="⏯ Pause/Resume", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.is_paused():
            await self.player.resume()
            await interaction.response.send_message("▶️ Resumed", ephemeral=True)
        else:
            await self.player.pause()
            await interaction.response.send_message("⏸️ Paused", ephemeral=True)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.is_playing():
            await interaction.response.send_message("❌ Nothing playing.", ephemeral=True); return
        await self.player.stop()
        await interaction.response.send_message("⏭️ Skipped", ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.primary, row=0)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.loop = not getattr(self.player, "loop", False)
        await interaction.response.send_message(f"🔁 Loop {'enabled' if self.player.loop else 'disabled'}", ephemeral=True)

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.primary, row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not getattr(self.player, "queue", None) or self.player.queue.is_empty:
            await interaction.response.send_message("❌ Queue empty.", ephemeral=True); return
        random.shuffle(self.player.queue._queue)
        await interaction.response.send_message("🔀 Queue shuffled", ephemeral=True)

    @discord.ui.button(label="🗑 Clear Queue", style=discord.ButtonStyle.danger, row=1)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if getattr(self.player, "queue", None):
            self.player.queue.clear()
        await interaction.response.send_message("🗑 Queue cleared", ephemeral=True)

    @discord.ui.button(label="🎵 Lyrics", style=discord.ButtonStyle.primary, row=2)
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not getattr(self.player, "current", None):
            await interaction.response.send_message("❌ No song playing.", ephemeral=True); return
        title = getattr(self.player.current, "title", None)
        if not genius:
            await interaction.response.send_message("❌ Genius API not configured.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        try:
            song = genius.search_song(title)
            if song and song.lyrics:
                text = song.lyrics
                if len(text) <= 4000:
                    embed = discord.Embed(title=f"Lyrics — {song.title}", description=text, color=discord.Color.blue())
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
                    for idx, chunk in enumerate(chunks, start=1):
                        embed = discord.Embed(title=f"Lyrics (part {idx}/{len(chunks)}) — {song.title}", description=chunk, color=discord.Color.blue())
                        await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Lyrics not found.", ephemeral=True)
        except Exception:
            await interaction.followup.send("⚠️ Error fetching lyrics.", ephemeral=True)

    @discord.ui.button(label="💥 EARRAPE", style=discord.ButtonStyle.primary, row=2)
    async def earrape(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ Only the original requester can activate EARRAPE from this panel.", ephemeral=True)
            return
        modal = EarrapeConfirmModal(self.player, requester_id=self.requester_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔉 Vol -", style=discord.ButtonStyle.primary, row=3)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        pv = getattr(self.player, "custom_volume", 100)
        new = max(0, pv - 10)
        try:
            await self.player.set_volume(new)
            self.player.custom_volume = new
        except Exception:
            pass
        await interaction.response.send_message(f"🔉 Volume set to {new}%", ephemeral=True)

    @discord.ui.button(label="🔊 Vol +", style=discord.ButtonStyle.primary, row=3)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        pv = getattr(self.player, "custom_volume", 100)
        new = min(1000, pv + 10)
        try:
            await self.player.set_volume(new)
            self.player.custom_volume = new
        except Exception:
            pass
        await interaction.response.send_message(f"🔊 Volume set to {new}%", ephemeral=True)

    @discord.ui.button(label="📜 Queue", style=discord.ButtonStyle.primary, row=4)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not getattr(self.player, "queue", None) or self.player.queue.is_empty:
            await interaction.response.send_message("📭 Queue is empty.", ephemeral=True); return
        embed = discord.Embed(title="🎶 Queue", color=discord.Color.blue())
        for i, t in enumerate(self.player.queue._queue, start=1):
            title = getattr(t, "title", "Unknown")
            req = getattr(t, "requester", None)
            embed.add_field(name=f"{i}. {title}", value=(f"Requested by {req.mention}" if req else "—"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------
# Play command (prefix)
# ----------------------------
@bot.command(name="play")
async def cmd_play(ctx, *, query: str):
    player = await connect_player_for(ctx)
    if not player:
        return
    is_url = query.startswith("http") or "youtube.com" in query or "youtu.be" in query
    try:
        if is_url:
            tracks = await wavelink.YouTubeTrack.search(query=query)
        else:
            tracks = await wavelink.YouTubeTrack.search(query=f"ytsearch:{query}")
    except Exception as e:
        await ctx.send("⚠️ Error searching. Make sure Lavalink is running.")
        print("Search error:", e)
        return

    if not tracks:
        await ctx.send("❌ No results found.")
        return

    added = 0
    for tr in (tracks if isinstance(tracks, list) else [tracks]):
        try:
            tr.requester = ctx.author
        except Exception:
            pass
        await player.queue.put_wait(tr)
        added += 1

    await ctx.send(f"✅ Added {added} track(s) to queue.")
    if not player.is_playing():
        nxt = player.queue.get()
        await player.play(nxt)
        await send_now_playing(player, ctx.author)

# ----------------------------
# Slash play (requires link)
# ----------------------------
@bot.tree.command(name="play", description="Play from a YouTube link or playlist (provide link).")
@discord.app_commands.describe(url="YouTube link or playlist")
async def slash_play(interaction: discord.Interaction, url: str):
    player = await connect_player_for(interaction)
    if not player:
        return
    try:
        tracks = await wavelink.YouTubeTrack.search(query=url)
    except Exception:
        await interaction.response.send_message("⚠️ Error searching. Ensure Lavalink is running.", ephemeral=True)
        return

    if not tracks:
        await interaction.response.send_message("❌ No results found.", ephemeral=True)
        return

    added = 0
    for tr in (tracks if isinstance(tracks, list) else [tracks]):
        try:
            tr.requester = interaction.user
        except Exception:
            pass
        await player.queue.put_wait(tr)
        added += 1

    await interaction.response.send_message(f"✅ Added {added} track(s) to queue.")
    if not player.is_playing():
        nxt = player.queue.get()
        await player.play(nxt)
        await send_now_playing(player, interaction.user)

# ----------------------------
# JOIN & LEAVE (prefix + slash)
# ----------------------------
@bot.command(name="join")
async def cmd_join(ctx):
    player = await connect_player_for(ctx)
    if not player:
        return
    await ctx.send(f"✅ Joined **{ctx.author.voice.channel}**")

@bot.tree.command(name="join", description="Join your voice channel")
async def slash_join(interaction: discord.Interaction):
    player = await connect_player_for(interaction)
    if not player:
        return
    await interaction.response.send_message(f"✅ Joined **{interaction.user.voice.channel}**")

@bot.command(name="leave")
async def cmd_leave(ctx):
    player = ctx.voice_client
    if not player:
        await ctx.send("❌ Not connected.")
        return
    try:
        if getattr(player, "_disconnect_task", None):
            player._disconnect_task.cancel()
    except Exception:
        pass
    try:
        await player.disconnect()
        await ctx.send("⏹️ Disconnected.")
    except Exception:
        await ctx.send("⚠️ Failed to disconnect.")

@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def slash_leave(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True)
        return
    try:
        if getattr(player, "_disconnect_task", None):
            player._disconnect_task.cancel()
    except Exception:
        pass
    try:
        await player.disconnect()
        await interaction.response.send_message("⏹️ Disconnected.", ephemeral=False)
    except Exception:
        await interaction.response.send_message("⚠️ Failed to disconnect.", ephemeral=True)

# ----------------------------
# Other controls (prefix)
# ----------------------------
@bot.command(name="skip")
async def cmd_skip(ctx):
    player = ctx.voice_client
    if not player or not player.is_playing():
        await ctx.send("❌ Nothing to skip."); return
    await player.stop()
    await ctx.send("⏭️ Skipped.")

@bot.command(name="pause")
async def cmd_pause(ctx):
    player = ctx.voice_client
    if not player or not player.is_playing():
        await ctx.send("❌ Not playing."); return
    await player.pause()
    await ctx.send("⏸️ Paused.")

@bot.command(name="resume")
async def cmd_resume(ctx):
    player = ctx.voice_client
    if not player or not player.is_paused():
        await ctx.send("❌ Nothing paused."); return
    await player.resume()
    await ctx.send("▶️ Resumed.")

@bot.command(name="stop")
async def cmd_stop(ctx):
    player = ctx.voice_client
    if not player:
        await ctx.send("❌ Not connected."); return
    await player.disconnect()
    await ctx.send("⏹️ Stopped and disconnected.")

@bot.command(name="queue")
async def cmd_queue(ctx):
    player = ctx.voice_client
    if not player or player.queue.is_empty:
        await ctx.send("📭 Queue is empty."); return
    embed = discord.Embed(title="🎶 Queue", color=discord.Color.blue())
    for idx, t in enumerate(player.queue._queue, start=1):
        title = getattr(t, "title", "Unknown")
        req = getattr(t, "requester", None)
        embed.add_field(name=f"{idx}. {title}", value=(f"Requested by {req.mention}" if req else "—"), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="np")
async def cmd_np(ctx):
    player = ctx.voice_client
    if player and getattr(player, "current", None):
        track = player.current
        await ctx.send(f"🎵 Now playing: **{getattr(track,'title','Unknown')}**")
    else:
        await ctx.send("❌ Nothing playing.")

@bot.command(name="volume")
async def cmd_volume(ctx, vol: int):
    player = ctx.voice_client
    if not player:
        await ctx.send("❌ Not connected."); return
    vol = max(0, min(vol, 1000))
    try:
        await player.set_volume(vol)
        player.custom_volume = vol
        await ctx.send(f"🔊 Volume set to {vol}%")
    except Exception:
        await ctx.send("⚠️ Could not set volume on the node.")

# ----------------------------
# 24/7 toggle (prefix & slash)
# ----------------------------
@bot.command(name="set247")
@commands.has_guild_permissions(manage_guild=True)
async def cmd_set247(ctx, mode: str):
    mode = mode.lower().strip()
    ensure_guild_settings(ctx.guild.id)
    if mode in ("on", "true", "1", "enable", "enabled"):
        settings["guilds"][str(ctx.guild.id)]["247"] = True
        save_settings(settings)
        await ctx.send("✅ 24/7 enabled for this server.")
    elif mode in ("off", "false", "0", "disable", "disabled"):
        settings["guilds"][str(ctx.guild.id)]["247"] = False
        save_settings(settings)
        await ctx.send("✅ 24/7 disabled for this server.")
    else:
        await ctx.send("Usage: `?set247 on` or `?set247 off`")

@bot.tree.command(name="set_247", description="Enable or disable 24/7 mode for this guild (Manage Server required).")
@discord.app_commands.describe(enabled="Enable or disable 24/7 mode")
async def slash_set_247(interaction: discord.Interaction, enabled: bool):
    member = interaction.user
    if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator or member == member.guild.owner):
        await interaction.response.send_message("❌ You need Manage Server or Administrator to change this.", ephemeral=True)
        return
    ensure_guild_settings(interaction.guild.id)
    settings["guilds"][str(interaction.guild.id)]["247"] = bool(enabled)
    save_settings(settings)
    await interaction.response.send_message(f"✅ 24/7 {'enabled' if enabled else 'disabled'} for this server.")

# ----------------------------
# Now playing embed sender
# ----------------------------
async def send_now_playing(player: wavelink.Player, requester):
    tr = getattr(player, "current", None)
    if not tr:
        return
    title = getattr(tr, "title", "Unknown")
    url = getattr(tr, "uri", None)
    thumb = getattr(tr, "thumb", None)
    author = getattr(tr, "author", "Unknown")
    length_ms = getattr(tr, "length", 0) or 0
    minutes = (length_ms // 60000)
    seconds = (length_ms // 1000) % 60
    qlen = len(player.queue._queue) if getattr(player, "queue", None) else 0
    embed = discord.Embed(title="▶️ NOW PLAYING", description=f"[{title}]({url})" if url else title, color=discord.Color.blue())
    embed.add_field(name="Artist", value=author, inline=True)
    embed.add_field(name="Duration", value=f"{minutes:02d}:{seconds:02d}", inline=True)
    embed.add_field(name="Queue", value=str(qlen), inline=True)
    embed.add_field(name="Requested", value=(requester.mention if requester else "—"), inline=True)
    if thumb:
        embed.set_thumbnail(url=thumb)
    view = MusicControls(player, requester_id=getattr(requester, "id", None))
    ch = getattr(player, "text_channel", None)
    if ch:
        await ch.send(embed=embed, view=view)

# ----------------------------
# Track end event
# ----------------------------
@bot.event
async def on_wavelink_track_end(player: wavelink.Player, track, reason):
    # loop single
    if getattr(player, "loop", False):
        try:
            await player.play(track)
            return
        except Exception:
            pass
    # play next
    if not player.queue.is_empty:
        nxt = player.queue.get()
        await player.play(nxt)
        try:
            await send_now_playing(player, getattr(player, "current_requester", None) or player.text_channel)
        except Exception:
            pass
    else:
        gid = player.guild.id
        if is_247_enabled(gid):
            return
        await schedule_auto_disconnect(player, guild_id=gid, delay=120)

# ----------------------------
# Start keepalive thread & run bot
# ----------------------------
def start_keepalive():
    thr = threading.Thread(target=run_flask, daemon=True)
    thr.start()
    print(f"Flask keepalive running on port {FLASK_PORT}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Please set DISCORD_TOKEN in environment variables and restart.")
    else:
        start_keepalive()
        bot.run(DISCORD_TOKEN)
