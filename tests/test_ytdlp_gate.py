"""Poarta catre yt-dlp: o cerere pe rand, si niciun cookie distrus la timeout.

Doua proprietati, ambele incalcate de versiuni anterioare ale acestui modul:

1. Serializare reala. Un simplu interval distanta doar PLECARILE, deci doua
   extractii lente porneau la 1.2s una de alta si rulau suprapus — exact
   rafalele pe care throttle-ul exista sa le previna.

2. Fisierul de cookies supravietuieste unui timeout. Cand
   `with yt_dlp.YoutubeDL(...)` inconjura `asyncio.wait_for`, timeout-ul ieșea
   prin `__exit__` -> `close()` -> `save_cookies()` -> `open(path, 'w')`, pe
   bucla de evenimente, cat timp thread-ul era inca in `extract_info`. Fisierul
   partajat de pe volum era trunchiat (reprodus: 142 bytes -> 28), deci un
   singur timeout lasa botul fara autentificare pana la urmatorul reseed.

Ruleaza fara pytest, fara retea:  python tests/test_ytdlp_gate.py
"""
import asyncio
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import ytdlp


class _FakeYtDlpModule:
    """Ține locul modulului yt_dlp, cu semantica lui de close()."""

    def __init__(self, body_sec, cookiefile=None, on_event=None):
        self.body_sec = body_sec
        self.cookiefile = cookiefile
        self.on_event = on_event or (lambda *a: None)
        outer = self

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts
                outer.on_event('init', threading.current_thread().name)

            def extract_info(self, query, download=False):
                outer.on_event('enter', threading.current_thread().name)
                try:
                    time.sleep(outer.body_sec)
                finally:
                    outer.on_event('exit', threading.current_thread().name)
                return {'id': 'vid', 'title': 'T', 'ext': 'opus'}

            def prepare_filename(self, info):
                return f"downloads/{info['id']}.{info['ext']}"

            def close(self):
                # yt_dlp.YoutubeDL.close() -> save_cookies() -> rescrie
                # fisierul de cookies. Rescrierea in sine e corecta (asa
                # supravietuiesc valorile rotite); catastrofala e doar cand se
                # intampla pe alt thread decat cel care lucreaza.
                outer.on_event('close', threading.current_thread().name)
                if outer.cookiefile:
                    with open(outer.cookiefile, 'w', encoding='utf-8') as fh:
                        fh.write('# rotit de yt-dlp\nvaloare-noua\n')

        self.YoutubeDL = YoutubeDL


def _install(fake):
    saved = ytdlp.yt_dlp
    ytdlp.yt_dlp = fake
    return saved


def _reset_gate():
    ytdlp._NEXT_ALLOWED_AT = 0.0


def test_concurrent_extracts_never_overlap():
    lock = threading.Lock()
    live = {'now': 0, 'max': 0}

    def on_event(kind, thread):
        with lock:
            if kind == 'enter':
                live['now'] += 1
                live['max'] = max(live['max'], live['now'])
            elif kind == 'exit':
                live['now'] -= 1

    fake = _FakeYtDlpModule(0.15, on_event=on_event)
    saved = _install(fake)
    saved_min = ytdlp.YT_REQUEST_MIN_INTERVAL_SEC
    saved_max = ytdlp.YT_REQUEST_MAX_INTERVAL_SEC
    ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = 0.2
    ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = 0.2
    _reset_gate()
    try:
        async def main():
            start = time.monotonic()
            await asyncio.gather(*[
                ytdlp.extract({}, f'q{i}', stage=f's{i}') for i in range(3)
            ])
            return time.monotonic() - start

        elapsed = asyncio.run(main())
    finally:
        ytdlp.yt_dlp = saved
        ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = saved_min
        ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = saved_max
        _reset_gate()

    assert live['max'] == 1, f'{live["max"]} extractii au rulat simultan'
    floor = 3 * 0.15 + 2 * 0.2
    assert elapsed >= floor, f'{elapsed:.2f}s < {floor:.2f}s: intervalul nu s-a aplicat'


