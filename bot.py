"""Gogu — Bot de muzică Discord."""
import os
import sys
import logging
import asyncio
import threading
import time
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
from music.config import apply_cookies, seed_cookies_from_env

_cookie_path, _cookie_entries = seed_cookies_from_env(os.getenv("YT_COOKIES_CONTENT"))
if _cookie_path:
    log.info(f"YouTube cookies active ({_cookie_entries} entries)")
    apply_cookies(_cookie_path)
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

# Proba yt-dlp la pornire: acum OPT-IN, nu opt-out.
# start.sh isi dezactivase deja proba proprie cu motivul "probe consumes the
# fresh YouTube session and causes 429 for the bot", iar bot.py facea exact
# asta la fiecare boot. In plus, `with ThreadPoolExecutor(...)` face
# shutdown(wait=True) la ieșire, deci timeout-ul de 20s nu limita nimic:
# boot-ul aștepta oricum extractia intreaga.
def _ytdlp_startup_probe():
    import yt_dlp
    from music.config import yt_client_args, count_real_formats
    test_opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'format': 'best', 'socket_timeout': 8,
        'extractor_args': yt_client_args(*_probe_clients()),
    }
    if _yt_proxy:
        test_opts['proxy'] = _yt_proxy
    with yt_dlp.YoutubeDL(test_opts) as ydl:
        info = ydl.extract_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                                download=False)
    fmts = (info or {}).get('formats', [])
    return len(fmts), count_real_formats(fmts)


def _probe_clients():
    from music.config import WEB_CLIENTS
    return WEB_CLIENTS


if os.getenv('YTDLP_STARTUP_PROBE', '').strip().lower() in ('1', 'true', 'yes'):
    # Executor nu-l inchidem cu `with`: altfel ieșirea din bloc ar aștepta
    # thread-ul si timeout-ul de mai jos ar fi decorativ.
    _probe_ex = ThreadPoolExecutor(max_workers=1)
    try:
        total, real = _probe_ex.submit(_ytdlp_startup_probe).result(timeout=20)
        log.info(f"yt-dlp startup probe: {total} formats ({real} redabile)")
        if real == 0:
            log.warning("yt-dlp startup probe: 0 formate redabile — "
                        "YouTube blocheaza probabil acest IP")
    except concurrent.futures.TimeoutError:
        log.warning("yt-dlp startup probe: timeout dupa 20s, continui oricum")
    except Exception as e:
        log.warning(f"yt-dlp startup probe a esuat: {e}")
    finally:
        _probe_ex.shutdown(wait=False)
else:
    log.info("yt-dlp startup probe dezactivata (YTDLP_STARTUP_PROBE=1 o activeaza)")

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

