"""Gogu — Bot de muzică Discord."""
import hmac
import json
import os
import sys
import logging
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

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

from music.config import DOWNLOAD_DIR, env_num, log as music_log
from music.state import (get_state, guild_states, mark_paused, mark_resumed,
                         set_autoplay)
from music.idle import DISCONNECT, RADIO, decide_idle_action
from music.utils import safe_delete
from music.autoplay import prefill_autoplay_queue
from music import diag
from music import ytdlp as ytdlp_mod

TOKEN = os.getenv("DISCORD_TOKEN")

from music.config import apply_cookies, seed_cookies_from_env
from music.config import sweep_borrowed_cookies
from music.utils import sweep_partials, trim_download_cache


def _prepare_volume():
    """Curatenie si cookie-uri la pornire. Chemata din main(), NU la import.

    Cand rula la import, orice `import bot` executa scrieri pe volumul real.
    Reprodus cu COOKIE_DIR pointat pe un volum de test: rularea unui singur
    fisier de teste a rescris jar-ul rotit de yt-dlp cu valoarea veche din env,
    a PROMOVAT valoarea veche in `cookies.txt.good` (distrugand si tinta de
    revenire) si a golit tot cache-ul audio. Fara `/data`,
    `_cookie_file_paths()` cade pe `.`, deci `python tests/run_all.py` in
    checkout-ul viu scria `./cookies.txt` — adica exact modul documentat de a
    rula suita ataca starea de producție.
    """
    # La pornire stergem DOAR descarcarile intrerupte. Stergerea intregului
    # director arunca la fiecare deploy tot ce fusese ascultat, iar pe volum acel
    # audio e chiar cache-ul care face a doua redare gratuita.
    sweep_partials()
    # Copiile de jar rămase de la un proces omorat: fiecare conține o sesiune
    # Google, deci nu au ce sa zaca pe volum.
    sweep_borrowed_cookies()
    trim_download_cache(set())

    cookie_path, cookie_entries = seed_cookies_from_env(
        os.getenv("YT_COOKIES_CONTENT"))
    if cookie_path:
        log.info(f"YouTube cookies active ({cookie_entries} entries)")
        apply_cookies(cookie_path)
    else:
        # Fara cookies pe un IP de datacenter nu exista nicio cale functionala
        # catre YouTube. E o eroare, nu un avertisment: chiar daca pe volum sta un
        # jar valid, `apply_cookies` nu e chemat, deci nu il foloseste nimeni.
        log.error("YT_COOKIES_CONTENT not set — botul merge ca GUEST, iar de pe "
                  "un IP de datacenter asta inseamna ca YouTube va refuza tot")

    # Test PO Token server connectivity
    try:
        import urllib.error
        import urllib.request
        # /ping is the endpoint the bgutil plugin itself probes and it reports the
        # server version. /token does not exist, so it used to log a useless 404.
        req = urllib.request.Request('http://127.0.0.1:4416/ping', method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info(f"PO Token server OK: "
                     f"{resp.read(200).decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as e:
        log.warning(f"PO Token server answered HTTP {e.code} on /ping")
    except OSError as e:
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


def _run_startup_probe():
    """Sonda opt-in de yt-dlp. Si ea din main(), nu la import: face cereri reale."""
    if os.getenv('YTDLP_STARTUP_PROBE', '').strip().lower() not in ('1', 'true', 'yes'):
        log.info("yt-dlp startup probe dezactivata (YTDLP_STARTUP_PROBE=1 o activeaza)")
        return
    # Executor nu-l inchidem cu `with`: altfel ieșirea din bloc ar aștepta
    # thread-ul si timeout-ul de mai jos ar fi decorativ.
    probe_ex = ThreadPoolExecutor(max_workers=1)
    try:
        total, real = probe_ex.submit(_ytdlp_startup_probe).result(timeout=20)
        log.info(f"yt-dlp startup probe: {total} formats ({real} redabile)")
        if real == 0:
            log.warning("yt-dlp startup probe: 0 formate redabile — "
                        "YouTube blocheaza probabil acest IP")
    except concurrent.futures.TimeoutError:
        log.warning("yt-dlp startup probe: timeout dupa 20s, continui oricum")
    except Exception as e:
        log.warning(f"yt-dlp startup probe a esuat: {e}")
    finally:
        probe_ex.shutdown(wait=False)

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
    # La granita de incredere, o singura data. Botul ecoueaza text scris de
    # utilizatori: interogarea din confirmarea de coada, titlurile din `!remove` si
    # `!move`, numele comenzii greșite din mesajele de eroare. `item_title` doar
    # trunchiaza, nu escapeaza. Fara asta, `ConnectionState.allowed_mentions` e
    # None, discord.py omite complet campul din payload, si Discord interpreteaza
    # fiecare mention din text — deci `!play @everyone ceva` devine un ping real
    # trimis de bot.
    allowed_mentions=discord.AllowedMentions.none(),
)

_tree_synced = False
_heartbeat_task = None

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


IDLE_TICK_SEC = 60
IDLE_QUIET_SEC = 300


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
        await asyncio.sleep(IDLE_TICK_SEC)
        vc = ctx.voice_client
        connected = bool(vc and vc.is_connected())
        decision = decide_idle_action(
            state, connected=connected,
            playing=bool(connected and vc.is_playing()),
            paused=bool(connected and vc.is_paused()),
            now=time.time())
        # Motivul se retine: pana acum, "de ce nu cânta 24/7?" nu avea niciun
        # raspuns in loguri, pentru ca fiecare ramura ieșea printr-un `return` mut.
        state.last_idle_reason = decision.reason
        music_log.debug(f"tick inactivitate: {decision.action} ({decision.reason})")

        if decision.resume_autoplay:
            state.autoplay = True

        if decision.action == RADIO:
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
                state.idle_quiet_until = time.time() + IDLE_QUIET_SEC
                music_log.info("24/7: nimic de redat, reincerc in 5 minute")
            return

        if decision.action == DISCONNECT:
            await vc.disconnect()
            await safe_delete(state.current_msg)
            # Mesajul de plecare NU se reține ca panou. Cand era pastrat in
            # `current_msg`, discord.py il stergea 15 secunde mai tarziu si de
            # atunci fiecare refresh de panou edita un mesaj inexistent: 404
            # inghitit, `current_msg` rămas plin, deci nici auto-vindecarea (care
            # se uita doar la None) nu se declanșa. Panoul cu butoane nu mai
            # apărea pana la urmatorul !np sau pana la pornirea unei piese.
            state.current_msg = None
            await ctx.send("Am iesit - inactiv 1 minut.", delete_after=15)
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
# Rabdarea inainte de a pleca dintr-un canal ramas gol. Constanta, nu literal in
# cod, ca sa poata fi scurtata din teste: altfel verificarea comportamentului ar
# cere 20 de secunde de aȘteptare reala.
EMPTY_CHANNEL_GRACE_SEC = 20


@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user and before.channel and after.channel:
        was_muted = before.mute or before.self_mute
        is_muted = after.mute or after.self_mute
        vc = member.guild.voice_client
        if vc:
            state = get_state(member.guild.id)
            if not was_muted and is_muted and vc.is_playing():
                vc.pause()
                mark_paused(state, time.time())
                music_log.info("Bot muted -> pause")
            elif was_muted and not is_muted and vc.is_paused():
                vc.resume()
                mark_resumed(state, time.time())
                music_log.info("Bot unmuted -> resume")

    if member == bot.user and before.channel and not after.channel:
        state = get_state(member.guild.id)
        state.queue.clear()
        # by_user=False, deliberat: handler-ul asta NU poate sti cine a provocat
        # deconectarea. Se declanșeaza si cand discord.py rupe singur conexiunea
        # ("We were externally disconnected from voice", close 4014/4022/4021),
        # cand canalul de voce e sters, sau pe calea automata de canal gol. Cu
        # by_user=True, fiecare astfel de eveniment scria autoplay_user_off=True
        # — singurul scriitor al steagului — si de atunci decide_idle_action
        # raspundea "radio oprit de utilizator" pe viata procesului, deci 24/7 nu
        # mai repornea niciodata radioul, invinuind un utilizator care nu facuse
        # nimic. Cine chiar opreste deliberat (!stop, butonul Stop, !247 off) pune
        # deja steagul cu by_user=True inainte de deconectare.
        set_autoplay(state, False, by_user=False)
        # Sesiunea s-a incheiat, deci nici "stai conectat" nu mai are obiect: fara
        # asta rămânea always_on=True pe o stare deconectata.
        state.always_on = False
        state.loop_mode = 0
        state.is_loading = False
        if state.current_file:
            state.current_file = None
            player.trim_cache()
            state.current_file = None
        player.bump_play_generation(state)
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
            await asyncio.sleep(EMPTY_CHANNEL_GRACE_SEC)
            vc = member.guild.voice_client
            if vc and vc.channel == before.channel:
                real = [m for m in before.channel.members if not m.bot]
                # `always_on` se re-verifica DUPA pauza, nu doar inainte: 20 de
                # secunde sunt exact cat ii trebuie cuiva sa dea `!247` vazand ca
                # se goleste canalul, iar varianta care verifica doar la intrare
                # deconecta apoi sesiunea 24/7 pe care el tocmai o pornise.
                if not real and not state.always_on:
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
async def setup_hook():
    """Bataia porneste AICI, nu in on_ready.

    setup_hook e aȘteptat din `login()`, inainte de `connect()`, deci inainte de
    orice READY. Cand pornea la finalul lui on_ready, doua situatii normale
    lasau procesul fara nicio bataie, iar watchdog-ul il omorea sanatos:

    1. Orice excepție mai sus in on_ready. `bot.tree.sync()` prinde doar
       discord.HTTPException, iar o cadere de DNS/TCP da ClientConnectorError
       (un OSError). discord.py inghite excepțiile din handlere de eveniment si
       nu redifuzeaza NICIODATA on_ready la reconectare (parse_resumed trimite
       doar on_resumed), deci bataia nu mai porneste pe viata sesiunii.
    2. O pana la Discord: `connect()` reincearca la infinit, READY nu ajunge
       niciodata, on_ready nu ruleaza. Vechea varianta ieșea cu os._exit(1) la
       fiecare ~5 minute, ardea cele 10 reporniri permise si lasa serviciul jos
       — pentru o pana care nu era a noastra si pe care o repornire nu o repara.

    `_LAST_BEAT` e semanat la import cu momentul pornirii, deci nu exista o
    stare "n-a batut niciodata" confundabila cu "bucla e blocata".
    """
    _start_heartbeat()


def _start_heartbeat():
    """Porneste bataia daca nu bate deja. Idempotenta: se cheama de doua ori."""
    global _heartbeat_task
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = asyncio.get_running_loop().create_task(_heartbeat())
        log.info("Heartbeat pornit")


@bot.event
async def on_ready():
    # Inainte de orice await: chiar daca setup_hook a rulat deja, o bataie oprita
    # (task anulat, excepție scapata) trebuie sa se poata reporni, iar asta nu are
    # voie sa depinda de reusita a nimic de mai jos.
    _start_heartbeat()
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
        # OSError e in lista fiindca o cadere de DNS/TCP da ClientConnectorError,
        # care e un OSError, nu un discord.HTTPException — si scapa ca excepție
        # din handler, adica sare peste tot ce urmeaza aici.
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as e:
            log.warning(f"Sync arbore comenzi esuat: {e}")
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening, name="!help"
            )
        )
    except (discord.HTTPException, OSError, asyncio.TimeoutError) as e:
        # Statusul afisat e cosmetic; nu are voie sa rupa restul lui on_ready.
        log.warning(f"Nu am putut seta statusul: {e}")


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
                                 f"Vezi `!help`.")
    if isinstance(error, commands.BadArgument):
        return await _reply(ctx, "Argument invalid. Vezi `!help`.")
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


