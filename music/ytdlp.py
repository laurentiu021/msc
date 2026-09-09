"""Singurul loc prin care trec cererile catre yt-dlp.

Exista pentru ca throttle-ul era implementat doar in player.py, iar autoplay.py
si calea de playlist chemau `yt_dlp.YoutubeDL(...)` direct, prin
`run_in_executor`. Adica exact componenta care trage 50 de rezultate dintr-un
YouTube Mix ocolea limitatorul de rata — pe un IP de datacenter deja limitat.

Rezolva si a doua problema din vechea implementare: slotul era rezervat INAINTE
ca cererea sa ruleze, deci intervalul distanta doar momentele de plecare, nu
cererile intre ele. Doua extractii lente puteau pleca la 1.2s una de alta si
rula complet suprapus.
"""
import asyncio
import random
import time

import yt_dlp

from music.config import (YT_REQUEST_MAX_INTERVAL_SEC,
                          YT_REQUEST_MIN_INTERVAL_SEC, log)

_LOCK = asyncio.Lock()
_NEXT_ALLOWED_AT = 0.0

# Plafon absolut per cerere. socket_timeout acopera doar inactivitatea pe socket:
# un stream care curge foarte lent, sau un manifest live, putea tine un thread
# din executor ocupat pe viata procesului, fara nimic in loguri.
EXTRACT_TIMEOUT_SEC = 90
DOWNLOAD_TIMEOUT_SEC = 240


async def wait_for_slot():
    """Asteapta pana e permisa urmatoarea cerere catre YouTube."""
    global _NEXT_ALLOWED_AT
    async with _LOCK:
        wait_for = _NEXT_ALLOWED_AT - time.time()
        if wait_for > 0:
            log.info(f"YouTube throttling active: waiting {wait_for:.1f}s")
            await asyncio.sleep(wait_for)


def _reserve_next_slot():
    """Stabileste momentul urmatoarei cereri, masurat de la SFARSITUL acesteia."""
    global _NEXT_ALLOWED_AT
    low = max(0.0, YT_REQUEST_MIN_INTERVAL_SEC)
    high = max(low, YT_REQUEST_MAX_INTERVAL_SEC)
    _NEXT_ALLOWED_AT = time.time() + random.uniform(low, high)


async def extract(opts: dict, query: str, *, download: bool = False,
                  loop=None, stage: str = ''):
    """Ruleaza extract_info in executor, cu throttle si opts izolate.

    `opts` trebuie sa vina din config.make_search_opts()/make_download_opts():
    YoutubeDL muteaza dict-ul primit, deci refolosirea unuia global scurge stare
    intre apeluri.
    """
    await wait_for_slot()
    loop = loop or asyncio.get_running_loop()
    budget = DOWNLOAD_TIMEOUT_SEC if download else EXTRACT_TIMEOUT_SEC
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: ydl.extract_info(query, download=download)
                ),
                timeout=budget,
            )
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"yt-dlp a depasit {budget}s la {stage or 'cerere'}") from e
    finally:
        # Si pe eroare: o cerere care a picat a consumat oricum cota YouTube.
        _reserve_next_slot()
        if stage:
            log.debug(f"yt-dlp stage terminat: {stage}")


async def extract_and_prepare_filename(opts: dict, query: str, *, loop=None,
                                       stage: str = ''):
    """Descarca si intoarce (info, filename_probabil).

    prepare_filename cere aceeasi instanta YoutubeDL care a descarcat, de aceea
    nu se poate compune din `extract`.
    """
    await wait_for_slot()
    loop = loop or asyncio.get_running_loop()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: ydl.extract_info(query, download=True)
                ),
                timeout=DOWNLOAD_TIMEOUT_SEC,
            )
            return info, ydl.prepare_filename(info)
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"descarcarea a depasit {DOWNLOAD_TIMEOUT_SEC}s") from e
    finally:
        _reserve_next_slot()
        if stage:
            log.debug(f"yt-dlp stage terminat: {stage}")
