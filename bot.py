"""Gogu — Bot de muzică Discord."""
import os
import sys
import logging
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

# Înainte de music.* — citește .env local și lasă logging activ pentru logurile din config la import.
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gogu")

_yt_proxy = (os.getenv("YT_PROXY") or "").strip()
if _yt_proxy:
    log.info(
        "YT_PROXY is set (socks5=%s, len=%s)",
        str(_yt_proxy.startswith("socks5")).lower(),
        len(_yt_proxy),
    )
else:
    log.info("YT_PROXY is not set — YouTube traffic uses direct egress")

import discord
from discord.ext import commands

from music.config import DOWNLOAD_DIR, log as music_log
from music.state import get_state, guild_states
from music.utils import safe_delete, cleanup_file
from music.autoplay import prefill_autoplay_queue

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    log.error("DISCORD_TOKEN not set.")
    sys.exit(1)

# Cleanup downloads la pornire
for f in os.listdir(DOWNLOAD_DIR):
    try:
        os.remove(os.path.join(DOWNLOAD_DIR, f))
    except OSError:
        pass

# Write YouTube cookies if provided via env var
_yt_cookies = os.getenv("YT_COOKIES_CONTENT")
if _yt_cookies:
    _yt_cookies = _yt_cookies.replace("\\n", "\n").replace("\\t", "\t")
    with open("cookies.txt", "w") as f:
        f.write(_yt_cookies)
    lines = [l for l in _yt_cookies.strip().splitlines()
             if l.strip() and not l.startswith('#')]
    log.info(f"YouTube cookies written ({len(lines)} entries)")
    from music.config import apply_cookies
    apply_cookies()
else:
    log.warning("YT_COOKIES_CONTENT not set — YouTube may block requests")

# Test PO Token server connectivity
try:
    import urllib.request
    import urllib.error
    # /ping is the endpoint the bgutil plugin itself probes and it reports the
    # server version. /token does not exist, so it used to log a useless 404.
    req = urllib.request.Request('http://127.0.0.1:4416/ping', method='GET')
    with urllib.request.urlopen(req, timeout=5) as resp:
        log.info(f"PO Token server OK: {resp.read(200).decode('utf-8', 'replace')}")
except urllib.error.HTTPError as e:
    log.warning(f"PO Token server answered HTTP {e.code} on /ping")
except Exception as e:
    log.warning(f"PO Token server NOT responding: {e}")

# Test yt-dlp (rapid, cu timeout) — prin SOCKS5 poate bloca minute dacă proxy-ul e lent; nu întârzie niciodată login-ul Discord.
def _ytdlp_startup_probe():
    import yt_dlp
    from music.config import yt_client_args
    test_opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'format': 'best', 'socket_timeout': 8,
        'extractor_args': yt_client_args('mweb', 'web_safari'),
    }
    if _yt_proxy:
        test_opts['proxy'] = _yt_proxy
    with yt_dlp.YoutubeDL(test_opts) as ydl:
        return ydl.extract_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ', download=False)

if os.getenv('SKIP_YTDLP_STARTUP_TEST', '').strip().lower() in ('1', 'true', 'yes'):
    log.info('SKIP_YTDLP_STARTUP_TEST set — skipping yt-dlp probe')
else:
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_ytdlp_startup_probe)
            info = fut.result(timeout=20)
        fmts = info.get('formats', []) if info else []
        real = sum(1 for f in fmts if f.get('acodec', 'none') != 'none')
        log.info(f"yt-dlp startup test: {len(fmts)} formats ({real} real audio)")
        if real == 0:
            log.warning("yt-dlp startup test: 0 real formats — YouTube may be blocking this IP")
    except concurrent.futures.TimeoutError:
        log.warning(
            "yt-dlp startup test timed out after 20s (often slow/bad proxy). "
            "Discord will still start; set SKIP_YTDLP_STARTUP_TEST=1 to skip this check."
        )
    except Exception as e:
        log.warning(f"yt-dlp startup test failed: {e}")

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    description="Gogu — Music Bot",
    # Fara asta '!PLAY' si '!Play' sunt respinse ca CommandNotFound, iar botul
    # pare mort fiindca nu intra in voice si nu raspunde nimic.
    case_insensitive=True,
)

# --- Music engine init ---
import music.player as player
from music.ui import update_player_ui


