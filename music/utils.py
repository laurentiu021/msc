"""Functii utilitare: cleanup, format, filtrare."""
import os
import asyncio
import discord
from music.config import (BLACKLIST, DOWNLOAD_DIR, MAX_TRACK_SECONDS,
                          MIN_TRACK_SECONDS, log)


async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


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
