"""Constante si configurare muzica."""
import os
import logging

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
# Fara token, formatele lor sunt pur si simplu omise. Testul din
# tests/test_extractor_args.py refuza orice client din afara listei.
_YT_EXTRACTOR_ARGS = yt_client_args('mweb', 'web_safari')

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
    'default_search': 'ytsearch',
    'nocheckcertificate': True,
    'source_address': '0.0.0.0',
    'socket_timeout': 10,
    'skip_download': True,
    'format': 'best',
    'ignore_no_formats_error': True,
    'extractor_args': _YT_EXTRACTOR_ARGS,
}

YDL_OPTS_DOWNLOAD = {
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
    'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'nocheckcertificate': True,
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

def apply_cookies():
    """Salveaza path-ul cookies — dar NU le aplica by default."""
    global _cookies_path
    if os.path.exists('cookies.txt'):
        _cookies_path = 'cookies.txt'
        log.info("YouTube cookies available as fallback (not applied by default)")


def get_opts_with_cookies():
    """Returneaza opts CU cookies — fallback pt age-restricted/privat."""
    if not _cookies_path:
        return None, None
    search = dict(YDL_OPTS_SEARCH)
    search['cookiefile'] = _cookies_path
    download = dict(YDL_OPTS_DOWNLOAD)
    download['cookiefile'] = _cookies_path
    return search, download


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
