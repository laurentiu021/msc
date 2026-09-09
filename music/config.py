"""Constante si configurare muzica."""
import copy
import hashlib
import os
import logging

import yt_dlp

log = logging.getLogger('gogu.music')

DOWNLOAD_DIR = 'downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BLACKLIST = [
    "jazz", "piano", "relaxing", "chill", "lofi", "ambient",
    "meditation", "blues", "8d", "remix", "slowed", "reverb",
    "asmr", "karaoke", "instrumental", "tutorial",
]

def yt_client_args(*clients):
    """Construieste extractor_args pentru API-ul Python al yt-dlp.

    ATENTIE, capcana: API-ul cere dict-de-dict. Forma de linie de comanda
    ({'youtube': 'player_client=mweb'}) e primita fara nicio eroare, dar
    ignorata complet, iar yt-dlp foloseste clientii impliciti. Din cauza ei
    lanturile de clienti de mai jos rulau toate acelasi set implicit,
    multiplicand cererile degeaba si provocand 429 si 403.
    Vezi tests/test_extractor_args.py.
    """
    return {'youtube': {'player_client': list(clients)}}


# Doar clienti pentru care bgutil poate emite PO Token, adica familia web:
# MWEB, WEB, TVHTML5, WEB_EMBEDDED_PLAYER, WEB_REMIX, WEB_CREATOR
# (yt_dlp.extractor.youtube.pot.utils.WEBPO_CLIENTS).
# android_vr / ios sunt inutilizabile aici: din yt-dlp 2026.08 cer si ele GVS PO
# Token, iar acela vine din DroidGuard/iOSGuard, nu din BotGuard-ul lui bgutil.
# Fara token, formatele lor sunt pur si simplu omise.
#
# WEB_CLIENTS e singura definitie a clientilor folositi la runtime — atat pentru
# opts-urile de mai jos, cat si pentru lanturile din player.py. Cand era duplicata
# in player.py, testul verifica doar copia din config si o schimbare in player
# trecea nedetectata. tests/test_extractor_args.py verifica acum ambele.
WEB_CLIENTS = ('mweb', 'web_safari')

_YT_EXTRACTOR_ARGS = yt_client_args(*WEB_CLIENTS)

# Proxy optional — setat via env var YT_PROXY (ex: socks5://host:port)
_proxy = os.getenv('YT_PROXY')

# Traffic shaping — delay minim intre cereri YouTube consecutive
YT_REQUEST_MIN_INTERVAL_SEC = float(os.getenv('YT_REQUEST_MIN_INTERVAL_SEC', '1.2'))
YT_REQUEST_MAX_INTERVAL_SEC = float(os.getenv('YT_REQUEST_MAX_INTERVAL_SEC', '3.2'))

# Guest mode + PO Token (fara cookies — mai rapid si mai stabil)
YDL_OPTS_SEARCH = {
    'noplaylist': True,
    'quiet': False,
    'no_warnings': False,
    'default_search': 'ytsearch5',
    'source_address': '0.0.0.0',
    'socket_timeout': 10,
    'skip_download': True,
    'format': 'best',
    'ignore_no_formats_error': True,
    'extractor_args': _YT_EXTRACTOR_ARGS,
}

MAX_TRACK_SECONDS = 660
MIN_TRACK_SECONDS = 30
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

YDL_OPTS_DOWNLOAD = {
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
    # Refuza live-urile si piesele absurd de lungi INAINTE de descarcare.
    'match_filter': yt_dlp.utils.match_filter_func(
        f'!is_live & !live_from_start & duration < {MAX_TRACK_SECONDS}'
    ),
    'max_filesize': MAX_DOWNLOAD_BYTES,
    'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'source_address': '0.0.0.0',
    'retries': 3,
    'socket_timeout': 15,
    'extractor_args': _YT_EXTRACTOR_ARGS,
}

if _proxy:
    YDL_OPTS_SEARCH['proxy'] = _proxy
    YDL_OPTS_DOWNLOAD['proxy'] = _proxy
    YDL_OPTS_SEARCH['socket_timeout'] = 30
    YDL_OPTS_DOWNLOAD['socket_timeout'] = 30
    log.info("YouTube proxy configured for yt-dlp (value hidden)")

# Cookies disponibile ca fallback (pt content care cere cont: age-restricted, privat)
_cookies_path = None

# Directorul volumului persistent, daca exista. yt-dlp rescrie fisierul de
# cookies cu valorile rotite de YouTube (__Secure-1PSIDTS si SIDCC se schimba
# des). Pe disc efemer rotatia se pierde la fiecare restart si se revine la
# valorile din env, care imbatranesc pana cand YouTube le refuza. Pe volum,
# rotatia supravietuieste si cookie-urile tin mult mai mult.
COOKIE_DIR = os.getenv('COOKIE_DIR', '/data')


def _cookie_file_paths():
    """(fisier_cookies, fisier_amprenta) — pe volum daca exista, altfel local."""
    base = COOKIE_DIR if os.path.isdir(COOKIE_DIR) else '.'
    return os.path.join(base, 'cookies.txt'), os.path.join(base, '.cookies_seed')