def test_timeout_leaves_the_cookie_file_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        cookies = os.path.join(tmp, 'cookies.txt')
        original = ('# Netscape HTTP Cookie File\n'
                    '.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvaloare-originala\n')
        with open(cookies, 'w', encoding='utf-8') as fh:
            fh.write(original)

        closes = []

        def on_event(kind, thread):
            if kind == 'close':
                closes.append(thread)

        fake = _FakeYtDlpModule(0.6, cookiefile=cookies, on_event=on_event)
        saved = _install(fake)
        saved_budget = ytdlp.EXTRACT_TIMEOUT_SEC
        ytdlp.EXTRACT_TIMEOUT_SEC = 0.1
        leaked_before = ytdlp.leaked_workers()
        _reset_gate()
        try:
            async def main():
                try:
                    await ytdlp.extract({'cookiefile': cookies}, 'q')
                except TimeoutError:
                    # Exact momentul care conta: thread-ul lucreaza inca.
                    with open(cookies, encoding='utf-8') as fh:
                        return fh.read()
                raise AssertionError('nu a expirat bugetul')

            after_timeout = asyncio.run(main())
        finally:
            ytdlp.EXTRACT_TIMEOUT_SEC = saved_budget
            time.sleep(0.9)          # lasa thread-ul abandonat sa termine
            ytdlp.yt_dlp = saved
            _reset_gate()

        assert after_timeout == original, (
            'fisierul de cookies a fost atins la timeout: '
            f'{len(after_timeout)} bytes vs {len(original)}')
        assert ytdlp.leaked_workers() == leaked_before + 1, \
            'thread-ul abandonat nu e numarat, deci epuizarea e invizibila'
        assert closes, 'thread-ul nu a mai inchis niciodata instanta'
        assert all('ytdlp' in name for name in closes), \
            f'close() a rulat in afara executorului: {closes}'


def test_close_runs_in_the_worker_thread_not_the_loop():
    """Aceeasi cauza, verificata direct pe calea fericita."""
    threads = {}
    fake = _FakeYtDlpModule(
        0.01, on_event=lambda kind, thread: threads.setdefault(kind, thread))
    saved = _install(fake)
    _reset_gate()
    try:
        asyncio.run(ytdlp.extract({}, 'q'))
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()

    assert threads['close'] == threads['enter'], \
        f"close() pe alt thread decat extract_info: {threads}"
    assert 'ytdlp' in threads['close'], \
        f"nu s-a folosit executorul dedicat: {threads['close']}"


def test_download_returns_filename_from_the_same_instance():
    fake = _FakeYtDlpModule(0.01)
    saved = _install(fake)
    _reset_gate()
    try:
        info, filename = asyncio.run(
            ytdlp.extract_and_prepare_filename({}, 'q', stage='download'))
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()

    assert info['id'] == 'vid'
    assert filename == 'downloads/vid.opus', filename


def test_extract_without_want_filename_returns_only_info():
    fake = _FakeYtDlpModule(0.01)
    saved = _install(fake)
    _reset_gate()
    try:
        info = asyncio.run(ytdlp.extract({}, 'q'))
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()

    assert isinstance(info, dict) and info['id'] == 'vid', info


def test_executor_is_dedicated_and_bounded():
    """Pool-ul implicit e cel pe care discord.py il foloseste la probe audio."""
    assert ytdlp._EXECUTOR is not None
    assert 0 < ytdlp.MAX_WORKERS <= 8, ytdlp.MAX_WORKERS
    assert ytdlp.DOWNLOAD_TIMEOUT_SEC > ytdlp.EXTRACT_TIMEOUT_SEC


def test_the_gate_survives_a_second_event_loop():
    """Primitivele create la import se leaga de prima bucla si apoi arunca.

    asyncio.Semaphore parcheaza waiter-ii pe bucla curenta si de atunci refuza
    orice alta: "is bound to a different event loop" (verificat pe 3.12.10). Cu
    o singura bucla in producție nu s-ar vedea niciodata — pana cand cineva o
    reporneste in proces si fiecare cerere catre YouTube pica pe viata.
    """
    fake = _FakeYtDlpModule(0.02)
    saved = _install(fake)
    saved_min = ytdlp.YT_REQUEST_MIN_INTERVAL_SEC
    saved_max = ytdlp.YT_REQUEST_MAX_INTERVAL_SEC
    ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = 0.0
    ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = 0.0
    _reset_gate()
    try:
        async def contend():
            # Concurenta e obligatorie: fara parcare, semaforul nu se leaga de
            # bucla si testul ar trece si cu bug-ul prezent.
            await asyncio.gather(*[ytdlp.extract({}, f'q{i}') for i in range(3)])

        try:
            asyncio.run(contend())      # bucla A
            asyncio.run(contend())      # bucla B — aici cadea
        except RuntimeError as e:
            raise AssertionError(f'primitive legate de prima bucla: {e}')
    finally:
        ytdlp.yt_dlp = saved
        ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = saved_min
        ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = saved_max
        _reset_gate()


if __name__ == '__main__':
    # Consola Windows e cp1252: un mesaj de eșec cu diacritice ar arunca
    # UnicodeEncodeError si ar ascunde exact testul care a picat.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        try:
            fn()
            print(f'PASS {name}')
        except AssertionError as e:
            failed += 1
            print(f'FAIL {name}: {e}')
        except Exception as e:
            # Nu doar AssertionError: un test care CRAPA (RuntimeError,
            # TypeError) opreste altfel fisierul si testele de dupa el nu mai
            # ruleaza deloc, fara sa apara nicaieri ca lipsesc.
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
