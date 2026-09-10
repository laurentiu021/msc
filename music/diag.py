"""Ce se strica de fapt, adunat intr-un singur loc.

`!debug` raporta latenta, CPU, RAM si lungimea cozii — adica niciunul dintre
lucrurile care chiar cad. Procesul stie insa toate raspunsurile: ultima eroare
bruta de la yt-dlp, cate erori consecutive, cat mai tine intrerupatorul, daca
avem cookies si de cand, daca serverul de PO Token raspunde, cate thread-uri de
yt-dlp au fost abandonate dupa timeout, cata cota de API am ars azi. Pana acum,
drumul de la "nu cânta" la "care din cele patru cauze" trecea prin logurile
Railway deschise pe telefon.

Modulul nu importa discord: intra stare, iese un dict. Formatarea pentru Discord
sta in commands.py, iar `/status` serveste acelasi dict ca JSON.
"""
import os
import shutil
import time
import urllib.error
import urllib.request

import yt_dlp

from music import ytdlp
from music import youtube_api as yt_api
from music.config import (COOKIE_DIR, DOWNLOAD_DIR, MAX_TRACK_SECONDS,
                          cookie_status, cookies_available, log)

START_TS = time.time()

POT_URL = f"http://127.0.0.1:{os.getenv('POT_PORT', '4416')}/ping"

# Ultimul instantaneu calculat, servit de /status. Serverul HTTP e
# single-threaded pe un thread daemon, deci un apel blocant din handler ar
# intarzia fiecare sonda urmatoare, inclusiv healthcheck-ul Railway.
_CACHE: dict = {}


def scrub(text) -> str:
    """Scoate URL-ul de proxy din text inainte sa ajunga la un om.

    yt-dlp include proxy-ul configurat in mesajele lui de eroare, iar YT_PROXY
    poate conține user:parola. Definitia sta aici, nu in player.py, pentru ca
    acum si !health si /status arata acelasi text.
    """
    out = str(text)
    proxy = os.getenv('YT_PROXY')
    if proxy:
        out = out.replace(proxy, '<proxy>')
        host = proxy.split('@')[-1]
        if host and host != proxy:
            out = out.replace(host, '<proxy>')
    return out


def pot_ping(timeout: float = 2.0) -> tuple[bool, str]:
    """(raspunde, detaliu) pentru serverul de PO Token. Blocant: rulează in executor."""
    try:
        with urllib.request.urlopen(POT_URL, timeout=timeout) as resp:
            return True, resp.read(120).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)[:80]


def _remaining(deadline: float, now: float) -> int:
    return max(0, round(deadline - now)) if deadline else 0


def build(bot, guild_states, *, now=None, pot=None) -> dict:
    """Instantaneul complet. `now` si `pot` se injecteaza, ca sa fie testabil."""
    now = time.time() if now is None else now
    pot_ok, pot_detail = pot if pot is not None else (None, 'neverificat')

    try:
        free_bytes = shutil.disk_usage(DOWNLOAD_DIR).free
    except OSError:
        free_bytes = None

    downloads = 0
    try:
        downloads = len(os.listdir(DOWNLOAD_DIR))
    except OSError:
        pass

    guilds = {}
    for guild_id, state in (guild_states or {}).items():
        guilds[str(guild_id)] = {
            'playing_title': state.last_title or None,
            'queue': len(state.queue),
            'history': len(state.history),
            'autoplay': state.autoplay,
            'autoplay_user_off': state.autoplay_user_off,
            'always_on': state.always_on,
            'loop_mode': state.loop_mode,
            'is_loading': state.is_loading,
            'play_generation': state.play_generation,
            'consecutive_errors': state._consecutive_errors,
            'consecutive_rejects': state._consecutive_rejects,
            'breaker_sec_left': _remaining(state.breaker_until, now),
            'quiet_sec_left': _remaining(state.idle_quiet_until, now),
            'idle_timer_armed': bool(state.timeout_task
                                     and not state.timeout_task.done()),
            'last_idle_reason': state.last_idle_reason or None,
            'last_error': scrub(state.last_raw_error)[:200] if state.last_raw_error else None,
        }

    return {
        'ok': bool(bot and bot.is_ready() and not bot.is_closed()) and pot_ok is not False,
        'uptime_sec': max(0, round(now - START_TS)),
        'commit': (os.getenv('RAILWAY_GIT_COMMIT_SHA') or '')[:7] or None,
        'versions': {
            'yt_dlp': yt_dlp.version.__version__,
            'discord': _discord_version(),
        },
        'discord': {
            'ready': bool(bot and bot.is_ready()),
            'closed': bool(bot and bot.is_closed()),
            'latency_ms': _latency_ms(bot),
            'guilds': len(getattr(bot, 'guilds', []) or []),
        },
        'pot_server': {'ok': pot_ok, 'detail': pot_detail, 'url': POT_URL},
        'cookies': {**cookie_status(), 'in_use': cookies_available(),
                    'dir': COOKIE_DIR},
        'ytdlp': {
            'leaked_workers': ytdlp.leaked_workers(),
            'max_workers': ytdlp.MAX_WORKERS,
            'throttle_sec_left': _remaining(ytdlp._NEXT_ALLOWED_AT, now),
            'extract_budget_sec': ytdlp.EXTRACT_TIMEOUT_SEC,
            'download_budget_sec': ytdlp.DOWNLOAD_TIMEOUT_SEC,
        },
        'data_api': {
            'available': yt_api.is_available(),
            'units_spent': yt_api.units_spent(),
            'daily_cap': yt_api.DAILY_UNIT_CAP,
        },
        'disk': {'downloads': downloads,
                 'free_mb': round(free_bytes / 1024 / 1024) if free_bytes else None},
        'limits': {'max_track_sec': MAX_TRACK_SECONDS},
        'guilds_detail': guilds,
    }


