"""Singurul loc prin care trec cererile catre yt-dlp.

Exista pentru ca throttle-ul era implementat doar in player.py, iar autoplay.py
si calea de playlist chemau `yt_dlp.YoutubeDL(...)` direct, prin
`run_in_executor`. Adica exact componenta care trage 50 de rezultate dintr-un
YouTube Mix ocolea limitatorul de rata — pe un IP de datacenter deja limitat.

Trei lucruri sunt subtile si toate au fost greșite la un moment dat:

1. O cerere pe rand. Un simplu interval nu ajunge: distanta doar plecarile, deci
   doua extractii lente puteau porni la 1.2s una de alta si rula suprapus. Poarta
   e un semafor de 1, iar intervalul se aplica intre cereri.

2. Instanta YoutubeDL se construieste SI se inchide in thread-ul executorului.
   Cand `with yt_dlp.YoutubeDL(...)` inconjura `asyncio.wait_for`, un timeout
   ieșea prin `__exit__` -> `close()` -> `save_cookies()` -> `open(file, 'w')`,
   adica TRUNCHIA fisierul de cookies partajat, cat timp thread-ul era inca in
   `extract_info`. Reprodus: 142 de bytes au devenit 28. `wait_for` nu poate opri
   un thread, deci pe timeout NU inchidem nimic: un obiect scapat e ieftin, un
   fisier de cookies distrus pe volum nu e.

3. Executor propriu. `run_in_executor(None, ...)` foloseste pool-ul implicit, pe
   care discord.py il foloseste si el pentru `FFmpegOpusAudio.probe`. Un thread
   de yt-dlp blocat acolo intarzia pornirea audio, nu doar extractia.
"""
import asyncio
import concurrent.futures
import contextlib
import os
import random
import threading
import time

import yt_dlp

from music.config import (YT_REQUEST_MAX_INTERVAL_SEC,
                          YT_REQUEST_MIN_INTERVAL_SEC, log)
from music.errors import YtdlpTimeout

_NEXT_ALLOWED_AT = 0.0
_LOOP = None
_LOCK = None
_GATE = None


def _workers() -> int:
    """Cate thread-uri de yt-dlp, dintr-un env var care poate fi scris greșit."""
    try:
        return max(1, min(8, int(os.getenv('YTDLP_WORKERS', '2'))))
    except ValueError:
        log.warning("YTDLP_WORKERS nu e un numar; folosesc 2")
        return 2


MAX_WORKERS = _workers()
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_WORKERS, thread_name_prefix='ytdlp')

# Doua numere DIFERITE, si confuzia dintre ele era un defect grav: watchdog-ul
# citea contorul cumulativ ca pe un indicator instantaneu, deci dupa al doilea
# timeout din viata containerului (perfect normal intr-o seara) declanșa
# os._exit(1) la fiecare tick, la infinit, pana la epuizarea celor 10 reporniri
# permise de Railway.
#   _leaked_live  = cate thread-uri sunt abandonate CHIAR ACUM (scade cand se
#                   termina). Asta e singurul numar din care se poate deduce ca
#                   executorul e infundat.
#   _leaked_total = cate au fost de la pornire (doar pentru raportare).
_leak_lock = threading.Lock()
_leaked_live = 0
_leaked_total = 0

# Plafon absolut per cerere. socket_timeout acopera doar inactivitatea pe socket:
# un stream care curge foarte lent, sau un manifest live, putea tine un thread
# din executor ocupat pe viata procesului, fara nimic in loguri.
EXTRACT_TIMEOUT_SEC = 90
DOWNLOAD_TIMEOUT_SEC = 240


def leaked_workers() -> int:
    """Cate thread-uri sunt abandonate CHIAR ACUM. Indicator, nu istorie."""
    with _leak_lock:
        return _leaked_live


def leaked_workers_total() -> int:
    """Cate cereri au depasit bugetul de la pornire. Doar pentru raportare."""
    with _leak_lock:
        return _leaked_total


def _mark_leaked() -> tuple[int, int]:
    global _leaked_live, _leaked_total
    with _leak_lock:
        _leaked_live += 1
        _leaked_total += 1
        return _leaked_live, _leaked_total


def _release_leaked(_future) -> None:
    """Thread-ul abandonat s-a terminat in cele din urma; slotul e liber.

    Rulează in thread-ul executorului, de aceea contorul e sub lock.
    """
    global _leaked_live
    with _leak_lock:
        _leaked_live = max(0, _leaked_live - 1)
        left = _leaked_live
    log.info(f"Thread-ul yt-dlp abandonat s-a incheiat; abandonate acum: {left}")


