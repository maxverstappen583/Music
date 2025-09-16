# musicbot_247_flask.py
# Full-featured music bot + diagnostics for join/connect issues.
# Make sure DISCORD_TOKEN and other env vars are set before running.

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
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN") or os.getenv("GENIUS_API_TOKEN", "")
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "localhost")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", os.getenv("LAVALINK_PASS", "youshallnotpass"))
PREFIX = os.getenv("BOT_PREFIX", "?")
FLASK_PORT = int(os.getenv("FLASK_PORT", os.getenv("PORT", "8080")))

EARRAPE_DEFAULT_SECONDS = 8
EARRAPE_MAX_SECONDS = 30
EARRAPE_VOLUME = 400

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
# Utility: message sending (Context or Interaction)
# ----------------------------
async def safe_send(dest, content=None, embed=None, view=None, ephemeral=False):
    try:
        if isinstance(dest, commands.Context):
            return await dest.send(content=content, embed=embed, view=view)
        else:
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
# On ready -> connect to Lavalink node and report PyNaCl presence
# ----------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    # report PyNaCl availability (voice needs this)
    try:
        import nacl
        print("PyNaCl imported OK.")
    except Exception as e:
        print("WARNING: PyNaCl not available or failed to import:", e)

    # Try to create/attach a Lavalink node
    if not wavelink.NodePool.nodes:
        try:
            await wavelink.NodePool.create_node(
                bot=bot,
                host=LAVALINK_HOST,
                port=LAVALINK_PORT,
                password=LAVALINK_PASSWORD,
                https=False,
            )
            print(f"✅ Connected to Lavalink at {LAVALINK_HOST}:{LAVALINK_PORT}")
        except Exception as e:
            print("❌ Could not connect to Lavalink node:", e)
            print("If Lavalink is running in the same container, ensure it finished booting before the bot connects.")
    # sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"🔧 Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync failed:", e)

# ----------------------------
# Connect player helper (tries to connect with wavelink.Player)
# and provides helpful error messages
# ----------------------------
async def connect_player_for(ctx_or_inter):
    user = ctx_or_inter.author if isinstance(ctx_or_inter, commands.Context) else ctx_or_inter.user
    if not user or not getattr(user, "voice", None) or not getattr(user.voice, "channel", None):
        await safe_send(ctx_or_inter, "❌ You must be in a voice channel first.", ephemeral=isinstance(ctx_or_inter, discord.Interaction))
        return None

    channel = user.voice.channel
    guild = ctx_or_inter.guild
    player = guild.voice_client

    if player:
        # already connected, return it
        return player

    # Attempt to connect using wavelink.Player (requires Lavalink up)
    try:
        player = await channel.connect(cls=wavelink.Player)
        # set helpful attributes
        player.queue = wavelink.Queue()
        player.loop = False
        player.custom_volume = getattr(player, "custom_volume", 100)
        player.text_channel = ctx_or_inter.channel
        player._disconnect_task = None
        player._earrape_prev_volume = getattr(player, "custom_volume", 100)
        return player
    except Exception as e:
        # Provide detailed feedback to user and logs
        msg = f"❌ Failed to join voice channel using wavelink.Player: `{e}`\n"
        msg += "Possible causes:\n"
        msg += "- Lavalink is not running or not reachable (check logs / start script).\n"
        msg += "- Missing system libs (ffmpeg / PyNaCl).\n"
        msg += "I will attempt a standard Discord voice connect as a fallback (playback may not work until Lavalink is available)."
        print("Join error (wavelink connect):", e)
        await safe_send(ctx_or_inter, msg, ephemeral=isinstance(ctx_or_inter, discord.Interaction))

        # Fallback: try normal discord.py connect so the bot at least joins
        try:
            discord_vc = await channel.connect()
            print("Fallback: connected with discord.VoiceClient (playback via Lavalink will require Lavalink).")
            # Note: this VoiceClient won't support wavelink playback until replaced by a wavelink.Player.
            return discord_vc
        except Exception as e2:
            print("Fallback discord connect failed:", e2)
            await safe_send(ctx_or_inter, f"❌ Failed to join VC (fallback also failed): `{e2}`", ephemeral=isinstance(ctx_or_inter, discord.Interaction))
            return None

# ----------------------------
# Auto-disconnect scheduler (2 minutes if 24/7 disabled)
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
            except Exception:
                pass
        except asyncio.CancelledError:
            return

    player._disconnect_task = asyncio.create_task(_task())

