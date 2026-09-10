"""Constante si configurare muzica."""
import copy
import hashlib
import logging
import os
import time

import yt_dlp

log = logging.getLogger('gogu.music')


def env_num(name: str, default, *, low=None, high=None, cast=int):
    """Un numar dintr-un env var, cu limite. Singura cale de citire numerica.

    Toate valorile astea se scriu de mana in interfata Railway, unde nu exista
    nicio validare si nici un mesaj de eroare. Cu `int(os.getenv(...))` gol, o
    scapare de tastatura ("30O", un spatiu, o virgula in loc de punct) devenea un
    ValueError la IMPORT, adica un container care nu porneste deloc si un bot
    disparut fara nicio linie de log care sa spuna de ce.

    `low` conteaza la fel de mult ca formatul: un 0 acceptat in WATCHDOG_STALL_SEC
    facea din watchdog o bucla de repornire instantanee, iar un 0 in
    DOWNLOAD_CACHE_MB stergea fiecare piesa imediat dupa descarcare.
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = default
    else:
        try:
            value = cast(str(raw).strip())
        except (TypeError, ValueError):
            log.warning(f"{name}={raw!r} nu e un numar; folosesc {default}")
            value = default
    if low is not None and value < low:
        log.warning(f"{name}={value} sub minimul {low}; folosesc {low}")
        value = low
    if high is not None and value > high:
        log.warning(f"{name}={value} peste maximul {high}; folosesc {high}")
        value = high
    return value


# Volumul persistent, daca exista. Definit inainte de DOWNLOAD_DIR pentru ca si
# audio-ul si cache-ul yt-dlp trebuie sa ajunga pe el.
COOKIE_DIR = os.getenv('COOKIE_DIR', '/data')
_ON_VOLUME = os.path.isdir(COOKIE_DIR)

# Pe volum cand exista: un fisier deja descarcat inseamna zero cereri catre
# YouTube, zero octeti de media, zero rulari de Deno, niciun slot de throttle si
# nicio expunere la 429 sau la cookie-uri expirate. Pe disc efemer, fiecare
# deploy pierdea tot ce se ascultase.
DOWNLOAD_DIR = os.path.join(COOKIE_DIR, 'audio') if _ON_VOLUME else 'downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Cache-ul propriu al yt-dlp (semnaturi, provocari EJS). Fara cachedir explicit
# ajunge in ~/.cache/yt-dlp, efemer in container, deci prima piesa de dupa fiecare
# deploy platea din nou rezolvarea semnaturii.
YTDLP_CACHE_DIR = os.path.join(COOKIE_DIR, 'ytdlp-cache') if _ON_VOLUME else None

# Cat audio pastram inainte sa stergem cele mai vechi fisiere.
# Minim 50MB: sub atat evacuarea ar sterge piesa curenta imediat dupa ce a
# fost descarcata, deci fiecare redare ar plati din nou transferul.
DOWNLOAD_CACHE_BYTES = env_num('DOWNLOAD_CACHE_MB', 300, low=50) * 1024 * 1024

BLACKLIST = [
    "jazz", "piano", "relaxing", "chill", "lofi", "ambient",
    "meditation", "blues", "8d", "remix", "slowed", "reverb",
    "asmr", "karaoke", "instrumental", "tutorial",
]

class _YdlLog:
    """Logger pentru yt-dlp, ca deciziile lui sa nu mai fie invizibile.

    Cu quiet=True, yt-dlp raporteaza respingerile de filtru prin `to_screen`,
    care nu scrie nimic — deci o piesa refuzata pentru durata dispărea complet:
    nicio excepție, nicio linie in log, iar utilizatorului i se arata eroarea
    altei piese. Cu un logger, `to_screen` merge la `debug`, deci ridicam la
    nivel de INFO exact liniile care explica o decizie.
    """

    _PROMOTE = ('does not pass filter', 'larger than max-filesize',
                'skipping', 'Sign in to confirm', 'not available',
                'has already been downloaded')

    # Ultima linie care explica o decizie. Cand yt-dlp refuza o piesa prin
    # match_filter nu ridica nicio excepție, deci fara asta singurul motiv
    # disponibil era o presupunere scrisa de noi.
    last_reason: str | None = None

    def debug(self, msg):
        # Liniile de downloader vin cu \r in fata (sunt gandite pentru terminal,
        # ca sa se suprascrie una pe alta); intr-un log de linii ar tăia prefixul
        # propriu al inregistrarii.
        text = str(msg).strip('\r\n')
        # removeprefix, nu lstrip: lstrip primeste un SET de caractere, deci
        # lstrip('[debug] ') mânca si din "[download] File is larger" pana la
        # primul caracter din afara setului si scotea "ownload] File is larger".
        text = text.removeprefix('[debug] ')
        if any(marker in text for marker in self._PROMOTE):
            _YdlLog.last_reason = text
            log.info(f"yt-dlp: {text}")
        else:
            log.debug(f"yt-dlp: {text}")

    def info(self, msg):
        log.info(f"yt-dlp: {msg}")

    def warning(self, msg):
        _YdlLog.last_reason = str(msg)
        log.warning(f"yt-dlp: {msg}")

    def error(self, msg):
        _YdlLog.last_reason = str(msg)
        log.error(f"yt-dlp: {msg}")


YDL_LOGGER = _YdlLog()


def clear_ydl_reason():
    """Uita ultimul motiv, inainte de o incercare noua.

    Fara golire, motivul unei piese de acum o ora ar fi raportat ca explicatie
    pentru piesa curenta — exact bug-ul pe care state.last_raw_error il avea.
    """
    _YdlLog.last_reason = None


def last_ydl_reason() -> str | None:
    """Ultima decizie explicata de yt-dlp de la ultima golire."""
    return _YdlLog.last_reason


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
YT_REQUEST_MIN_INTERVAL_SEC = env_num('YT_REQUEST_MIN_INTERVAL_SEC', 1.2,
                                      low=0.0, cast=float)
YT_REQUEST_MAX_INTERVAL_SEC = env_num('YT_REQUEST_MAX_INTERVAL_SEC', 3.2,
                                      low=0.0, cast=float)

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
    'logger': YDL_LOGGER,
    'cachedir': YTDLP_CACHE_DIR or True,
}

MAX_TRACK_SECONDS = 660
MIN_TRACK_SECONDS = 30
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# Plafon separat pentru incercarea HLS. Chiar si cerand bestaudio, o redare HLS
# poate fi muxata; 30MB acopera orice piesa rezonabila si opreste din start un
# transfer de video pe un bot audio.
HLS_MAX_BYTES = 30 * 1024 * 1024

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
    # Cu un logger atasat, to_screen NU mai respecta quiet: iese direct pe
    # logger.debug. Fara asta, fiecare tick de progres al descarcarii ar deveni
    # un apel de logging formatat degeaba, de zeci de ori pe piesa.
    'noprogress': True,
    'source_address': '0.0.0.0',
    'retries': 3,
    'socket_timeout': 15,
    'extractor_args': _YT_EXTRACTOR_ARGS,
    'logger': YDL_LOGGER,
    'cachedir': YTDLP_CACHE_DIR or True,
}

if _proxy:
    YDL_OPTS_SEARCH['proxy'] = _proxy
    YDL_OPTS_DOWNLOAD['proxy'] = _proxy
    YDL_OPTS_SEARCH['socket_timeout'] = 30
    YDL_OPTS_DOWNLOAD['socket_timeout'] = 30
    log.info("YouTube proxy configured for yt-dlp (value hidden)")

# Cookies disponibile ca fallback (pt content care cere cont: age-restricted, privat)
_cookies_path = None

# COOKIE_DIR e definit sus, langa DOWNLOAD_DIR. yt-dlp rescrie fisierul de
# cookies cu valorile rotite de YouTube (__Secure-1PSIDTS si SIDCC se schimba
# des). Pe disc efemer rotatia se pierde la fiecare restart si se revine la
# valorile din env, care imbatranesc pana cand YouTube le refuza. Pe volum,
# rotatia supravietuieste si cookie-urile tin mult mai mult.


def _cookie_file_paths():
    """(fisier_cookies, fisier_amprenta) — pe volum daca exista, altfel local."""
    base = COOKIE_DIR if os.path.isdir(COOKIE_DIR) else '.'
    return os.path.join(base, 'cookies.txt'), os.path.join(base, '.cookies_seed')


def _cookie_lines(source: str, *, is_text: bool = False) -> list[str]:
    """Liniile utile din jar — un singur parser, folosit de tot ce il citeste."""
    if is_text:
        text = source or ''
    else:
        try:
            with open(source, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError:
            return []
    return [l for l in text.strip().splitlines()
            if l.strip() and not l.startswith('#')]


def _count_cookie_entries(path: str) -> int:
    return len(_cookie_lines(path))


# Cookie-urile fara care o sesiune Google nu mai autentifica nimic. Daca lipsesc
# toate, fisierul nu e "cookies rotite", e un fisier rupt.
COOKIE_CRITICAL = ('__Secure-1PSID', '__Secure-3PSID', 'SAPISID', 'SID')

_GOOD_SUFFIX = '.good'
_rolled_back = False


def cookie_health(path: str) -> dict:
    """{entries, present, missing, earliest_expiry} pentru un fisier Netscape.

    Exista pentru ca ramura care pastreaza fisierul de pe disc il pastra ORICUM:
    un jar trunchiat la 3 linii era raportat vesel drept "3 intrari, rotite de
    yt-dlp" si pastrat pentru totdeauna, iar singura reparatie era un om care
    edita YT_COOKIES_CONTENT pe Railway.
    """
    names = set()
    expiries = []
    lines = _cookie_lines(path)
    for line in lines:
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        names.add(parts[5])
        try:
            expiry = int(parts[4])
        except ValueError:
            continue
        if expiry > 0:
            expiries.append(expiry)
    return {
        'entries': len(lines),
        'present': sorted(n for n in names if n in COOKIE_CRITICAL),
        'missing': sorted(set(COOKIE_CRITICAL) - names),
        'earliest_expiry': min(expiries) if expiries else None,
    }


def cookies_valid(path: str) -> bool:
    """Mai poate autentifica? Cel putin un cookie critic de sesiune."""
    if not os.path.exists(path):
        return False
    health = cookie_health(path)
    return bool(health['entries'] and health['present'])


def _write_private(path: str, text: str) -> None:
    """Scrie atomic si cu drepturi 0600.

    Temp + os.replace: scrierea directa are o fereastra in care fisierul e
    trunchiat, iar un redeploy exact in acel moment lasa un jar scurt pe volum.
    0600 pentru ca o sesiune Google activa nu are ce cauta lizibila de altcineva.
    """
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text if text.endswith('\n') else text + '\n')
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def promote_cookies() -> bool:
    """Marcheaza jar-ul curent drept ultimul bun cunoscut. True daca s-a copiat."""
    path = _cookies_path or _cookie_file_paths()[0]
    if not cookies_valid(path):
        return False
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        _write_private(path + _GOOD_SUFFIX, text)
        return True
    except OSError as e:
        log.debug(f"Nu am putut promova cookie-urile: {e}")
        return False


def rollback_cookies() -> bool:
    """Revine la ultimul jar bun. Cel mult o data pe proces.

    Rotatia scrisa de yt-dlp e de obicei buna, dar cand YouTube invalideaza
    sesiunea scrie peste fisier valori care nu mai autentifica. Fara revenire,
    singura reparatie era un om care lipea cookie-uri noi pe Railway.
    """
    global _rolled_back
    if _rolled_back:
        return False
    path = _cookies_path or _cookie_file_paths()[0]
    good = path + _GOOD_SUFFIX
    if not cookies_valid(good):
        return False
    try:
        with open(good, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        _write_private(path, text)
    except OSError as e:
        log.warning(f"Revenirea la cookie-urile bune a eșuat: {e}")
        return False
    _rolled_back = True
    log.warning(f"Cookies revenite la ultima versiune buna ({good})")
    return True


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

    entries = len(_cookie_lines(raw, is_text=True))

    if previous == fingerprint and os.path.exists(path):
        # Verificarea de validitate vine INAINTE de a pastra fisierul. Ramura
        # asta il pastra necondiționat, deci un jar trunchiat (de o scriere
        # intrerupta, sau de vechea trunchiere la timeout) supravietuia pentru
        # totdeauna, raportat drept "rotite de yt-dlp".
        if cookies_valid(path):
            health = cookie_health(path)
            log.info(f"Cookies pastrate din {path} ({health['entries']} intrari, "
                     f"critice: {', '.join(health['present'])}); env neschimbat")
            return path, health['entries']
        if rollback_cookies() and cookies_valid(path):
            rotated = _count_cookie_entries(path)
            log.warning(f"Jar-ul de pe disc era rupt; am revenit la copia buna "
                        f"({rotated} intrari)")
            return path, rotated
        log.warning(f"Jar-ul din {path} nu mai are cookie-uri de sesiune "
                    f"(lipsesc: {', '.join(cookie_health(path)['missing'])}); "
                    f"rescriu din env")

    try:
        _write_private(path, raw)
        _write_private(seed_path, fingerprint)
        # Prima copie buna: fara ea, o revenire nu are unde sa se intoarca.
        if cookies_valid(path):
            promote_cookies()
    except OSError as e:
        log.error(f"Nu pot scrie cookies in {path}: {e}")
        # Daca pe volum exista deja cookie-uri, le pastram. Varianta care
        # intorcea (None, 0) pornea botul ca guest, adica singura configuratie
        # fara nicio cale functionala, desi avea cookie-uri bune pe disc.
        if os.path.exists(path):
            log.warning(f"Folosesc cookie-urile existente din {path}")
            return path, _count_cookie_entries(path)
        return None, 0

    log.info(f"Cookies scrise in {path} ({entries} intrari) din env"
             f"{' — valoare noua, reseed' if previous else ''}")
    return path, entries


def cookie_status() -> dict:
    """Starea fisierului de cookies, pentru !health si /status.

    Varsta conteaza la fel de mult ca numarul de intrari: yt-dlp rescrie fisierul
    la fiecare rotatie de `__Secure-1PSIDTS`, deci un fisier vechi de zile pe un
    bot care a redat inseamna ca sesiunea nu se mai reinnoieste.
    """
    path = _cookies_path or _cookie_file_paths()[0]
    if not os.path.exists(path):
        return {'path': path, 'exists': False, 'entries': 0, 'age_sec': None}
    try:
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        age = None
    health = cookie_health(path)
    return {
        'path': path,
        'exists': True,
        'entries': health['entries'],
        'age_sec': round(age) if age is not None else None,
        # Numarul de intrari nu spune daca jar-ul mai autentifica: un fisier cu
        # 20 de linii si zero cookie-uri de sesiune e la fel de inutil ca unul gol.
        'session_cookies': health['present'],
        'missing_critical': health['missing'],
        'valid': bool(health['entries'] and health['present']),
        'has_good_copy': os.path.exists(path + _GOOD_SUFFIX),
    }


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


# Doar -vn. Verificat in discord.py 2.7.1 instalat: FFmpegOpusAudio emite deja
# `-ar 48000 -ac 2 -b:a {bitrate}k`, iar sirul nostru se adauga DUPA, deci
# `-b:a 128k` suprascria bitrate-ul pe care from_probe tocmai il masurase din
# fisier (pe calea de transcodare; pe `-c:a copy` ffmpeg il ignora oricum).
# FFmpegPCMAudio emite `-f s16le -ar 48000 -ac 2`, deci si acolo restul era
# duplicat inutil.
FFMPEG_OPTS = {
    'options': '-vn',
}