def _interval() -> tuple[float, float]:
    low = max(0.0, YT_REQUEST_MIN_INTERVAL_SEC)
    return low, max(low, YT_REQUEST_MAX_INTERVAL_SEC)


def _primitives():
    """(lock, poarta) legate de bucla care ruleaza ACUM.

    asyncio.Lock si asyncio.Semaphore create la import se leaga de prima bucla
    pe care ajung sa parcheze un waiter si arunca RuntimeError pe orice alta:
    "is bound to a different event loop" (verificat pe 3.12.10). Botul are o
    singura bucla, deci nu se vede niciodata — pana in ziua in care cineva o
    reporneste in proces si atunci FIECARE cerere catre YouTube pica pe viata.
    """
    global _LOOP, _LOCK, _GATE
    loop = asyncio.get_running_loop()
    if _GATE is None or loop is not _LOOP:
        _LOOP, _LOCK, _GATE = loop, asyncio.Lock(), asyncio.Semaphore(1)
    return _LOCK, _GATE


@contextlib.asynccontextmanager
async def _slot():
    """O cerere pe rand, cu pauza intre ele masurata de la sfarsit."""
    global _NEXT_ALLOWED_AT
    lock, gate = _primitives()
    async with gate:
        async with lock:
            wait_for = _NEXT_ALLOWED_AT - time.time()
        if wait_for > 0:
            log.info(f"YouTube throttling active: waiting {wait_for:.1f}s")
            await asyncio.sleep(wait_for)
        try:
            yield
        finally:
            # Atribuire directa, fara max(): rezervarea se scrie INAINTE de
            # eliberarea portii, deci niciun alt apelant nu a putut citi o
            # valoare intre timp, iar time.time() de acum e prin construcție
            # ulterior oricarei rezervari anterioare.
            low, high = _interval()
            async with lock:
                _NEXT_ALLOWED_AT = time.time() + random.uniform(low, high)


async def extract(opts: dict, query: str, *, download: bool = False,
                  loop=None, stage: str = '', want_filename: bool = False):
    """Ruleaza extract_info in executor, sub poarta si cu opts izolate.

    `opts` trebuie sa vina din config.make_search_opts()/make_download_opts():
    YoutubeDL muteaza dict-ul primit, deci refolosirea unuia global scurge stare
    intre apeluri.

    Cu want_filename=True intoarce (info, filename): `prepare_filename` cere
    aceeasi instanta care a descarcat, deci se calculeaza in acelasi thread.
    """
    loop = loop or asyncio.get_running_loop()
    budget = DOWNLOAD_TIMEOUT_SEC if download else EXTRACT_TIMEOUT_SEC

    def _run():
        # Construim SI inchidem aici, in thread-ul executorului. Daca inchiderea
        # ar avea loc pe bucla de evenimente in timp ce thread-ul lucreaza, ar
        # trunchia fisierul de cookies si ar scoate stratul HTTP de sub el.
        ydl = yt_dlp.YoutubeDL(opts)
        try:
            info = ydl.extract_info(query, download=download)
            return info, (ydl.prepare_filename(info) if want_filename else None)
        finally:
            ydl.close()

    async with _slot():
        # Trimitem DIRECT in executor ca sa pastram concurrent.futures.Future:
        # la timeout, `wait_for` anuleaza doar invelisul asyncio, iar callback-ul
        # lui s-ar declanșa imediat. Future-ul executorului se incheie abia cand
        # thread-ul chiar termina — singurul moment in care slotul e liber cu
        # adevarat, si deci singurul din care se poate scadea contorul.
        work = _EXECUTOR.submit(_run)
        try:
            info, filename = await asyncio.wait_for(
                asyncio.wrap_future(work, loop=loop), timeout=budget)
        except asyncio.TimeoutError as e:
            live, total = _mark_leaked()
            work.add_done_callback(_release_leaked)
            log.warning(
                f"yt-dlp a depasit {budget}s la {stage or 'cerere'}; thread-ul "
                f"continua in fundal (abandonate acum: {live}/{MAX_WORKERS}, "
                f"total de la pornire: {total})")
            raise YtdlpTimeout(stage, budget) from e

    if stage:
        log.debug(f"yt-dlp stage terminat: {stage}")
    return (info, filename) if want_filename else info


async def extract_and_prepare_filename(opts: dict, query: str, *, loop=None,
                                       stage: str = ''):
    """Descarca si intoarce (info, filename_probabil)."""
    return await extract(opts, query, download=True, loop=loop, stage=stage,
                         want_filename=True)
