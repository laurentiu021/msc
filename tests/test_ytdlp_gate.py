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

    ROTATION = ('# rotit de yt-dlp\n'
                '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tvaloare-noua\n')

    def __init__(self, body_sec, cookiefile=None, on_event=None):
        self.body_sec = body_sec
        self.cookiefile = cookiefile
        self.on_event = on_event or (lambda *a: None)
        # Calea pe care yt-dlp a PRIMIT-O de fapt. Cu izolarea de jar, nu are voie
        # sa fie niciodata fisierul comun.
        self.seen_cookiefiles = []
        outer = self

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts
                outer.seen_cookiefiles.append(opts.get('cookiefile'))
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
                # Scrie in fisierul din OPTS-urile lui, ca yt-dlp adevarat.
                target = self.opts.get('cookiefile') or outer.cookiefile
                if target:
                    with open(target, 'w', encoding='utf-8') as fh:
                        fh.write(outer.ROTATION)

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

    fake = _FakeYtDlpModule(0.05, on_event=on_event)
    saved = _install(fake)
    _reset_gate()
    try:
        async def main():
            await asyncio.gather(*[
                ytdlp.extract({}, f'q{i}', stage=f's{i}') for i in range(3)
            ])

        asyncio.run(main())
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()

    assert live['max'] == 1, f'{live["max"]} extractii au rulat simultan'


class _FrozenClock:
    """Ceas si somn injectate: intervalul se VERIFICA, nu se aȘteapta.

    Varianta care masura `time.monotonic()` era instabila prin construcție:
    rezervarea se calculeaza cu `time.time()`, iar cele doua ceasuri au rezolutii
    diferite pe Windows, deci un test cu prag de 0.85s cadea cu 0.84s masurat. Un
    test cu ceas de perete e si lent si nesigur; aici timpul e o valoare pe care o
    controlam, iar somnul doar avanseaza ceasul.
    """

    def __init__(self, ytdlp_module):
        self.module = ytdlp_module
        self.now = 1_000.0
        self.sleeps = []

    def __enter__(self):
        outer = self
        real_asyncio = self.module.asyncio
        self._saved = (self.module.time, real_asyncio,
                       self.module.YT_REQUEST_MIN_INTERVAL_SEC,
                       self.module.YT_REQUEST_MAX_INTERVAL_SEC)

        class _Time:
            @staticmethod
            def time():
                return outer.now

            @staticmethod
            def monotonic():
                return outer.now

        class _Asyncio:
            """Trece tot la asyncio-ul real, in afara de `sleep`."""

            def __getattr__(self, name):
                return getattr(real_asyncio, name)

            @staticmethod
            async def sleep(delay, *args, **kwargs):
                outer.sleeps.append(delay)
                outer.now += delay
                return await real_asyncio.sleep(0)

        self.module.time = _Time()
        self.module.asyncio = _Asyncio()
        # Interval fix: verificam poarta, nu generatorul de numere aleatoare.
        self.module.YT_REQUEST_MIN_INTERVAL_SEC = 2.0
        self.module.YT_REQUEST_MAX_INTERVAL_SEC = 2.0
        _reset_gate()
        return self

    def __exit__(self, *exc):
        (self.module.time, self.module.asyncio,
         self.module.YT_REQUEST_MIN_INTERVAL_SEC,
         self.module.YT_REQUEST_MAX_INTERVAL_SEC) = self._saved
        _reset_gate()
        return False


def test_the_interval_is_measured_from_the_end_of_the_previous_request():
    """Un simplu interval distanta doar PLECARILE.

    Prima varianta rezerva urmatorul slot la INTRARE, deci doua extractii lente
    porneau la 1.2s una de alta si rulau suprapus — exact rafalele pe care
    throttle-ul exista sa le previna. Rezervarea se scrie in `finally`, dupa ce
    corpul s-a terminat.
    """
    holder = {}

    def slow_body(kind, _thread):
        # Corpul cererii "dureaza" 5 secunde de ceas injectat. Fara asta, intrarea
        # si ieșirea cad in aceeasi clipa si diferenta dintre cele doua momente de
        # rezervare devine invizibila — exact ce facea defectul greu de prins.
        if kind == 'enter' and holder.get('clock'):
            holder['clock'].now += 5.0

    fake = _FakeYtDlpModule(0.0, on_event=slow_body)
    saved = _install(fake)
    try:
        with _FrozenClock(ytdlp) as clock:
            holder['clock'] = clock

            async def main():
                for i in range(3):
                    await ytdlp.extract({}, f'q{i}', stage=f's{i}')

            asyncio.run(main())
            waits = [round(s, 3) for s in clock.sleeps if s > 0]

        assert waits == [2.0, 2.0], (
            f'poarta nu a aȘteptat intervalul intre cereri: {waits}. '
            f'Cu rezervarea facuta la INTRARE, corpul de 5s consuma singur '
            f'intervalul si urmatoarea cerere porneste imediat.')
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()


