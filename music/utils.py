"""Functii utilitare: cleanup, format, filtrare."""
import asyncio
import json
import os
import re
import time

import aiohttp
import discord
from music.config import (BLACKLIST, DOWNLOAD_CACHE_BYTES, DOWNLOAD_DIR,
                          MAX_TRACK_SECONDS, MIN_TRACK_SECONDS, log)

# Tot ce poate ieși din stratul HTTP al lui discord.py, intr-un singur loc.
#
# `discord.HTTPException` singur NU ajunge, si asta a fost greșit in fiecare try
# din stratul de UI: discord.py nu invelește eșecurile de transport. http.py
# re-ridica `OSError` cand errno nu e 54/10054, iar `aiohttp.ServerDisconnectedError`
# (un `Exception`, nu un `OSError`) nu e prins deloc. O conexiune keep-alive
# inchisa de Discord exact cand scriem un DELETE scapa astfel din stratul de UI in
# try-ul de redare din process_play, care apoi sterge fisierul pe care FFmpeg il
# streameaza, bate contorul de erori si avanseaza coada — panoul omoara piesa.
#
# Plasa e larga deliberat, dar ENUMERATA: un `except Exception` ar inghiti si
# defectele noastre (AttributeError, KeyError) pe care vrem sa le vedem.
DISCORD_ERRORS = (discord.HTTPException, OSError, aiohttp.ClientError,
                  asyncio.TimeoutError)


async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except DISCORD_ERRORS as e:
            log.debug(f"Nu am putut sterge un mesaj: {e}")


# Plafoanele de codare. Opus la 128 kbps e transparent pentru muzica, iar sursa
# de la YouTube nu depaseste ~130 kbps oricum, deci mai mult e doar risipa.
MAX_ENCODE_KBPS = 128
MIN_ENCODE_KBPS = 48
# Bitrate-ul implicit al unui canal de voce Discord fara boost.
DEFAULT_CHANNEL_KBPS = 64


