"""Functii utilitare: cleanup, format, filtrare."""
import asyncio
import os
import re
import time

import discord
from music.config import (BLACKLIST, DOWNLOAD_CACHE_BYTES, DOWNLOAD_DIR,
                          MAX_TRACK_SECONDS, MIN_TRACK_SECONDS, log)


async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


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
        if path not in protected:
            entries.append((mtime, size, path))

    if total <= max_bytes:
        return 0

    removed = 0
    for _, size, path in sorted(entries):          # cele mai vechi primele
        if total <= max_bytes:
            break
        try:
            os.remove(path)
        except OSError as e:
            log.debug(f"Nu am putut sterge {path}: {e}")
            continue
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
    for name in names:
        if not name.endswith(('.part', '.ytdl')):
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    if removed:
        log.info(f"Curatenie la pornire: {removed} descarcari intrerupte sterse")
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
        except Exception:
            pass
    # Fara loop, varianta veche ieșea fara sa stearga nimic si fisierul rămânea
    # pe disc definitiv. Stergem sincron, e o singura operatie de filesystem.
    try:
        if os.path.exists(filename):
            os.remove(filename)
    except OSError as e:
        log.warning(f"Nu am putut sterge {filename}: {e}")
