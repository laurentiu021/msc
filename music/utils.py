"""Functii utilitare: cleanup, format, filtrare."""
import os
import asyncio
import discord
from music.config import BLACKLIST, DOWNLOAD_DIR, log


async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


def is_clean(title: str, duration, last_title: str) -> bool:
    if duration and (duration > 660 or duration < 30):
        return False
    t = title.lower()
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
        except Exception:
            pass