def encode_bitrate_kbps(channel) -> int:
    """Cat are voie sa primeasca canalul asta, in kbps.

    `VoiceChannel.bitrate` e in bps. Cand lipsește (un canal fals, un obiect
    partial) cadem pe valoarea implicita a lui Discord, nu pe zero.
    """
    raw = getattr(channel, 'bitrate', None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raw = DEFAULT_CHANNEL_KBPS * 1000
    return max(MIN_ENCODE_KBPS, min(int(raw // 1000), MAX_ENCODE_KBPS))


async def make_opus_source(filename: str, channel, **ffmpeg_opts):
    """Sursa Opus cu bitrate ALES DE NOI, nu de sonda lui discord.py.

    `FFmpegOpusAudio.from_probe` pare exact ce trebuie, dar discord.py 2.7.1
    calculeaza `bitrate = max(round(bit_rate / 1000), 512)` in
    `_probe_codec_native` (player.py:677 in versiunea instalata) — un `max` unde
    intentia era evident un `min`. Deci `from_probe` cere lui FFmpeg MINIM 512
    kbps, indiferent de sursa.

    Cat timp YouTube da opus nu se aude nimic din asta: codec-ul probat e 'opus',
    discord.py pune `-c:a copy` si FFmpeg ignora `-b:a`. Dar cand YouTube nu mai
    da opus — experimentul SABR-only lasa doar AAC — se intra pe reencodare si
    FFmpeg produce un flux de 512 kbps pentru un canal de 64.

    Codec-ul il luam tot de la sonda: acolo e corect, si un fisier deja opus
    rămâne pe `-c:a copy`, adica zero reencodare.
    """
    codec, _ = await discord.FFmpegOpusAudio.probe(filename)
    bitrate = encode_bitrate_kbps(channel)
    copies = codec in ('opus', 'libopus', 'copy')
    log.info(f"Audio: codec={codec} -> {'copy' if copies else 'reencodare libopus'} "
             f"@{bitrate}k (canal {getattr(channel, 'bitrate', '?')})")
    return discord.FFmpegOpusAudio(filename, codec=codec, bitrate=bitrate,
                                   **ffmpeg_opts)


def clean_search_title(title) -> str:
    """Curata un titlu pentru cautare.

    'Los Del Rio - Macarena (Official Video)' -> 'Los Del Rio - Macarena'
    Exista o singura data: autoplay.py si youtube_api.py aveau fiecare varianta
    proprie, cu liste diferite de cuvinte, deci aceeasi piesa era curatata
    diferit in functie de cine o cerea si cota de API se ducea pe cozi murdare.
    """
    title = str(title or '')
    clean = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean = re.sub(
        r'\b(official|video|audio|lyrics|lyric|hd|hq|4k|mv|music\s*video|'
        r'visualizer|visualiser|clip|feat\.?|ft\.?|prod\.?|remix|'
        r'challenge|reaction|tutorial|cover|live|performance|vevo)\b',
        '', clean, flags=re.I
    ).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    clean = re.sub(r'\s*[-|]+\s*$', '', clean).strip()
    return clean if len(clean) >= 3 else title


def item_title(item, limit: int | None = None) -> str:
    """Titlul unui element din coada, niciodata None.

    yt-dlp intoarce title=None pentru unele intrari (nu lipsa cheii, deci
    dict.get(k, default) nu ajuta), iar indexarea unui None arunca TypeError
    exact in primul statement din _play_next_async si ucide sesiunea mut.
    """
    if isinstance(item, dict):
        raw = item.get('title') or item.get('query') or 'Necunoscut'
    else:
        raw = item or 'Necunoscut'
    text = str(raw)
    return text[:limit] if limit else text


# Metadatele de langa fisierul audio. Existau doar in `state.history`, care are
# 20 de intrari si e golit de `!stop`, de plecarea din voce si de butonul Inapoi —
# iar intrarile lui nu purtau nici durata, nici thumbnail. Deci un hit de cache
# pornea cu durata 0: panoul pierdea lungimea si tot rândul de timp rămas, `!seek`
# rămânea fara plafon (garda e scrisa `if state.last_duration and ...`), si fiindca
# lipseau si views/likes fiecare hit cumpara o unitate de Data API plus un al doilea
# edit de panou — exact costul pe care cache-ul exista sa il elimine.
#
# Fisierul insoțitor face cache-ul sa se descrie singur: orice piesa de pe disc e
# redabila cu metadate complete, indiferent cat de veche e, fara nicio cerere.
_META_EXT = '.meta.json'
_META_FIELDS = ('url', 'title', 'channel', 'duration', 'thumbnail', 'views',
                'likes')

# Extensiile audio pe care le poate scrie yt-dlp. Definite AICI, nu in resolve.py:
# le foloseste si bucla de descarcare (cand `prepare_filename` a ghicit alta
# extensie) si lista de sugestii, iar doua copii ar putea sa divergeze — o piesa
# `.ogg` ar fi atunci descarcabila dar niciodata sugerata.
AUDIO_EXTS = ('.opus', '.m4a', '.webm', '.mp3', '.ogg')


def _meta_path(audio_path: str) -> str:
    """Un singur loc unde se decide numele fisierului insoțitor."""
    return os.path.splitext(audio_path)[0] + _META_EXT


def write_track_meta(audio_path: str, meta: dict) -> bool:
    """Reține metadatele piesei langa fisierul audio. True daca s-a scris."""
    if not audio_path:
        return False
    payload = {k: meta.get(k) for k in _META_FIELDS}
    try:
        with open(_meta_path(audio_path), 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return True
    except OSError as e:
        log.debug(f"Nu am putut scrie metadatele pentru {audio_path}: {e}")
        return False


def read_track_meta(audio_path: str) -> dict | None:
    """Metadatele de langa un fisier din cache, sau None.

    None inseamna "nu stim ce e fisierul asta", iar apelantul trebuie sa trateze
    asta ca lipsa de cache: mai bine o descarcare in plus decat un panou care
    minte despre ce cânta.
    """
    if not audio_path:
        return None
    try:
        with open(_meta_path(audio_path), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        log.debug(f"Fara metadate pentru {audio_path}: {e}")
        return None
    if not isinstance(data, dict) or not data.get('title'):
        return None
    return data


# Sugestiile pentru autocomplete-ul lui `/play`. De cand exista insoțitorii, cache-ul
# de pe volum se descrie singur, deci lista pieselor ascultate e deja pe disc: nicio
# cerere catre YouTube si niciun index separat de intreținut.
_SUGGEST_TTL_SEC = 30.0
_suggest_cache: tuple[float, list[dict]] = (0.0, [])

# Minuscule si fara diacritice, ca potrivirea sa nu depinda de tastatura: cine
# tasteaza "macarena" trebuie sa gaseasca "Los Del Rio - Macarena".
_DIACRITICS = str.maketrans('ăâîșțĂÂÎȘȚşţŞŢ', 'aaistAAISTstST')


def _fold(text) -> str:
    return str(text or '').translate(_DIACRITICS).lower()


def cached_tracks(directory: str | None = None, *, now=None) -> list[dict]:
    """Piesele din cache, cea mai recent folosita prima.

    Memoizat 30 de secunde: autocomplete-ul se declanșeaza la FIECARE tasta, iar
    Discord da doar 3 secunde raspunsului. O listare de director plus cateva citiri
    de JSON sunt ieftine — dar nu de zece ori pe secunda.
    """
    global _suggest_cache
    directory = directory or DOWNLOAD_DIR
    now = time.time() if now is None else now
    stamp, cached = _suggest_cache
    if cached and now - stamp < _SUGGEST_TTL_SEC:
        return cached

    entries = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if not name.endswith(_META_EXT):
            continue
        stem = os.path.join(directory, name[:-len(_META_EXT)])
        for ext in AUDIO_EXTS:
            audio = stem + ext
            if not os.path.exists(audio):
                continue
            meta = read_track_meta(audio)
            if meta:
                try:
                    used = os.path.getmtime(audio)
                except OSError:
                    used = 0.0
                entries.append({**meta, 'used_at': used})
            break
    entries.sort(key=lambda item: item['used_at'], reverse=True)
    _suggest_cache = (now, entries)
    return entries


def suggest_tracks(query: str, limit: int = 25,
                   directory: str | None = None) -> list[dict]:
    """Piesele din cache care se potrivesc cu ce s-a tastat pana acum.

    Fara text tastat, primele sunt cele mai recent ascultate — exact ce vrea cineva
    care deschide `/play` pe telefon ca sa repuna ce s-a dat acum o ora.
    """
    text = _fold(query)
    out = []
    for track in cached_tracks(directory):
        if text and text not in _fold(track.get('title')):
            continue
        out.append(track)
        if len(out) >= limit:
            break
    return out


def cached_download(video_id: str, directory: str | None = None) -> str | None:
    """Fisierul deja descarcat pentru acest ID, sau None.

    Un hit inseamna zero cereri catre YouTube, zero octeti de media, zero rulari
    de Deno, niciun slot de throttle si nicio expunere la 429 sau la cookie-uri
    expirate. Intr-un grup de cațiva oameni aceleasi piese revin constant: acelasi
    link dat din nou, butonul Back, loop pe coada, selectul de salt, sau autoplay
    care re-propune o piesa ieșita din cele 20 de intrari de history.

    Nu intoarce NICIODATA un `.part`: o descarcare intrerupta de un SIGKILL ar fi
    redata ca piesa corupta. Nici fisiere de zero octeti.
    """
    if not video_id:
        return None
    directory = directory or DOWNLOAD_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in sorted(names):
        # Insoțitorul de metadate nu poate fi confundat cu audio: extensia lui e
        # dubla, deci `splitext('vid.meta.json')` da stem-ul 'vid.meta', care nu se
        # potriveste niciodata cu un ID. De aceea nu are nevoie de o excludere
        # proprie aici — dar `_META_EXT` trebuie sa rămână cu doua puncte.
        stem, ext = os.path.splitext(name)
        if stem != video_id or ext in ('.part', '.ytdl', ''):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getsize(path) <= 0:
                continue
            # Atinge fisierul, ca LRU-ul sa reflecte folosirea, nu descarcarea.
            os.utime(path, None)
        except OSError:
            continue
        return path
    return None


def trim_download_cache(keep, max_bytes: int | None = None,
                        directory: str | None = None) -> int:
    """Tine cache-ul audio sub plafon, stergand cele mai vechi. Returneaza cate.

    `keep` sunt fisierele in uz chiar acum si nu se sterg niciodata, indiferent
    cat de plin e cache-ul: mai bine depasim plafonul cu o piesa decat sa tragem
    fisierul de sub FFmpeg.
    """
    directory = directory or DOWNLOAD_DIR
    max_bytes = DOWNLOAD_CACHE_BYTES if max_bytes is None else max_bytes
    protected = {os.path.abspath(p) for p in keep if p}
    entries = []
    total = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        path = os.path.abspath(os.path.join(directory, name))
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        total += size
        # `.part` si `.ytdl` sunt descarcari IN CURS. `cached_download` le exclude
        # deja de la servire, dar evacuarea nu le excludea de la stergere: cu un
        # cache plin, un transfer mare in desfasurare se stergea singur de sub
        # yt-dlp (verificat: un `.part` de 5MB cu plafon 1MB era sters), apoi
        # `try_rename` eșua si descarcarea aparea ca defectiune tehnica, bătând
        # contorul de erori consecutive. Marimea lor se numara in total — de-aia
        # trebuie evacuat altceva — dar nu sunt candidati.
        if name.endswith(('.part', '.ytdl')):
            continue
        if path not in protected:
            entries.append((mtime, size, path))

    if total <= max_bytes:
        return 0

    removed = 0
    for _, size, path in sorted(entries):          # cele mai vechi primele
        if total <= max_bytes:
            break
        if path.endswith(_META_EXT):
            # Insoțitorul pleaca odata cu audio-ul lui, nu singur: altfel ar
            # rămâne un fisier audio pe disc pe care nimeni nu mai stie sa il
            # descrie, adica un hit de cache cu panou gol.
            continue
        try:
            os.remove(path)
        except OSError as e:
            log.debug(f"Nu am putut sterge {path}: {e}")
            continue
        meta = _meta_path(path)
        try:
            if os.path.exists(meta):
                total -= os.path.getsize(meta)
                os.remove(meta)
        except OSError:
            pass
        total -= size
        removed += 1
    if removed:
        log.info(f"Cache audio: {removed} fisiere vechi sterse "
                 f"({total // 1024 // 1024}MB rămași)")
    return removed


def sweep_partials(directory: str | None = None) -> int:
    """Sterge descarcarile intrerupte. De rulat la pornire.

    Un SIGKILL in mijlocul unei descarcari lasa un `.part`; fara curatarea asta,
    cache-ul ar putea servi mai tarziu un fisier trunchiat.
    """
    directory = directory or DOWNLOAD_DIR
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    stems = {os.path.splitext(n)[0] for n in names
             if not n.endswith(_META_EXT) and not n.endswith(('.part', '.ytdl'))}
    for name in names:
        path = os.path.join(directory, name)
        if name.endswith(_META_EXT):
            # Insoțitor fara audio: fisierul lui a fost evacuat de o versiune
            # anterioara, sau descarcarea a fost intrerupta dupa ce metadatele
            # ajunseseră pe disc. Fara curatare, ar rămâne pe volum pe veci.
            if name[:-len(_META_EXT)] in stems:
                continue
        elif not name.endswith(('.part', '.ytdl')):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    if removed:
        log.info(f"Curatenie la pornire: {removed} fisiere orfane sterse")
    return removed


def playback_remaining(now: float, start_time: float, duration: float,
                       paused_at: float = 0.0) -> tuple[float, float]:
    """(scurs, ramas) in secunde. Pauza nu consuma din piesa.

    Functie pura, cu `now` primit: panoul calcula finalul ca
    start_time + durata, iar nimic nu ajusta start_time la pauza, deci dupa o
    pauza de 10 minute panoul anunta ca piesa s-a terminat acum 6 minute — exact
    semnalul greșit pe un deploy care uneori chiar se blocheaza.
    """
    if not duration or not start_time:
        return 0.0, 0.0
    reference = paused_at if paused_at else now
    elapsed = max(0.0, reference - start_time)
    return min(elapsed, duration), max(0.0, duration - elapsed)


def is_clean(title, duration, last_title: str) -> bool:
    if duration and (duration > MAX_TRACK_SECONDS or duration < MIN_TRACK_SECONDS):
        return False
    if not title:
        # Fara titlu nu putem filtra nimic; il tratam ca nepotrivit ca sa nu
        # ajunga in coada un element pe care apoi nu-l putem nici afisa.
        return False
    t = str(title).lower()
    if any(word in t for word in BLACKLIST):
        return False
    if last_title:
        lt = last_title.lower()
        if lt[:15] in t and len(lt) > 15:
            return False
    return True


def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "0:00"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def cleanup_file(filename, loop=None):
    """Scheduleaza stergerea fisierului cu delay."""
    if not filename:
        return

    async def _delayed_delete():
        await asyncio.sleep(2)
        for _ in range(3):
            try:
                if os.path.exists(filename):
                    os.remove(filename)
                return
            except OSError:
                await asyncio.sleep(1)
        log.warning(f"Nu am putut sterge {filename} dupa 3 incercari")

    if loop:
        try:
            asyncio.run_coroutine_threadsafe(_delayed_delete(), loop)
            return
        except RuntimeError as e:
            # Bucla inchisa. Nu inghitim in tacere: cadem pe stergerea sincrona
            # de mai jos, dar motivul trebuie sa se vada in loguri.
            log.debug(f"Stergerea amanata nu a putut fi programata: {e}")
    # Fara loop, varianta veche ieșea fara sa stearga nimic si fisierul rămânea
    # pe disc definitiv. Stergem sincron, e o singura operatie de filesystem.
    try:
        if os.path.exists(filename):
            os.remove(filename)
    except OSError as e:
        log.warning(f"Nu am putut sterge {filename}: {e}")