def _discord_version() -> str:
    try:
        import discord
        return discord.__version__
    except Exception:
        return 'necunoscut'


def _latency_ms(bot):
    latency = getattr(bot, 'latency', None)
    if not latency or latency == float('inf'):
        return None
    return round(latency * 1000, 1)


def store(snapshot: dict) -> None:
    """Reține instantaneul pentru /status, cu momentul calculului."""
    global _CACHE
    _CACHE = {**snapshot, 'generated_at': time.time()}


def cached() -> dict:
    """Ultimul instantaneu. Gol daca inca nu a rulat niciun heartbeat."""
    return dict(_CACHE)


def problems(snapshot: dict) -> list[str]:
    """Doar ce e in neregula, in ordinea in care conteaza.

    Exista ca sa nu fie nevoie sa citesti 30 de campuri pentru a afla daca e
    ceva rau: prima linie din `!health` trebuie sa fie un verdict.
    """
    out = []
    if not snapshot:
        return ['niciun instantaneu inca']
    if snapshot['pot_server']['ok'] is False:
        out.append(f"PO Token server nu raspunde ({snapshot['pot_server']['detail']})")
    cookies = snapshot['cookies']
    if not cookies['exists'] or not cookies['entries']:
        out.append('fara cookies: YouTube va cere verificare de bot')
    elif not cookies.get('valid', True):
        out.append(f"cookies fara sesiune valida (lipsesc: "
                   f"{', '.join(cookies.get('missing_critical') or [])})")
    elif cookies['age_sec'] and cookies['age_sec'] > 7 * 24 * 3600:
        out.append(f"cookies nerotite de {cookies['age_sec'] // 86400} zile")
    ytdlp_info = snapshot['ytdlp']
    if ytdlp_info['leaked_workers'] >= ytdlp_info['max_workers']:
        out.append(f"toate cele {ytdlp_info['max_workers']} thread-uri de yt-dlp "
                   f"sunt abandonate: cererile nu mai pornesc")
    elif ytdlp_info['leaked_workers']:
        out.append(f"{ytdlp_info['leaked_workers']} thread-uri de yt-dlp abandonate")
    api = snapshot['data_api']
    if api['units_spent'] >= api['daily_cap']:
        out.append('cota Data API epuizata pe ziua de azi')
    if not snapshot['discord']['ready']:
        out.append('nu suntem conectati la Discord')
    free_mb = snapshot['disk']['free_mb']
    if free_mb is not None and free_mb < 100:
        out.append(f'doar {free_mb}MB liberi pe disc')
    for guild_id, guild in snapshot['guilds_detail'].items():
        if guild['breaker_sec_left']:
            out.append(f"guild {guild_id}: intrerupator activ inca "
                       f"{guild['breaker_sec_left']}s")
        if guild['consecutive_errors']:
            out.append(f"guild {guild_id}: {guild['consecutive_errors']} erori consecutive")
    return out


async def refresh(bot, guild_states, loop) -> dict:
    """Calculeaza si retine un instantaneu. Sonda de rețea merge in executor."""
    pot = await loop.run_in_executor(None, pot_ping)
    snapshot = build(bot, guild_states, pot=pot)
    store(snapshot)
    for line in problems(snapshot):
        log.debug(f"diag: {line}")
    return snapshot