def test_no_wait_is_needed_for_the_first_request():
    fake = _FakeYtDlpModule(0.0)
    saved = _install(fake)
    try:
        with _FrozenClock(ytdlp) as clock:
            asyncio.run(ytdlp.extract({}, 'q', stage='s'))
            assert [s for s in clock.sleeps if s > 0] == [], (
                f'prima cerere a fost intarziata degeaba: {clock.sleeps}')
    finally:
        ytdlp.yt_dlp = saved
        _reset_gate()


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
        total_before = ytdlp.leaked_workers_total()
        during = {}
        _reset_gate()
        try:
            async def main():
                try:
                    await ytdlp.extract({'cookiefile': cookies}, 'q')
                except TimeoutError:
                    # Exact momentul care conta: thread-ul lucreaza inca.
                    during['live'] = ytdlp.leaked_workers()
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
        # Doua proprietati diferite, si confundarea lor arma os._exit(1) pe viata:
        # cat timp thread-ul lucreaza, slotul E ocupat...
        assert during['live'] == leaked_before + 1, \
            'thread-ul abandonat nu e numarat, deci epuizarea e invizibila'
        # ...dar cand se termina, slotul se elibereaza. Indicatorul trebuie sa
        # coboare, altfel watchdog-ul crede la infinit ca executorul e infundat.
        assert ytdlp.leaked_workers() == leaked_before, (
            'thread-ul abandonat s-a terminat, dar contorul a rămas sus: '
            f'{ytdlp.leaked_workers()} vs {leaked_before}')
        assert ytdlp.leaked_workers_total() == total_before + 1, \
            'timeout-ul nu a fost inregistrat in istoric'
        assert closes, 'thread-ul nu a mai inchis niciodata instanta'
        assert all('ytdlp' in name for name in closes), \
            f'close() a rulat in afara executorului: {closes}'


def test_a_request_never_hands_yt_dlp_the_shared_jar():
    """Izolarea trebuie sa fie in POARTA, nu in buna-voința apelantilor.

    `YoutubeDL.close()` cheama `save_cookies()`, care rescrie necondiționat
    fisierul din jar-ul lui din memorie. Cat timp acel fisier e cel comun, orice
    thread — inclusiv unul abandonat, care se inchide minute mai tarziu, in afara
    portii — poate decide ce conține jar-ul de pe volum.
    """
    with tempfile.TemporaryDirectory() as tmp:
        shared = os.path.join(tmp, 'cookies.txt')
        original = ('# Netscape HTTP Cookie File\n'
                    '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tvaloare-veche\n')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(original)

        fake = _FakeYtDlpModule(0.01)
        saved = _install(fake)
        _reset_gate()
        try:
            asyncio.run(ytdlp.extract({'cookiefile': shared}, 'q'))
        finally:
            ytdlp.yt_dlp = saved
            _reset_gate()

        assert fake.seen_cookiefiles, 'nu s-a construit nicio instanta'
        assert all(p and p != shared for p in fake.seen_cookiefiles), (
            f'yt-dlp a primit chiar fisierul comun: {fake.seen_cookiefiles}')
        # Copia nu are voie sa rămână in urma: fiecare conține o sesiune Google.
        leftovers = [n for n in os.listdir(tmp) if n.startswith('cookies-')]
        assert leftovers == [], f'copii nesterse: {leftovers}'


def test_a_successful_request_moves_its_rotation_into_the_shared_jar():
    """Izolarea nu are voie sa piarda rotatia.

    YouTube schimba `__Secure-1PSIDTS` si `SIDCC` des, iar valorile noi vin exact
    prin scrierea lui yt-dlp. Daca ele rămân in copia aruncata, sesiunea de pe
    volum imbatraneste pana e refuzata — adica exact problema pe care persistenta
    pe volum exista sa o rezolve.
    """
    with tempfile.TemporaryDirectory() as tmp:
        shared = os.path.join(tmp, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write('# Netscape HTTP Cookie File\n'
                     '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tveche\n')

        fake = _FakeYtDlpModule(0.01)
        saved = _install(fake)
        _reset_gate()
        try:
            asyncio.run(ytdlp.extract({'cookiefile': shared}, 'q'))
        finally:
            ytdlp.yt_dlp = saved
            _reset_gate()

        body = open(shared, encoding='utf-8').read()
        assert 'valoare-noua' in body, (
            'rotatia scrisa de yt-dlp nu a ajuns in jar-ul de pe volum')


def test_a_timeout_leaves_the_shared_jar_alone_even_after_the_thread_writes():
    """Cronologia completa a defectului, prin poarta reala.

    Thread-ul abandonat se inchide DUPA ce timeout-ul a fost raportat si dupa ce o
    revenire a putut rescrie jar-ul comun. Inainte, scrierea lui de la close()
    ateriza in fisierul comun si anula revenirea — a carei singura lovitura pe
    proces era deja consumata.
    """
    with tempfile.TemporaryDirectory() as tmp:
        shared = os.path.join(tmp, 'cookies.txt')
        restored = ('# Netscape HTTP Cookie File\n'
                    '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tRESTAURAT\n')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write('# Netscape HTTP Cookie File\n'
                     '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tmoarta\n')

        fake = _FakeYtDlpModule(0.6)
        saved = _install(fake)
        saved_budget = ytdlp.EXTRACT_TIMEOUT_SEC
        ytdlp.EXTRACT_TIMEOUT_SEC = 0.1
        _reset_gate()
        try:
            async def main():
                try:
                    await ytdlp.extract({'cookiefile': shared}, 'q')
                except TimeoutError:
                    # Aici intervine revenirea, cat timp thread-ul lucreaza inca.
                    with open(shared, 'w', encoding='utf-8') as fh:
                        fh.write(restored)
                    return True
                raise AssertionError('nu a expirat bugetul')

            assert asyncio.run(main()) is True
        finally:
            ytdlp.EXTRACT_TIMEOUT_SEC = saved_budget
            time.sleep(0.9)          # lasa thread-ul abandonat sa se inchida
            ytdlp.yt_dlp = saved
            _reset_gate()

        assert open(shared, encoding='utf-8').read() == restored, (
            'thread-ul abandonat a rescris jar-ul comun si a anulat revenirea')
        leftovers = [n for n in os.listdir(tmp) if n.startswith('cookies-')]
        assert leftovers == [], f'copia thread-ului abandonat a rămas: {leftovers}'


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