# --- Heartbeat, watchdog si curatenie ---------------------------------------
# Un proces viu cu /health 200 nu e repornit niciodata de Railway
# (restartPolicyType = ON_FAILURE, iar healthcheckPath e evaluat doar la deploy).
# Scenariul concret: thread-urile de yt-dlp abandonate dupa timeout se aduna
# peste o seara de reincercari pana cand executorul e epuizat; de atunci fiecare
# cerere aȘteapta un worker care nu se mai elibereaza, `wait_for` expira fara ca
# corpul sa ruleze, iar botul raspunde la orice !play cu mesajul de timeout, pe
# viata containerului.
HEARTBEAT_SEC = 15
# Minim 60s: sub o bataie si ceva, watchdog-ul ar raporta ca blocata o bucla
# perfect sanatoasa si ar reporni procesul la fiecare tick.
WATCHDOG_STALL_SEC = env_num('WATCHDOG_STALL_SEC', 300, low=60)
# Cat timp trebuie sa fie TOATE thread-urile de yt-dlp ocupate de cereri
# abandonate ca sa acceptam ca executorul nu se mai elibereaza. O citire de o
# clipa nu e o defectiune: doua descarcari lente pot depasi bugetul de 240s si
# totusi sa se termine singure la 250s. Peste plafonul de aici nu se mai termina
# niciodata, iar procesul e viu si inutil.
WATCHDOG_SATURATED_SEC = env_num('WATCHDOG_SATURATED_SEC', 300, low=60)
# Fereastra de pornire trebuie sa fie STRICT mai lunga decat pragul de blocaj,
# altfel expira in acelasi tick si nu cumpara nicio margine (vezi _should_restart).
WATCHDOG_BOOT_GRACE_SEC = WATCHDOG_STALL_SEC + 120
SNAPSHOT_EVERY_SEC = 60
SWEEP_EVERY_SEC = 600
_LAST_BEAT = time.monotonic()
_BOOT_MONOTONIC = time.monotonic()


