"""Negocierea cu yt-dlp, testata direct: fara ctx, fara voce, fara stare.

Inainte, ca sa verifici o singura afirmatie despre opts-urile de descarcare
trebuia un client de voce fals, un ctx fals si un harness care inlocuia sase
globale din player, doua functii din ytdlp si FFmpegOpusAudio. Intrebarile de mai
jos erau practic imposibil de pus:

- cate cereri costa o piesa refuzata?
- lantul de guest chiar ruleaza dupa cel cu cookies?
- eroarea raportata e a ACESTEI rezolvari, sau a uneia de acum o ora?

Ultima intrebare era chiar un defect: `state.last_raw_error` era o cutie poștala
intre piese, folosita ca sa treaca text peste o granita raise/except, niciodata
golita la succes, si preferata excepției reale la raportare. Acum textul se
intoarce ca valoare, deci nu poate supravietui piesei care l-a produs.

    python tests/test_resolve.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import config, resolve, ytdlp as ytdlp_mod

VIDEO = 'https://www.youtube.com/watch?v=vid123'
PLAYABLE = [{'acodec': 'opus', 'url': 'https://x/a', 'protocol': 'https'}]


class _Ytdlp:
    """Inlocuieste poarta catre yt-dlp si retine fiecare cerere."""

    def __init__(self, extract=None, download=None):
        self.calls = []
        self._extract = extract
        self._download = download

    def __enter__(self):
        self.saved = (ytdlp_mod.extract, ytdlp_mod.extract_and_prepare_filename)

        async def extract(opts, query, download=False, loop=None, stage=''):
            self.calls.append((stage, opts))
            if self._extract is None:
                return {'id': 'vid123', 'title': 'Artist - Piesa', 'duration': 200,
                        'webpage_url': VIDEO, 'formats': PLAYABLE}
            result = self._extract(stage, opts)
            if isinstance(result, Exception):
                raise result
            return result

        async def download(opts, query, loop=None, stage=''):
            self.calls.append((stage, opts))
            if self._download is None:
                raise AssertionError('descarcarea nu era aȘteptata in acest test')
            result = self._download(stage, opts)
            if isinstance(result, Exception):
                raise result
            return result

        ytdlp_mod.extract = extract
        ytdlp_mod.extract_and_prepare_filename = download
        return self

    def __exit__(self, *exc):
        ytdlp_mod.extract, ytdlp_mod.extract_and_prepare_filename = self.saved
        return False

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


def _run(coro):
    return asyncio.run(coro)


# --- cate cereri costa o piesa refuzata --------------------------------------

def test_a_live_stream_costs_one_request_and_no_download():
    """Intrebarea care nu se putea pune inainte, in trei linii."""
    live = {'id': 'live1', 'title': 'Radio non-stop', 'is_live': True,
            'live_status': 'is_live', 'webpage_url': VIDEO, 'formats': PLAYABLE}
    with _Ytdlp(extract=lambda stage, opts: live) as yt:
        result = _run(resolve.resolve_from_url(VIDEO))

    assert result.reject_reason and 'live' in result.reject_reason.lower()
    assert len(yt.calls) == 1, f'a facut {len(yt.calls)} cereri: {yt.stages}'
    assert not any('download' in s for s in yt.stages)
    assert result.filename is None


def test_an_overlong_track_is_refused_before_the_download():
    long_track = {'id': 'x', 'title': 'Colaj', 'duration': 3 * 3600,
                  'webpage_url': VIDEO, 'formats': PLAYABLE}
    with _Ytdlp(extract=lambda stage, opts: long_track) as yt:
        result = _run(resolve.resolve_from_url(VIDEO))
    assert result.reject_reason and 'minute' in result.reject_reason
    assert not any('download' in s for s in yt.stages)


# --- lantul de clienti --------------------------------------------------------

def test_the_guest_chain_runs_after_cookies_fail():
    """Cookies primele (IP de datacenter), guest ca rezerva."""
    saved = config._cookies_path
    config._cookies_path = 'cookies-de-test.txt'      # cookies_available() -> True
    try:
        def extract(stage, opts):
            if 'cookiefile' in opts:
                return RuntimeError('HTTP Error 429: Too Many Requests')
            return {'id': 'vid123', 'title': 'T', 'duration': 200,
                    'webpage_url': VIDEO, 'formats': PLAYABLE}

        with _Ytdlp(extract=extract,
                    download=lambda stage, opts: ({'id': 'vid123'}, None)) as yt:
            result = _run(resolve.resolve_from_url(VIDEO))
    finally:
        config._cookies_path = saved

    extract_calls = [opts for stage, opts in yt.calls if stage.startswith('extract_')]
    assert len(extract_calls) == 2, f'lantul nu a continuat: {yt.stages}'
    assert 'cookiefile' in extract_calls[0], 'cookies nu au fost primele'
    assert 'cookiefile' not in extract_calls[1], 'a doua incercare tot cu cookies'
    assert result.used_cookies is False
    assert '429' in (result.raw_error or ''), result.raw_error


def test_the_download_carries_the_client_that_worked():
    seen = {}

    def download(stage, opts):
        seen.update(opts)
        return ({'id': 'vid123'}, None)

    with _Ytdlp(download=download):
        result = _run(resolve.resolve_from_url(VIDEO))

    assert seen['extractor_args']['youtube']['player_client'], seen['extractor_args']
    assert seen['format'], 'lipseste selectorul de format'
    assert seen['max_filesize'] == config.MAX_DOWNLOAD_BYTES
    assert result.client == config.WEB_CLIENTS


# --- eroarea raportata e a ACESTEI rezolvari ----------------------------------

def test_the_error_belongs_to_this_resolution_only():
    """Nu exista cutie poștala: fiecare rezolvare isi duce propriul text."""
    def failing(stage, opts):
        return RuntimeError('Sign in to confirm you are not a bot')

    with _Ytdlp(extract=failing):
        first = _run(resolve.resolve_from_url(VIDEO))
    assert 'Sign in' in first.raw_error

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        with _Ytdlp(download=lambda stage, opts: ({'id': 'vid123'}, target)):
            second = _run(resolve.resolve_from_url(VIDEO))
        assert second.raw_error is None, (
            f'eroarea rezolvarii anterioare a supravietuit: {second.raw_error}')


def test_a_successful_resolution_reports_no_error():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        with _Ytdlp(download=lambda stage, opts: ({'id': 'vid123'}, target)):
            result = _run(resolve.resolve_from_url(VIDEO))
        # In interiorul blocului: fisierul trebuie sa existe cand verificam `ok`.
        assert result.ok is True, (result.filename, result.raw_error)
        assert result.raw_error is None
        assert result.reject_reason is None
        assert result.download_info == {'id': 'vid123'}


# --- oprire intre etape -------------------------------------------------------

def test_a_stop_between_stages_aborts_before_the_download():
    """Verificarea se facea doar la final, dupa ce toate cererile erau plătite."""
    with _Ytdlp() as yt:
        result = _run(resolve.resolve_from_url(VIDEO, should_continue=lambda: False))
    assert result.interrupted is True
    assert not any('download' in s for s in yt.stages), yt.stages
    assert result.reject_reason is None, 'o oprire nu e un refuz'


# --- cautarea -----------------------------------------------------------------

def test_a_url_is_not_searched():
    with _Ytdlp() as yt:
        url, reject = _run(resolve.search_to_url(VIDEO))
    assert url == VIDEO and reject is None
    assert yt.calls == [], 'a cerut o cautare pentru un link'


def test_text_becomes_one_flat_search():
    entries = {'entries': [
        {'id': 'a', 'title': 'Artist - Piesa', 'duration': 200, 'live_status': None,
         'url': 'https://www.youtube.com/watch?v=a'}]}
    with _Ytdlp(extract=lambda stage, opts: entries) as yt:
        url, reject = _run(resolve.search_to_url('artist piesa'))
    assert url.endswith('v=a') and reject is None
    assert yt.stages == ['search_flat'], yt.stages
    assert yt.calls[0][1]['extract_flat'] is True


def test_everything_filtered_is_a_rejection_not_an_error():
    entries = {'entries': [
        {'id': 'a', 'title': 'Live acum', 'duration': 20000, 'live_status': 'is_live'},
        {'id': 'b', 'title': 'Scurt', 'duration': 3, 'live_status': None}]}
    with _Ytdlp(extract=lambda stage, opts: entries):
        url, reject = _run(resolve.search_to_url('ceva'))
    assert url is None
    assert reject and 'filtrate' in reject


def test_nothing_found_is_neither_a_url_nor_a_rejection():
    with _Ytdlp(extract=lambda stage, opts: {'entries': []}):
        url, reject = _run(resolve.search_to_url('ceva'))
    assert url is None and reject is None, (url, reject)


def test_the_search_avoids_repeating_the_current_track():
    entries = {'entries': [
        {'id': 'a', 'title': 'Artist - Aceeasi Piesa', 'duration': 200,
         'live_status': None, 'url': 'https://www.youtube.com/watch?v=a'},
        {'id': 'b', 'title': 'Artist - Altceva', 'duration': 200,
         'live_status': None, 'url': 'https://www.youtube.com/watch?v=b'}]}
    with _Ytdlp(extract=lambda stage, opts: entries):
        url, _ = _run(resolve.search_to_url('artist', avoid_title='Artist - Aceeasi Piesa'))
    assert url.endswith('v=b'), f'a ales aceeasi piesa: {url}'


# --- granita: resolve nu are voie sa stie de stare ---------------------------

def test_resolve_knows_nothing_about_guild_state_or_discord():
    """Motivul intregii separari; un import ar aduce inapoi cuplajul.

    Pe AST, nu pe text: docstring-ul modulului chiar spune "nici GuildState", si
    o verificare pe text s-ar agata de propria explicatie.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(resolve.__file__).read_text(encoding='utf-8'))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or '')
            modules |= {f'{node.module}.{a.name}' for a in node.names}
    assert not any(m.startswith('discord') for m in modules), modules
    assert not any('music.state' in m for m in modules), modules

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ('GuildState', 'ctx', 'last_raw_error', 'guild_states'):
        assert forbidden not in names | attrs, f'resolve.py atinge {forbidden!r}'


def test_the_player_no_longer_holds_a_second_copy_of_the_rules():
    import pathlib

    src = pathlib.Path(os.path.join(os.path.dirname(resolve.__file__),
                                    'player.py')).read_text(encoding='utf-8')
    for moved in ('DOWNLOAD_ATTEMPTS = [', 'def _unplayable_reason',
                  'def _worth_another_format', 'COOKIE_CHAIN = ['):
        assert moved not in src, f'player.py mai are o copie a {moved!r}'


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
