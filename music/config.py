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

# PO Token provider pe port 4416 (bgutil in acelasi container)
# Chain de clienti: mweb (cu PO Token) -> android_vr (fara nimic) -> tv (cu cookies)
_YT_EXTRACTOR_ARGS = {
    'youtube': 'player_client=mweb,android_vr,tv',
}

# Proxy optional — setat via env var YT_PROXY (ex: socks5://host:port)
_proxy = os.getenv('YT_PROXY')

# Guest mode + PO Token (fara cookies — mai rapid si mai stabil)
YDL_OPTS_SEARCH = {
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
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
    log.info(f"YouTube proxy configured: {_proxy[:20]}...")

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


def has_real_formats(formats_list: list) -> bool:
    """Verifica daca lista de formate contine formate audio/video reale."""
    for f in formats_list:
        if f.get('acodec', 'none') != 'none' or f.get('vcodec', 'none') != 'none':
            if 'storyboard' not in f.get('format_note', '').lower():
                return True
    return False


FFMPEG_OPTS = {
    'options': '-vn -b:a 128k -ar 48000 -ac 2',
}