async def _heartbeat():
    """Bate la 15s, reface instantaneul la 60s, curata discul la 10 minute.

    Bataia trebuie sa fie desa, ca watchdog-ul sa distinga repede o bucla
    blocata. Instantaneul nu: el include o sonda catre serverul de PO Token, si
    n-are rost sa il intrebam de patru ori pe minut.
    """
    global _LAST_BEAT
    last_sweep = 0.0
    last_snapshot = 0.0
    while True:
        try:
            _LAST_BEAT = time.monotonic()
            now = time.monotonic()
            if now - last_snapshot >= SNAPSHOT_EVERY_SEC:
                last_snapshot = now
                await diag.refresh(bot, guild_states, bot.loop)
            if now - last_sweep >= SWEEP_EVERY_SEC:
                last_sweep = now
                keep = {st.current_file for st in guild_states.values() if st.current_file}
                await bot.loop.run_in_executor(None, lambda: trim_download_cache(keep))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Un heartbeat care moare ar declanșa watchdog-ul degeaba.
            log.warning("Heartbeat a eșuat", exc_info=True)
        await asyncio.sleep(HEARTBEAT_SEC)


def _saturation_start(now: float, leaked: int, max_workers: int,
                      since: float) -> float:
    """De cand dureaza saturarea ACTUALA a executorului (0 = nu e saturat).

    Separata si pura pentru ca aici sta jumatatea greu de nimerit: momentul
    trebuie sa se PĂSTREZE cat timp saturarea tine (altfel nu se acumuleaza
    niciodata destul) si sa se UITE complet in clipa in care un thread se
    elibereaza (altfel o saturare veche de o ora ar declanșa repornirea la prima
    reapariție, oricat de scurta).
    """
    if max_workers and leaked >= max_workers:
        return since or now
    return 0.0