# ----------------------------
# Simple Join/Leave (prefix & slash) with diagnostic output
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
    except Exception as e:
        await ctx.send(f"⚠️ Failed to disconnect: `{e}`")

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
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Failed to disconnect: `{e}`", ephemeral=True)

# ----------------------------
# Minimal play that shows errors clearly (prefix + slash)
# Only YouTube supported (no Spotify)
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
        await ctx.send(f"⚠️ Search error: `{e}` — make sure Lavalink is running and reachable.")
        print("Search error:", e)
        return

    if not tracks:
        await ctx.send("❌ No results found.")
        return

    # If this player is a discord.VoiceClient fallback (not wavelink.Player) - warn user
    if not isinstance(player, wavelink.Player):
        await ctx.send("⚠️ Playback requires Lavalink/wavelink. Bot connected via fallback; playback may not work until Lavalink is available.")
        return

    added = 0
    for tr in (tracks if isinstance(tr, list) else [tracks]):
        try:
            tr.requester = ctx.author
        except Exception:
            pass
        await player.queue.put_wait(tr)
        added += 1

    await ctx.send(f"✅ Added {added} track(s) to queue.")
    if not player.is_playing():
        await player.play(player.queue.get())
        await send_now_playing(player, ctx.author)

@bot.tree.command(name="play", description="Play from a YouTube link or playlist")
@discord.app_commands.describe(url="YouTube link or playlist")
async def slash_play(interaction: discord.Interaction, url: str):
    player = await connect_player_for(interaction)
    if not player:
        return
    try:
        tracks = await wavelink.YouTubeTrack.search(query=url)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Search error: `{e}`", ephemeral=True)
        print("Search error (slash):", e)
        return

    if not tracks:
        await interaction.response.send_message("❌ No results found.", ephemeral=True)
        return

    if not isinstance(player, wavelink.Player):
        await interaction.response.send_message("⚠️ Playback requires Lavalink/wavelink. Bot connected via fallback; playback may not work until Lavalink is available.", ephemeral=True)
        return

    added = 0
    for tr in (tracks if isinstance(tr, list) else [tracks]):
        try:
            tr.requester = interaction.user
        except Exception:
            pass
        await player.queue.put_wait(tr)
        added += 1

    await interaction.response.send_message(f"✅ Added {added} track(s) to queue.")
    if not player.is_playing():
        await player.play(player.queue.get())
        await send_now_playing(player, interaction.user)

# ----------------------------
# Controls: skip/pause/resume/stop/np/queue/volume (prefix only)
# (similar to earlier — left concise)
# ----------------------------
@bot.command(name="skip")
async def cmd_skip(ctx):
    player = ctx.voice_client
    if not player or not getattr(player, "is_playing", lambda: False)():
        await ctx.send("❌ Nothing to skip."); return
    await player.stop()
    await ctx.send("⏭️ Skipped.")

@bot.command(name="pause")
async def cmd_pause(ctx):
    player = ctx.voice_client
    if not player or not getattr(player, "is_playing", lambda: False)():
        await ctx.send("❌ Not playing."); return
    await player.pause()
    await ctx.send("⏸️ Paused.")

@bot.command(name="resume")
async def cmd_resume(ctx):
    player = ctx.voice_client
    if not player or not getattr(player, "is_paused", lambda: False)():
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
    if not player or not getattr(player, "queue", None) or player.queue.is_empty:
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
    except Exception as e:
        await ctx.send(f"⚠️ Could not set volume: `{e}`")

# ----------------------------
# Minimal now playing sender (no fancy controls here for brevity)
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
    ch = getattr(player, "text_channel", None)
    if ch:
        await ch.send(embed=embed)

# ----------------------------
# Track end: play next or schedule disconnect
# ----------------------------
@bot.event
async def on_wavelink_track_end(player: wavelink.Player, track, reason):
    if getattr(player, "loop", False):
        try:
            await player.play(track)
            return
        except Exception:
            pass
    if not player.queue.is_empty:
        nxt = player.queue.get()
        await player.play(nxt)
        try:
            await send_now_playing(player, player.text_channel)
        except Exception:
            pass
    else:
        gid = player.guild.id
        if is_247_enabled(gid):
            return
        await schedule_auto_disconnect(player, guild_id=gid, delay=120)

# ----------------------------
# Start keepalive & run
# ----------------------------
def start_keepalive():
    thr = threading.Thread(target=run_flask, daemon=True)
    thr.start()
    print(f"Flask keepalive running on port {FLASK_PORT}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Please set DISCORD_TOKEN environment variable.")
    else:
        start_keepalive()
        bot.run(DISCORD_TOKEN)
