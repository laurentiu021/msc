"""Canarul de YouTube si anunțul de repornire — cele doua semnale de alarma.

Amandoua exista pentru ca botul nu avea NICIUN mod de a spune ca s-a rupt ceva:

- yt-dlp e pinuit si YouTube schimba fara sa anunțe. `default_search` incompatibil
  cu `extract_flat` (cautarea intorcea zero rezultate, tacut) si experimentul
  SABR-only (formatele opus au dispărut) s-au aflat amandoua abia cand cineva a
  incercat sa asculte muzica.
- Railway renunța dupa `restartPolicyMaxRetries`, deci o bucla de crash-uri se
  termina cu botul mort pana observa cineva. Nu exista alerta, iar domeniul public
  a fost sters intenționat, deci nici monitorizare externa.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_canary_and_boot.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DISCORD_TOKEN', 'test-token-nefolosit')

from music import canary


def _report(**kwargs):
    base = {'ok': True, 'search': 5, 'formats': 30, 'opus': True,
            'cookies': True, 'errors': [], 'elapsed': 1.0}
    base.update(kwargs)
    return base


def test_a_dead_text_search_is_reported():
    """Exact bug-ul care a trecut nedetectat: zero rezultate, fara nicio eroare."""
    problems = canary.regressions(_report(search=0), _report())
    assert any('cautarea' in p for p in problems), problems


def test_losing_opus_is_reported_as_a_regression():
    """SABR-only Șterge formatele opus, deci fiecare piesa trece pe reencodare."""
    problems = canary.regressions(_report(opus=False), _report(opus=True))
    assert any('opus' in p for p in problems), problems


def test_a_healthy_check_says_nothing():
    """Un canar care raporteaza zilnic aceeasi stare buna devine zgomot."""
    assert canary.regressions(_report(), _report()) == []


def test_a_state_that_was_already_bad_yesterday_is_not_re_reported():
    """Diferenta conteaza: fara comparatie, aceeasi problema ar fi anunțata la
    infinit si nimeni nu ar mai citi anunțurile."""
    problems = canary.regressions(_report(opus=False), _report(opus=False))
    assert not any('opus' in p for p in problems), problems


def test_the_first_ever_check_still_reports_a_missing_opus():
    """Fara raport anterior nu exista comparatie, dar starea proasta conteaza."""
    problems = canary.regressions(_report(opus=False), None)
    assert any('opus' in p for p in problems), problems


def test_an_extraction_failure_is_reported_even_when_unexpected():
    problems = canary.regressions(
        _report(formats=0, errors=['extractie: RuntimeError: ceva nou']), _report())
    assert any('format' in p for p in problems), problems
    assert any('RuntimeError' in p for p in problems), problems


def test_the_summary_line_is_one_line_and_greppable():
    line = canary.describe(_report(search=0, opus=False))
    assert line.startswith('CANARY '), line
    assert '\n' not in line, line
    assert 'cautare=0' in line and 'opus=0' in line, line


def test_the_canary_never_downloads_anything():
    """O verificare de sanatate nu are ce sa caute in cache-ul de pe volum."""
    import ast
    import inspect

    src = inspect.getsource(canary)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call)}
    for forbidden in ('ytdlp.extract_and_prepare_filename', 'resolve_from_url',
                      'trim_cache', 'write_track_meta'):
        assert forbidden not in called, (
            f'canarul descarca sau atinge cache-ul ({forbidden})')


def test_the_canary_announces_only_regressions():
    said = []

    async def announce(text):
        said.append(text)

    async def fake_run(loop=None):
        return _report(search=0)

    saved = canary.run
    canary.run = fake_run
    try:
        asyncio.run(canary.check_and_report(previous=_report(),
                                            announce=announce))
        assert said and 'Canar' in said[0], said
        said.clear()
        canary.run = lambda loop=None: _healthy()
    finally:
        canary.run = saved


async def _healthy():
    return _report()


def test_a_healthy_canary_stays_silent():
    said = []

    async def announce(text):
        said.append(text)

    saved = canary.run
    canary.run = lambda loop=None: _healthy()
    try:
        asyncio.run(canary.check_and_report(previous=_report(),
                                            announce=announce))
    finally:
        canary.run = saved
    assert said == [], said


def test_a_short_previous_session_is_called_a_crash_loop():
    """Railway renunța dupa cateva incercari, deci o bucla de crash-uri se termina
    cu botul MORT — si asta e singurul moment in care se poate spune."""
    import bot as bot_mod

    said = []

    async def announce(text):
        said.append(text)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, '.last_boot')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('1000.0,1010.0')          # a trait 10 secunde
        saved = (bot_mod._UPTIME_FILE, bot_mod.announce)
        bot_mod._UPTIME_FILE = path
        bot_mod.announce = announce
        try:
            asyncio.run(bot_mod._announce_boot())
        finally:
            bot_mod._UPTIME_FILE, bot_mod.announce = saved

    assert said, 'o repornire nu a fost anunțata deloc'
    assert 'crash' in said[0].lower(), said[0]


def test_a_long_previous_session_is_reported_plainly():
    import bot as bot_mod

    said = []

    async def announce(text):
        said.append(text)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, '.last_boot')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('1000.0,90000.0')         # ~24 de ore
        saved = (bot_mod._UPTIME_FILE, bot_mod.announce)
        bot_mod._UPTIME_FILE = path
        bot_mod.announce = announce
        try:
            asyncio.run(bot_mod._announce_boot())
        finally:
            bot_mod._UPTIME_FILE, bot_mod.announce = saved

    assert said and 'crash' not in said[0].lower(), said
    assert 'uptime' in said[0].lower(), said[0]


def test_the_first_boot_on_a_fresh_volume_says_nothing():
    """Un volum nou nu e o repornire; un anunț acolo ar fi doar zgomot."""
    import bot as bot_mod

    said = []

    async def announce(text):
        said.append(text)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, '.last_boot')
        saved = (bot_mod._UPTIME_FILE, bot_mod.announce)
        bot_mod._UPTIME_FILE = path
        bot_mod.announce = announce
        try:
            asyncio.run(bot_mod._announce_boot())
        finally:
            bot_mod._UPTIME_FILE, bot_mod.announce = saved
        assert os.path.exists(path), 'nu a notat momentul pornirii pentru data viitoare'

    assert said == [], said


def test_a_corrupt_uptime_file_is_not_a_crash():
    """Fisierul e pe volum, deci poate fi trunchiat de un SIGKILL."""
    import bot as bot_mod

    said = []

    async def announce(text):
        said.append(text)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, '.last_boot')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('gunoi')
        saved = (bot_mod._UPTIME_FILE, bot_mod.announce)
        bot_mod._UPTIME_FILE = path
        bot_mod.announce = announce
        try:
            asyncio.run(bot_mod._announce_boot())
        finally:
            bot_mod._UPTIME_FILE, bot_mod.announce = saved

    assert said == [], said


def test_announcing_without_a_channel_configured_is_a_no_op():
    """Neconfigurat trebuie sa insemne "totul merge la fel, doar in loguri"."""
    import bot as bot_mod

    saved = bot_mod.STATUS_CHANNEL_ID
    bot_mod.STATUS_CHANNEL_ID = 0
    try:
        asyncio.run(bot_mod.announce('nu are unde sa ajunga'))
    finally:
        bot_mod.STATUS_CHANNEL_ID = saved


def test_the_canary_runs_from_the_heartbeat():
    """Un canar pe care nu il cheama nimeni nu prinde nimic."""
    import ast
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod._heartbeat)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    assert 'canary.check_and_report' in called, called
    assert bot_mod.CANARY_EVERY_SEC >= 600, (
        f'{bot_mod.CANARY_EVERY_SEC}s e prea des: canarul ar consuma cote si '
        f'slotul de cereri pentru nimic')


def test_the_boot_notice_is_actually_sent_on_ready():
    """Un anunț pe care nu il cheama nimeni nu ajunge nicaieri."""
    import ast
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod.on_ready)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    assert '_announce_boot' in called, called


def test_both_js_runtimes_are_enabled():
    """yt-dlp porneste implicit DOAR cu deno.

    Node e oricum in imagine (ruleaza serverul de PO token) si rezolva acelasi
    challenge — verificat contra YouTube-ului real: cu node activat, 30 de formate
    si itag 251 opus; fara niciun runtime, "n challenge solving failed" si
    formatele opus dispar tacit. Deci amandoua, ca o instalare de deno rupta sa nu
    inseamna jumatate din formate pierdute fara nicio eroare.
    """
    from music.config import JS_RUNTIMES, YDL_OPTS_DOWNLOAD, YDL_OPTS_SEARCH

    assert set(JS_RUNTIMES) == {'deno', 'node'}, JS_RUNTIMES
    for opts, name in ((YDL_OPTS_SEARCH, 'cautare'), (YDL_OPTS_DOWNLOAD, 'descarcare')):
        assert opts.get('js_runtimes') == JS_RUNTIMES, (
            f'opts-urile de {name} nu declara runtime-urile JS: '
            f'{opts.get("js_runtimes")}')


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
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