def _should_restart(now: float, last_beat: float, leaked: int, max_workers: int,
                    boot: float, saturated_since: float = 0.0) -> str | None:
    """Motivul repornirii, sau None. Pura, ca sa poata fi testata fara os._exit.

    Strict la defecte locale, auto-provocate. Niciodata la eșecuri de redare: un
    blocaj YouTube ar lua serviciul complet jos exact cand nu e vina noastra.

    `leaked` trebuie sa fie indicatorul INSTANTANEU (ytdlp.leaked_workers), nu
    totalul de la pornire. Cand era totalul, al doilea timeout din viata
    containerului — o seara normala de reincercari — armă os._exit(1) la fiecare
    tick de 15s, pana la epuizarea celor 10 reporniri permise de Railway, si de
    atunci serviciul rămânea jos.
    """
    if now - boot < WATCHDOG_BOOT_GRACE_SEC:
        # Fereastra de pornire: o problema la boot nu are voie sa arda bugetul de 10
        # reporniri al Railway. Cand fereastra era egala cu pragul de blocaj, nu
        # proteja NIMIC pe ramura de heartbeat: `_LAST_BEAT` si `_BOOT_MONOTONIC`
        # sunt semanate din acelasi `time.monotonic()` (delta masurata 0.0s), deci
        # ambele expirau la acelasi tick — tacere pana la boot+300, os._exit(1) la
        # boot+301, si o cadenta de o auto-omorâre la ~315s de viata a procesului.
        # Cu restartPolicyMaxRetries=10 asta epuiza bugetul in ~52 de minute, iar
        # healthcheckPath e evaluat doar la deploy: serviciul rămânea jos pana cand
        # venea un om.
        return None
    if now - last_beat > WATCHDOG_STALL_SEC:
        return (f'bucla de evenimente blocata: niciun heartbeat de '
                f'{round(now - last_beat)}s')
    if max_workers and leaked >= max_workers and saturated_since:
        held = now - saturated_since
        if held >= WATCHDOG_SATURATED_SEC:
            return (f'toate cele {max_workers} thread-uri de yt-dlp sunt '
                    f'abandonate de {round(held)}s: nicio cerere nu mai poate porni')
    return None


