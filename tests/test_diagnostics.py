"""Instantaneul de diagnostic, verdictul, watchdog-ul si curatenia de pe disc.

`!debug` raporta latenta, CPU si RAM — niciunul dintre lucrurile care chiar cad.
Drumul de la "nu cânta" la "care din cele patru cauze" trecea prin logurile
Railway deschise pe telefon.

Doua proprietati nenegociabile:

1. Valoarea lui YT_PROXY (care poate conține user:parola) nu are voie sa apara in
   ieșire — nici in embed-ul de Discord, nici in JSON-ul de la /status.
2. Watchdog-ul se declanșeaza DOAR la defecte locale, auto-provocate. Niciodata la
   eșecuri de redare: un blocaj YouTube ar lua serviciul complet jos exact cand nu
   e vina noastra.

Curatenia de pe disc e in tests/test_download_cache.py, langa politica de cache
pe care o serveste.

    python tests/test_diagnostics.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# bot.py iese cu sys.exit(1) fara token. Testele nu pornesc nimic — main() e sub
# __main__ — dar importul modulului trebuie sa treaca.
os.environ.setdefault('DISCORD_TOKEN', 'token-de-test')

from music import diag
from music.state import GuildState

NOW = 2_000_000.0


class _FakeBot:
    latency = 0.042
    guilds = [object(), object()]

    def __init__(self, ready=True, closed=False):
        self._ready = ready
        self._closed = closed

    def is_ready(self):
        return self._ready

    def is_closed(self):
        return self._closed


def _snapshot(state=None, **kwargs):
    states = {7: state} if state is not None else {7: GuildState()}
    return diag.build(_FakeBot(), states, now=NOW, pot=(True, 'ok'), **kwargs)


def test_the_snapshot_answers_the_questions_that_matter():
    st = GuildState()
    st.last_title = 'Artist - Piesa'
    st.queue = [{'query': 'x', 'title': 'X'}]
    st._consecutive_errors = 3
    st.breaker_until = NOW + 120
    st.idle_quiet_until = NOW + 30
    st.last_raw_error = 'HTTP Error 429: Too Many Requests'
    st.last_idle_reason = 'radio oprit de utilizator'

    snap = _snapshot(st)
    guild = snap['guilds_detail']['7']
    assert guild['consecutive_errors'] == 3
    assert guild['breaker_sec_left'] == 120
    assert guild['quiet_sec_left'] == 30
    assert '429' in guild['last_error']
    assert guild['last_idle_reason'] == 'radio oprit de utilizator'
    assert guild['playing_title'] == 'Artist - Piesa'
    assert snap['versions']['yt_dlp'] and snap['versions']['discord']
    assert snap['ytdlp']['max_workers'] >= 1
    assert 'units_spent' in snap['data_api']
    assert snap['uptime_sec'] >= 0, 'uptime negativ'


def test_the_snapshot_separates_the_live_gauge_from_the_history():
    """Un singur numar nu poate raspunde la ambele intrebari.

    "E infundat ACUM?" decide reporniri; "cat de des se intampla?" explica o
    seara proasta. Cand era un singur contor cumulativ, prima intrebare primea
    raspunsul celei de-a doua si watchdog-ul repornea procesul la infinit.
    """
    from music import ytdlp

    live, total = ytdlp._mark_leaked()
    try:
        snap = _snapshot()
        assert snap['ytdlp']['leaked_workers'] == live, snap['ytdlp']
        assert snap['ytdlp']['timeouts_total'] == total, snap['ytdlp']
    finally:
        ytdlp._release_leaked(None)

    after = _snapshot()['ytdlp']
    assert after['leaked_workers'] == live - 1, after
    assert after['timeouts_total'] == total, \
        'istoricul a scazut cand thread-ul s-a eliberat'


def test_the_proxy_secret_never_reaches_the_output():
    import json

    secret = 'socks5://utilizator:parolasecreta@proxy.example.com:1080'
    saved = os.environ.get('YT_PROXY')
    os.environ['YT_PROXY'] = secret
    try:
        st = GuildState()
        st.last_raw_error = f'unable to connect through {secret} — HTTP 407'
        snap = _snapshot(st)
        payload = json.dumps({**snap, 'problems': diag.problems(snap)}, default=str)
        assert 'parolasecreta' not in payload, 'parola de proxy a ajuns in /status'
        assert 'proxy.example.com' not in payload, 'host-ul de proxy a ajuns in /status'
        assert '<proxy>' in snap['guilds_detail']['7']['last_error']
    finally:
        if saved is None:
            os.environ.pop('YT_PROXY', None)
        else:
            os.environ['YT_PROXY'] = saved


def test_a_clean_snapshot_reports_no_problems():
    st = GuildState()
    snap = _snapshot(st)
    # Cookie-urile lipsesc pe masina de test, deci filtram exact acea linie.
    issues = [line for line in diag.problems(snap) if 'cookies' not in line]
    assert issues == [], issues


def test_problems_names_each_real_fault():
    st = GuildState()
    st.breaker_until = NOW + 60
    st._consecutive_errors = 5
    snap = _snapshot(st)
    snap['pot_server'] = {'ok': False, 'detail': 'connection refused', 'url': 'x'}
    snap['ytdlp']['leaked_workers'] = snap['ytdlp']['max_workers']
    snap['data_api'] = {'available': False, 'units_spent': 4000, 'daily_cap': 4000}

    issues = ' | '.join(diag.problems(snap))
    for expected in ('PO Token', 'abandonate', 'cota Data API', 'intrerupator',
                     'erori consecutive'):
        assert expected in issues, f'{expected!r} lipseste din: {issues}'


def test_a_jar_without_session_cookies_is_flagged():
    """Numarul de intrari nu spune daca jar-ul mai autentifica.

    Un fisier cu 20 de linii si zero cookie-uri de sesiune e la fel de inutil ca
    unul gol, dar raportul vechi il arata identic cu unul sanatos.
    """
    snap = _snapshot()
    snap['cookies'] = {'exists': True, 'entries': 20, 'age_sec': 60,
                       'valid': False, 'missing_critical': ['SID', 'SAPISID'],
                       'session_cookies': []}
    issues = ' | '.join(diag.problems(snap))
    assert 'sesiune valida' in issues, issues
    assert 'SID' in issues, issues


def test_cookies_on_disk_but_not_in_use_is_a_problem():
    """Singura stare in care NIMIC nu poate cânta era raportata drept sanatoasa.

    `seed_cookies_from_env` intoarce (None, 0) cand YT_COOKIES_CONTENT lipseste,
    deci `apply_cookies` nu e chemat si botul merge ca guest — pe un IP de
    datacenter, adica refuzat la tot. `problems()` citea doar
    exists/entries/valid/age_sec, iar `!health` afisa intrari + varsta.
    """
    snap = _snapshot()
    snap['cookies'] = {'exists': True, 'entries': 2, 'age_sec': 60, 'valid': True,
                       'session_cookies': ['SID'], 'missing_critical': [],
                       'has_good_copy': True, 'in_use': False}
    issues = ' | '.join(diag.problems(snap))
    assert 'guest' in issues, f'starea guest-only nu apare in probleme: {issues}'


def test_a_completely_full_disk_is_reported():
    """Singura stare de disc care nu producea nicio linie era cea mai rea.

    `if free_bytes` colapsa "0 octeti liberi" in acelasi None ca "disk_usage a
    aruncat OSError", iar `problems()` sare peste None — deci `!health` raspundea
    la "de ce nu cânta?" cu "Totul in regula" pe un volum plin.
    """
    import shutil

    saved = shutil.disk_usage

    class _Usage:
        total = 100
        used = 100
        free = 0

    shutil.disk_usage = lambda path: _Usage()
    try:
        snap = _snapshot()
    finally:
        shutil.disk_usage = saved

    assert snap['disk']['free_mb'] == 0, snap['disk']
    issues = ' | '.join(diag.problems(snap))
    assert 'disc' in issues, f'un volum plin nu apare in probleme: {issues}'


def test_an_unreadable_disk_stays_distinct_from_a_full_one():
    import shutil

    saved = shutil.disk_usage
    shutil.disk_usage = lambda path: (_ for _ in ()).throw(OSError('nope'))
    try:
        snap = _snapshot()
    finally:
        shutil.disk_usage = saved
    assert snap['disk']['free_mb'] is None, snap['disk']


def test_an_empty_snapshot_says_so_instead_of_crashing():
    assert diag.problems({}) == ['niciun instantaneu inca']
    assert diag.problems(None) == ['niciun instantaneu inca']


def test_the_cache_is_what_status_serves():
    snap = _snapshot()
    diag.store(snap)
    cached = diag.cached()
    assert cached['uptime_sec'] == snap['uptime_sec']
    assert 'generated_at' in cached, '/status nu poate spune cat de vechi e'
    cached['uptime_sec'] = -1
    assert diag.cached()['uptime_sec'] != -1, 'cache-ul e mutabil din afara'


def test_pot_ping_failure_is_reported_not_raised():
    saved = diag.POT_URL
    diag.POT_URL = 'http://127.0.0.1:1/ping'      # port pe care nimic nu ascultă
    try:
        ok, detail = diag.pot_ping(timeout=0.4)
    finally:
        diag.POT_URL = saved
    assert ok is False and detail, (ok, detail)


# --- watchdog ---------------------------------------------------------------

def _restart(now, last_beat, leaked, max_workers=2, boot=0.0,
             saturated_since=None):
    import bot as bot_mod
    if saturated_since is None:
        # Implicit: saturarea tine de suficient timp, ca testele care verifica
        # ALTE ramuri sa nu depinda de plafonul de persistenta.
        saturated_since = now - bot_mod.WATCHDOG_SATURATED_SEC - 1
    return bot_mod._should_restart(now, last_beat, leaked, max_workers, boot,
                                   saturated_since)


def test_watchdog_stays_quiet_while_healthy():
    assert _restart(now=10_000, last_beat=9_995, leaked=0) is None


def test_watchdog_fires_when_the_event_loop_stops_beating():
    reason = _restart(now=10_000, last_beat=9_000, leaked=0)
    assert reason and 'heartbeat' in reason, reason


def test_watchdog_fires_when_every_ytdlp_thread_is_leaked():
    reason = _restart(now=10_000, last_beat=9_999, leaked=2, max_workers=2)
    assert reason and 'abandonate' in reason, reason


def test_watchdog_tolerates_leaks_below_the_pool_size():
    assert _restart(now=10_000, last_beat=9_999, leaked=1, max_workers=2) is None


def test_watchdog_ignores_a_momentary_saturation():
    """Doua descarcari lente pot depasi bugetul si totusi sa se termine.

    Repornirea la prima citire saturata omoara redarea in curs pentru o stare
    care se rezolva singura in cateva secunde.
    """
    assert _restart(now=10_000, last_beat=9_999, leaked=2, max_workers=2,
                    saturated_since=9_990) is None


def test_watchdog_needs_the_saturation_to_persist():
    import bot as bot_mod
    since = 10_000 - bot_mod.WATCHDOG_SATURATED_SEC
    reason = _restart(now=10_000, last_beat=9_999, leaked=2, max_workers=2,
                      saturated_since=since)
    assert reason and 'abandonate' in reason, reason


def test_saturation_clock_starts_once_and_resets_on_recovery():
    """Contorul de persistenta e jumatatea usor de greșit a verificarii.

    Daca s-ar rescrie la fiecare tick, saturarea nu s-ar acumula niciodata si
    verificarea ar fi moarta. Daca nu s-ar uita la revenire, o saturare veche de
    o ora ar declanșa repornirea la prima reapariție, oricat de scurta.
    """
    import bot as bot_mod
    start = bot_mod._saturation_start(1_000.0, 2, 2, 0.0)
    assert start == 1_000.0, start
    assert bot_mod._saturation_start(1_015.0, 2, 2, start) == 1_000.0, \
        'momentul se rescrie la fiecare tick: saturarea nu se acumuleaza niciodata'
    assert bot_mod._saturation_start(1_030.0, 1, 2, start) == 0.0, \
        'un thread s-a eliberat, dar ceasul de saturare a rămas pornit'
    assert bot_mod._saturation_start(2_000.0, 2, 2, 0.0) == 2_000.0, \
        'saturarea noua trebuie masurata de acum, nu de la episodul vechi'


def test_a_lifetime_timeout_counter_can_never_arm_the_watchdog():
    """Contorul citit de watchdog trebuie sa poata SCADEA.

    Bug-ul original: ytdlp.leaked_workers() numara timeout-urile de la pornire,
    deci al doilea timeout din viata containerului (o seara normala) armă
    os._exit(1) la fiecare tick de 15s, pana la epuizarea celor 10 reporniri
    permise de Railway — si de atunci serviciul rămânea jos.
    """
    from music import ytdlp

    before = ytdlp.leaked_workers()
    live, total = ytdlp._mark_leaked()
    try:
        assert live == before + 1, (live, before)
        assert total >= 1
    finally:
        ytdlp._release_leaked(None)
    assert ytdlp.leaked_workers() == before, (
        'leaked_workers() nu scade la loc, deci e un istoric, nu un indicator')
    assert ytdlp.leaked_workers_total() == total, \
        'istoricul nu are voie sa scada'


def test_watchdog_is_silent_during_the_boot_window():
    """Altfel o problema la pornire arde bugetul de 10 reporniri al Railway."""
    import bot as bot_mod
    boot = 10_000 - (bot_mod.WATCHDOG_STALL_SEC - 5)
    assert _restart(now=10_000, last_beat=0, leaked=99, boot=boot) is None


def test_watchdog_ignores_playback_failures():
    """Erorile de redare nu sunt un motiv de repornire: ar lua serviciul jos."""
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod._should_restart)
    for forbidden in ('_consecutive_errors', 'breaker_until', 'last_raw_error',
                      'queue'):
        assert forbidden not in src, (
            f'watchdog-ul se uita la {forbidden}: un blocaj YouTube ar reporni botul')


# --- /status ----------------------------------------------------------------

def _get(path, token=None, header_token=None):
    """Cheama handler-ul HTTP fara socket: fara server, fara firewall, fara port."""
    import bot as bot_mod

    handler = object.__new__(bot_mod._Health)
    handler.path = path if token is None else f'{path}?token={token}'
    handler.headers = {'X-Status-Token': header_token} if header_token else {}
    captured = {}
    handler._respond = lambda code, ctype, body: captured.update(
        code=code, content_type=ctype, body=body)
    handler.send_error = lambda code, *a, **k: captured.update(code=code, body=b'')
    handler.do_GET()
    return captured


def _with_token(value):
    """Setter/restaurator pentru STATUS_TOKEN."""
    saved = os.environ.get('STATUS_TOKEN')
    if value is None:
        os.environ.pop('STATUS_TOKEN', None)
    else:
        os.environ['STATUS_TOKEN'] = value
    return saved


def test_status_serves_the_cached_snapshot_as_json():
    import json

    saved = _with_token('secret-de-test')
    try:
        diag.store(_snapshot())
        resp = _get('/status', token='secret-de-test')
        assert resp['code'] == 200, resp
        assert resp['content_type'] == 'application/json'
        payload = json.loads(resp['body'])
        assert 'problems' in payload and 'guilds_detail' in payload
        assert 'generated_at' in payload, 'nu se poate spune cat de vechi e raportul'
    finally:
        _with_token(saved)


def test_status_is_404_without_a_token_configured():
    """Fail-CLOSED. Portul de healthcheck devine public in clipa in care
    serviciului i se atașeaza un domeniu, iar repo-ul e public: calea /status e
    cunoscuta oricui citeste codul. Fara STATUS_TOKEN, endpoint-ul nu exista."""
    saved = _with_token(None)
    try:
        diag.store(_snapshot())
        assert _get('/status')['code'] == 404
        assert _get('/status', token='orice')['code'] == 404
    finally:
        _with_token(saved)


def test_status_rejects_a_wrong_token():
    saved = _with_token('bun')
    try:
        diag.store(_snapshot())
        assert _get('/status', token='greșit')['code'] == 404
        assert _get('/status')['code'] == 404, 'a servit fara niciun token'
        assert _get('/status', header_token='bun')['code'] == 200, 'header-ul nu merge'
    finally:
        _with_token(saved)


def test_the_status_payload_carries_no_credentials():
    """Nu are credentiale, dar are ID de guild, ce se reda si calea de cookies —
    de aceea e in spatele unui token, nu de aceea e nevinovat."""
    import json

    saved_token = _with_token('t')
    saved_env = {k: os.environ.get(k) for k in
                 ('DISCORD_TOKEN', 'YOUTUBE_API_KEY', 'YT_COOKIES_CONTENT')}
    os.environ['DISCORD_TOKEN'] = 'MTA-token-secret-de-discord'
    os.environ['YOUTUBE_API_KEY'] = 'AIzaCheieSecretaDeApi'
    os.environ['YT_COOKIES_CONTENT'] = 'valoare-secreta-de-cookie'
    try:
        diag.store(_snapshot())
        body = json.dumps(json.loads(_get('/status', token='t')['body']))
        for secret in ('MTA-token-secret-de-discord', 'AIzaCheieSecretaDeApi',
                       'valoare-secreta-de-cookie'):
            assert secret not in body, f'{secret[:12]}... a ajuns in /status'
    finally:
        _with_token(saved_token)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_status_says_503_before_the_first_heartbeat():
    saved = _with_token('t')
    try:
        diag._CACHE = {}
        resp = _get('/status', token='t')
        assert resp['code'] == 503, resp
    finally:
        _with_token(saved)


def test_status_never_blocks_the_handler():
    """Serverul e single-threaded: un apel de rețea in handler intarzie fiecare sonda."""
    import ast
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod._Health._status)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
             if isinstance(n, ast.Call)}
    assert 'diag.cached' in calls, calls
    for forbidden in ('diag.build', 'diag.refresh', 'diag.pot_ping'):
        assert forbidden not in calls, f'{forbidden} blocheaza handler-ul HTTP'


def test_health_stays_plain_text_for_railway():
    """/health NU cere token: Railway il sondeaza si nu scurge nimic."""
    saved = _with_token(None)
    diag.store(_snapshot())
    resp = _get('/health')
    _with_token(saved)
    assert resp['content_type'] == 'text/plain', resp
    assert resp['body'] in (b'ok', b'starting')


def test_unknown_paths_are_404():
    assert _get('/admin')['code'] == 404


def test_the_healthcheck_does_not_depend_on_cookies():
    """Un jar expirat nu are voie sa blocheze chiar deploy-ul care aduce unul nou."""
    import ast
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod._Health.do_GET)
    tree = ast.parse(src.strip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert 'cookie_status' not in names and 'cookies_available' not in names
    assert 'cookies' not in attrs


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