def seed_cookies_from_env(raw: str) -> tuple[str | None, int]:
    """Scrie cookie-urile din env, dar NU peste o versiune rotita de yt-dlp.

    Reseed-ul se face doar cand valoarea din env s-a schimbat efectiv, detectat
    prin amprenta. Altfel un restart ar arunca rotatia si ne-am intoarce la
    cookie-uri vechi. Returneaza (path, numar_intrari).
    """
    if not raw:
        return None, 0
    raw = raw.replace('\\n', '\n').replace('\\t', '\t')
    path, seed_path = _cookie_file_paths()
    fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()

    previous = None
    if os.path.exists(seed_path):
        try:
            with open(seed_path, encoding='utf-8') as fh:
                previous = fh.read().strip()
        except OSError:
            previous = None

    entries = len([l for l in raw.strip().splitlines()
                   if l.strip() and not l.startswith('#')])

    if previous == fingerprint and os.path.exists(path):
        rotated = len([l for l in open(path, encoding='utf-8', errors='replace')
                       .read().strip().splitlines()
                       if l.strip() and not l.startswith('#')])
        log.info(f"Cookies pastrate din {path} ({rotated} intrari, rotite de yt-dlp); "
                 f"env neschimbat")
        return path, rotated

    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(raw if raw.endswith('\n') else raw + '\n')
        with open(seed_path, 'w', encoding='utf-8') as fh:
            fh.write(fingerprint)
    except OSError as e:
        log.error(f"Nu pot scrie cookies in {path}: {e}")
        return None, 0

    log.info(f"Cookies scrise in {path} ({entries} intrari) din env"
             f"{' — valoare noua, reseed' if previous else ''}")
    return path, entries


def apply_cookies(path: str | None = None):
    """Salveaza path-ul cookies — dar NU le aplica by default."""
    global _cookies_path
    candidate = path or _cookie_file_paths()[0]
    if not os.path.exists(candidate) and os.path.exists('cookies.txt'):
        candidate = 'cookies.txt'
    if os.path.exists(candidate):
        _cookies_path = candidate
        log.info(f"YouTube cookies available as fallback: {candidate}")


def cookies_available() -> bool:
    return _cookies_path is not None


def make_search_opts(with_cookies: bool = False, **overrides) -> dict:
    """Opts de CAUTARE proaspete la fiecare apel.

    Copie adanca, intentionat: YoutubeDL MUTEAZA dict-ul primit (ii injecteaza
    outtmpl, http_headers, js_runtimes si altele). Cu .copy() superficial,
    YDL_OPTS_SEARCH — care nu are deliberat outtmpl — capata template-ul implicit
    al yt-dlp, relativ la CWD, si fiecare consumator il moștenea. La fel,
    extractor_args era UN singur obiect partajat intre toate apelurile.
    """
    opts = copy.deepcopy(YDL_OPTS_SEARCH)
    if with_cookies and _cookies_path:
        opts['cookiefile'] = _cookies_path
    opts.update(overrides)
    return opts


def make_download_opts(with_cookies: bool = False, **overrides) -> dict:
    """Opts de DESCARCARE proaspete la fiecare apel (vezi make_search_opts)."""
    opts = copy.deepcopy(YDL_OPTS_DOWNLOAD)
    if with_cookies and _cookies_path:
        opts['cookiefile'] = _cookies_path
    opts.update(overrides)
    return opts


def get_opts_with_cookies():
    """(search, download) cu cookies, sau (None, None) daca nu avem."""
    if not _cookies_path:
        return None, None
    return make_search_opts(with_cookies=True), make_download_opts(with_cookies=True)


def is_playable_audio_format(f: dict) -> bool:
    """Format din care se poate chiar cânta.

    Verificarea trebuie sa fie stricta. Cand era permisiva, un singur format
    inutilizabil raportat ca "real" declara clientul reusit, scurtcircuita
    lantul si apoi TOATE download-urile eseuau — exact ce a facut android_vr
    fara GVS PO Token: "5 formats (1 real)", urmat de zero descarcari.
    """
    if f.get('has_drm'):
        return False
    if not (f.get('url') or f.get('fragments') or f.get('manifest_url')):
        return False
    if 'storyboard' in (f.get('format_note') or '').lower():
        return False
    if f.get('acodec', 'none') != 'none':
        return True
    # HLS muxat: audio e in stream chiar daca acodec lipseste din metadata.
    return 'm3u8' in (f.get('protocol') or '') and f.get('vcodec', 'none') != 'none'


def count_real_formats(formats_list: list) -> int:
    return sum(1 for f in (formats_list or []) if is_playable_audio_format(f))


def has_real_formats(formats_list: list) -> bool:
    """Verifica daca lista contine cel putin un format redabil."""
    return count_real_formats(formats_list) > 0


FFMPEG_OPTS = {
    'options': '-vn -b:a 128k -ar 48000 -ac 2',
}