def start_timeout(ctx):
    guild_id = ctx.guild.id if hasattr(ctx, 'guild') else ctx.id
    state = get_state(guild_id)
    if state.timeout_task and not state.timeout_task.done():
        state.timeout_task.cancel()
    state.timeout_task = bot.loop.create_task(idle_timer(ctx))


def cancel_timeout(ctx):
    guild_id = ctx.guild.id if hasattr(ctx, 'guild') else ctx.id
    state = get_state(guild_id)
    if state.timeout_task and not state.timeout_task.done():
        state.timeout_task.cancel()
        state.timeout_task = None


async def idle_timer(ctx):
    await asyncio.sleep(60)
    state = get_state(ctx.guild.id)
    vc = ctx.voice_client
    if state.always_on:
        if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            if state.last_url and not state.is_loading:
                state.autoplay = True
                if not state.queue:
                    try:
                        await prefill_autoplay_queue(state, bot.loop)
                    except Exception:
                        pass
                if state.queue:
                    state.is_loading = True
                    next_item = state.queue.pop(0)
                    await player.process_play(ctx, next_item['query'], is_radio=True)
        return
    if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
        await vc.disconnect()
        await safe_delete(state.current_msg)
        state.current_msg = await ctx.send(
            "Am iesit - inactiv 1 minut.", delete_after=15
        )
        state.queue.clear()
        state.history.clear()


player.init(bot, update_player_ui, start_timeout, cancel_timeout)

from music.commands import setup_music_commands
setup_music_commands(
    bot, player.process_play, player.play_next,
    update_player_ui, start_timeout, cancel_timeout
)

# --- Events ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user and before.channel and after.channel:
        was_muted = before.mute or before.self_mute
        is_muted = after.mute or after.self_mute
        vc = member.guild.voice_client
        if vc:
            if not was_muted and is_muted and vc.is_playing():
                vc.pause()
                music_log.info("Bot muted -> pause")
            elif was_muted and not is_muted and vc.is_paused():
                vc.resume()
                music_log.info("Bot unmuted -> resume")

    if member == bot.user and before.channel and not after.channel:
        state = get_state(member.guild.id)
        state.queue.clear()
        state.autoplay = False
        state.loop_mode = 0
        state.is_loading = False
        if state.preloaded:
            cleanup_file(state.preloaded.get('filename'), bot.loop)
            state.preloaded = None
        cancel_timeout(member.guild)

    if not member.bot and before.channel:
        bot_in_channel = any(m == bot.user for m in before.channel.members)
        if bot_in_channel and len(before.channel.members) == 1:
            await asyncio.sleep(20)
            vc = member.guild.voice_client
            if vc and vc.channel == before.channel:
                real = [m for m in before.channel.members if not m.bot]
                if not real:
                    await vc.disconnect()


@bot.event
async def on_message(message):
    """Obligatoriu: dacă suprascrii on_message, trebuie apelat process_commands."""
    if not message.author.bot and message.guild:
        raw = message.content or ""
        if raw.lstrip().startswith("!"):
            log.info(
                "Heard prefix message: author=%s content_len=%s preview=%r",
                message.author,
                len(raw),
                raw[:120],
            )
    await bot.process_commands(message)


@bot.before_invoke
async def _log_command_invoke(ctx):
    if ctx.command:
        log.info("Running command: %s (guild=%s)", ctx.command.name, ctx.guild and ctx.guild.id)


@bot.event
async def on_ready():
    player._loop = asyncio.get_event_loop()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Connected to {len(bot.guilds)} guild(s)")
    log.info(
        "Pentru comenzi cu ! în server: Bot > Privileged Gateway Intents > "
        "MESSAGE CONTENT INTENT = ON în Developer Portal."
    )
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    except Exception:
        pass
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name="!mhelp"
        )
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Logat, nu ignorat: altfel o comanda greasita e indistinguibila de un bot picat.
        log.info("Unknown command: %r", (ctx.message.content or "")[:80])
        return
    log.error(f"Command error '{ctx.command}': {error}")


# --- Entry point ---
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass


def main():
    port = int(os.getenv("PORT", "8080"))
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), _Health).serve_forever(),
        daemon=True,
    ).start()
    log.info("Connecting to Discord Gateway...")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