def _watchdog():
    """Thread daemon: iese cu cod nenul cand procesul e viu dar inutil."""
    saturated_since = 0.0
    while True:
        time.sleep(HEARTBEAT_SEC)
        now = time.monotonic()
        leaked, workers = ytdlp_mod.leaked_workers(), ytdlp_mod.MAX_WORKERS
        saturated_since = _saturation_start(now, leaked, workers, saturated_since)
        reason = _should_restart(now, _LAST_BEAT, leaked, workers,
                                _BOOT_MONOTONIC, saturated_since)
        if not reason:
            continue
        log.critical(f"WATCHDOG: {reason}. Ies cu cod 1 ca Railway sa reporneasca.")
        try:
            # Inchiderea curata NU e opționala: fara ea panoul rămâne cu butoane
            # moarte si Discord arata "This interaction failed".
            asyncio.run_coroutine_threadsafe(_shutdown(), bot.loop).result(timeout=10)
        except Exception:
            log.warning("WATCHDOG: inchiderea curata a eșuat", exc_info=True)
        os._exit(1)


class _Health(BaseHTTPRequestHandler):
    server_version = 'gogu'          # fara banner cu versiunea de Python
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = 10                     # o conexiune inactiva nu mai blocheaza thread-ul

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')
        if path == '/status':
            return self._status()
        if path not in ('', '/health'):
            self.send_error(404)
            return
        # /health rămâne text simplu: exact ce aȘteapta Railway, si nimic care sa
        # depinda de cookies sau de rețea. Un jar expirat nu are voie sa blocheze
        # chiar deploy-ul care aduce unul proaspat.
        ready = bool(bot and bot.is_ready() and not bot.is_closed())
        body = b'ok' if ready else b'starting'
        self._respond(200 if ready else 503, 'text/plain', body)

    def _status_authorized(self) -> bool:
        """Token obligatoriu, comparat in timp constant.

        Fail-CLOSED: fara STATUS_TOKEN in env, /status nu exista. Portul de
        healthcheck devine public in clipa in care serviciului i se atașeaza un
        domeniu, iar repo-ul e public — deci calea `/status` e cunoscuta oricui
        citeste codul. Instantaneul nu contine credentiale, dar contine ID-ul
        guild-ului, ce se reda acum, coada, calea fisierului de cookies, versiunile
        bibliotecilor si ultima eroare yt-dlp: informatii operationale care nu au
        ce sa caute la vedere. `!health` in Discord da acelasi lucru, autentificat
        de Discord.
        """
        expected = (os.getenv('STATUS_TOKEN') or '').strip()
        if not expected:
            return False
        supplied = (self.headers.get('X-Status-Token')
                    or parse_qs(urlparse(self.path).query).get('token', [''])[0]
                    or '')
        # Pe bytes, nu pe str: compare_digest arunca TypeError la orice caracter
        # non-ASCII, deci un token cu diacritice ar fi crapat handler-ul in loc
        # sa dea 404.
        return hmac.compare_digest(supplied.encode('utf-8', 'replace'),
                                   expected.encode('utf-8', 'replace'))

    def _status(self):
        if not self._status_authorized():
            # 404, nu 401: un 401 confirma ca endpoint-ul exista.
            self.send_error(404)
            return
        # DOAR din cache: serverul e single-threaded pe un thread daemon, deci un
        # apel blocant aici ar intarzia fiecare sonda urmatoare, inclusiv
        # healthcheck-ul.
        snapshot = diag.cached()
        if not snapshot:
            self._respond(503, 'application/json', b'{"error":"no snapshot yet"}')
            return
        payload = {**snapshot, 'problems': diag.problems(snapshot)}
        body = json.dumps(payload, default=str).encode('utf-8')
        self._respond(200, 'application/json', body)

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def _shutdown():
    """Inchide curat: sterge panoul, iese din voce, apoi inchide gateway-ul."""
    log.info("Opresc botul (SIGTERM/SIGINT)...")
    # View-urile au timeout=None dar nu sunt inregistrate ca persistente, deci
    # dupa repornire butoanele vechi nu mai sunt ascultate de nimeni si Discord
    # arata "This interaction failed". Le luam de pe masa la oprire.
    for state in list(guild_states.values()):
        if state.current_view is not None:
            try:
                state.current_view.stop()
            except Exception:
                pass
        await safe_delete(state.current_msg)
        state.current_msg = None
        state.current_view = None
        # La oprire NU stergem audio-ul: pe volum e cache, iar urmatoarea
        # pornire il gaseste si redarea aceleiasi piese e gratuita.
        state.current_file = None
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    await bot.close()


async def _runner():
    # discord.py nu instaleaza handler de SIGTERM, deci la fiecare redeploy
    # Railway omora procesul brusc, fara sa inchida conexiunea de voce — de
    # aceea botul aparea uneori inca "in canal" dupa repornire.
    loop = asyncio.get_running_loop()
    for sig in ('SIGTERM', 'SIGINT'):
        try:
            import signal
            loop.add_signal_handler(getattr(signal, sig),
                                    lambda: asyncio.create_task(_shutdown()))
        except (NotImplementedError, AttributeError, RuntimeError):
            pass  # Windows nu suporta add_signal_handler pentru SIGTERM
    async with bot:
        await bot.start(TOKEN)


def main():
    if not TOKEN:
        log.error("DISCORD_TOKEN not set.")
        sys.exit(1)
    # Efectele pe disc si cererile de rețea se fac AICI, nu la import: un
    # `import bot` (teste, unelte, un REPL) nu are voie sa atinga volumul.
    _prepare_volume()
    _run_startup_probe()
    port = env_num('PORT', 8080, low=1, high=65535)
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), _Health).serve_forever(),
        daemon=True,
    ).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    log.info("Connecting to Discord Gateway...")
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