_tree_synced = False

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
    """Detecteaza inactivitatea. NU reda nimic: doar programeaza si se re-armeaza.

    Inainte, aceasta functie facea ea insasi redarea, in interiorul task-ului pe
    care fiecare comanda il anuleaza. CancelledError e BaseException, deci
    process_play murea fara sa elibereze is_loading, si botul tacea la orice
    !play. Acum redarea pleaca in propriul task, iar timer-ul se re-armeaza
    intotdeauna, altfel 24/7 murea definitiv la primul prefill fara rezultate.
    """
    state = get_state(ctx.guild.id)
    cancelled = False
    try:
        await asyncio.sleep(60)
        vc = ctx.voice_client
        connected = bool(vc and vc.is_connected())
        idle = connected and not vc.is_playing() and not vc.is_paused()

        if state.always_on:
            if not connected or not idle or state.is_loading:
                return
            now = time.time()
            if now < state.breaker_until or now < state.idle_quiet_until:
                return
            if not state.last_url:
                return
            state.autoplay = True
            if not state.queue:
                try:
                    await prefill_autoplay_queue(state, bot.loop)
                except Exception as e:
                    music_log.warning(f"Prefill 24/7 esuat: {e}")
            if state.queue:
                player.play_next(ctx)
            else:
                # Nimic de redat: taci 5 minute in loc sa bati YouTube-ul
                # din minut in minut cat timp ne blocheaza.
                state.idle_quiet_until = time.time() + 300
                music_log.info("24/7: nimic de redat, reincerc in 5 minute")
            return

        if idle:
            await vc.disconnect()
            await safe_delete(state.current_msg)
            state.current_msg = await ctx.send(
                "Am iesit - inactiv 1 minut.", delete_after=15
            )
            state.queue.clear()
            state.history.clear()
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Re-armare doar daca nu am fost anulati (altfel cancel_timeout n-ar
        # avea niciun efect) si doar in 24/7, unde botul trebuie sa rezista.
        # call_soon amana pana task-ul e done, ca start_timeout sa nu se
        # anuleze pe el insusi.
        if not cancelled and state.always_on:
            bot.loop.call_soon(start_timeout, ctx)


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
        state = get_state(member.guild.id)
        # 24/7 inseamna exact "stai conectat", deci nu plecam.
        if state.always_on:
            return
        bot_in_channel = any(m == bot.user for m in before.channel.members)
        # Numaram doar oamenii: "len(members) == 1" nu se declansa deloc daca
        # in canal mai statea un al doilea bot, si Gogu cânta la pereti.
        humans_left = [m for m in before.channel.members if not m.bot]
        if bot_in_channel and not humans_left:
            await asyncio.sleep(20)
            vc = member.guild.voice_client
            if vc and vc.channel == before.channel:
                real = [m for m in before.channel.members if not m.bot]
                if not real:
                    await vc.disconnect()


@bot.check
async def _guild_only(ctx):
    """Toate comenzile dereferentiaza ctx.guild.id, deci in DM crapau."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


@bot.event
async def on_message(message):
    """Obligatoriu: dacă suprascrii on_message, trebuie apelat process_commands."""
    if not message.author.bot and message.guild:
        raw = message.content or ""
        if raw.lstrip().startswith("!"):
            # Fara autor si fara continut: logul asta a ajuns odata in git
            # public cu handle-uri de membri si tot ce au ascultat.
            log.info("Heard prefix message: content_len=%s", len(raw))
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
    global _tree_synced
    if not _tree_synced:
        try:
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            _tree_synced = True
        except discord.HTTPException as e:
            log.warning(f"Sync arbore comenzi esuat: {e}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name="!mhelp"
        )
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Logat, nu ignorat: altfel o comanda greasita e indistinguibila de un
        # bot picat. Doar numele comenzii, nu continutul mesajului.
        attempted = (ctx.message.content or '').lstrip('!').split(' ')[0][:32]
        log.info("Unknown command: %r", attempted)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await _reply(ctx, f"Lipseste un argument: `{error.param.name}`. "
                                 f"Vezi `!mhelp`.")
    if isinstance(error, commands.BadArgument):
        return await _reply(ctx, "Argument invalid. Vezi `!mhelp`.")
    if isinstance(error, commands.NoPrivateMessage):
        return await _reply(ctx, "Comenzile merg doar pe server, nu in DM.")
    if isinstance(error, commands.CommandInvokeError):
        log.error(f"Command error '{ctx.command}': {error.original}", exc_info=error.original)
        return await _reply(ctx, "Ceva a crapat la comanda asta. Verifica logurile.")
    log.error(f"Command error '{ctx.command}': {error}")
    await _reply(ctx, "Nu am putut executa comanda.")


async def _reply(ctx, text):
    try:
        await ctx.send(text, delete_after=15)
    except discord.HTTPException:
        pass


# --- Entry point ---
class _Health(BaseHTTPRequestHandler):
    server_version = 'gogu'          # fara banner cu versiunea de Python
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = 10                     # o conexiune inactiva nu mai blocheaza thread-ul

    def do_GET(self):
        if self.path.rstrip('/') not in ('', '/health'):
            self.send_error(404)
            return
        ready = bool(bot and bot.is_ready() and not bot.is_closed())
        body = b'ok' if ready else b'starting'
        self.send_response(200 if ready else 503)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
