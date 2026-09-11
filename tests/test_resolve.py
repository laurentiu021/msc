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

def test_a_text_search_uses_an_explicit_prefix_not_default_search():
    """`default_search` si `extract_flat` sunt INCOMPATIBILE, in tacere.

    Verificat in yt-dlp 2026.8.19: `default_search` nu e aplicat de YoutubeDL, ci de
    extractorul GENERIC (extractor/generic.py:768-793), care intoarce
    `url_result('ytsearch5:' + url)` — adica un rezultat care trebuie PROCESAT ca sa
    devina o cautare. Iar `extract_flat=True` inseamna, in documentatia lui yt-dlp,
    "True: Never process".

    Rezultatul: dict fara `entries`, instantaneu, fara nicio excepție si fara niciun
    warning — botul raspundea "nu am gasit nimic" la ORICE titlu. Fake-ul de mai jos
    reproduce exact semantica aceea, deci testul pica pe codul vechi.
    """
    sent = []

    def generic_semantics(stage, opts):
        sent.append(opts.get('_query'))
        return None

    class _EmulatedYtDlp:
        """Se comporta ca yt-dlp: fara prefix explicit, cautarea nu se intampla."""

        def __init__(self, query):
            self.query = query

        def result(self, opts):
            has_prefix = str(self.query).startswith('ytsearch')
            if not has_prefix and opts.get('extract_flat'):
                # Exact ce intoarce extractorul generic cand nimeni nu proceseaza
                # mai departe url_result-ul: niciun `entries`.
                return {'_type': 'url', 'url': f'ytsearch5:{self.query}',
                        'extractor': 'generic'}
            return {'entries': [
                {'id': 'vid123', 'title': 'Artist - Piesa', 'duration': 200,
                 'live_status': None,
                 'url': 'https://www.youtube.com/watch?v=vid123'}]}

    seen_queries = []

    saved = ytdlp_mod.extract

    async def fake_extract(opts, query, download=False, loop=None, stage=''):
        seen_queries.append(query)
        return _EmulatedYtDlp(query).result(opts)

    ytdlp_mod.extract = fake_extract
    try:
        url, reject = _run(resolve.search_to_url('macarena los del rio'))
    finally:
        ytdlp_mod.extract = saved

    assert seen_queries and seen_queries[0].startswith('ytsearch'), (
        f'cautarea nu duce prefixul explicit: {seen_queries}')
    assert url == 'https://www.youtube.com/watch?v=vid123', (url, reject)
    assert reject is None, reject


def test_the_search_options_never_set_default_search():
    """Garda mecanica: reapariția lui ar dezactiva iar cautarea, in tacere."""
    opts = config.make_search_opts(extract_flat=True)
    assert 'default_search' not in opts or opts['default_search'] is None, (
        "`default_search` a revenit in opts-urile de cautare: cu extract_flat=True "
        "cautarea de text intoarce zero rezultate fara nicio eroare")
    assert config.SEARCH_PREFIX.startswith('ytsearch'), config.SEARCH_PREFIX
    assert config.search_query('ceva') == f'{config.SEARCH_PREFIX}ceva'


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

# --- bucla de descarcare -----------------------------------------------------

def test_a_download_timeout_stops_the_retries():
    """Doi scriitori pe ACELASI fisier, si nu exista lock per id.

    `outtmpl` e `%(id)s.%(ext)s`, deci calea de pe disc E cheia de cache. La
    timeout cererea e abandonata dar thread-ul continua sa descarce, iar bucla
    trecea imediat la urmatorul mod de cookies cu acelasi url, format si outtmpl.
    In yt-dlp 2026.8.19 al doilea scriitor vede `.part`-ul, seteaza `resume_len` si
    `open_mode='ab'` si adauga in fisierul in care primul inca scrie; poate chiar
    decide ca e complet si sa redenumeasca un `.part` care inca creste peste numele
    final din cache. Excluderea `.part` din `cached_download` nu apara de asta.
    """
    from music.errors import YtdlpTimeout

    attempts = []

    def timeout_download(stage, opts):
        attempts.append(bool(opts.get('cookiefile')))
        return YtdlpTimeout('download', 240)

    saved = config._cookies_path
    config._cookies_path = 'cookies-de-test.txt'
    try:
        with _Ytdlp(download=timeout_download) as yt:
            result = _run(resolve.resolve_from_url(VIDEO))
    finally:
        config._cookies_path = saved

    downloads = [s for s in yt.stages if s.startswith('download')]
    assert len(downloads) == 1, (
        f'a mai incercat dupa timeout, in acelasi fisier: {downloads}')
    assert result.filename is None
    assert result.raw_error and 'depasit' in result.raw_error, result.raw_error


def test_the_download_error_is_not_the_extraction_error():
    """Eroarea de la extractie era transmisa in bucla de descarcare ca valoare de
    start, deci poarta HLS decidea pe baza unei erori de la o cerere complet
    diferita, iar garda din apelant nu ajungea sa consulte `last_ydl_reason()`."""
    def extract(stage, opts):
        if stage == 'retry_mweb':
            return {'id': 'vid123', 'title': 'T', 'duration': 200,
                    'webpage_url': VIDEO, 'formats': PLAYABLE}
        # Primul lant eșueaza cu o eroare de FORMAT...
        return {'id': 'vid123', 'title': 'T', 'duration': 200,
                'webpage_url': VIDEO, 'formats': PLAYABLE}

    def download(stage, opts):
        # ...iar descarcarea eșueaza cu altceva complet.
        return RuntimeError('HTTP Error 429: Too Many Requests')

    with _Ytdlp(extract=extract, download=download) as yt:
        result = _run(resolve.resolve_from_url(VIDEO))

    assert result.raw_error and '429' in result.raw_error, (
        f'a raportat altceva decat eroarea descarcarii: {result.raw_error}')
    downloads = [s for s in yt.stages if s.startswith('download')]
    assert len(downloads) == 1, (
        f'un 429 nu devine alt raspuns cu alt selector de format: {downloads}')


def test_a_recovered_retry_hands_its_own_cookie_mode_to_the_download():
    """Reincercarea foloseste WEB_CLIENTS si cookies_available(), dar apelantul
    pastra `(None, False)` de la selectia care eșuase — deci descarcarea pornea cu
    exact modul de cookies care abia dăduse 0 formate redabile."""
    empty = {'id': 'vid123', 'title': 'T', 'duration': 200,
             'webpage_url': VIDEO, 'formats': []}
    recovered = {'id': 'vid123', 'title': 'T', 'duration': 200,
                 'webpage_url': VIDEO, 'formats': PLAYABLE}

    def extract(stage, opts):
        return recovered if stage == 'retry_mweb' else empty

    modes = []

    def download(stage, opts):
        modes.append(bool(opts.get('cookiefile')))
        return {'id': 'vid123', 'ext': 'opus'}, __file__

    saved = config._cookies_path
    saved_sleep = resolve.asyncio.sleep
    config._cookies_path = 'cookies-de-test.txt'

    async def no_sleep(_seconds):
        return None

    resolve.asyncio.sleep = no_sleep
    try:
        with _Ytdlp(extract=extract, download=download):
            result = _run(resolve.resolve_from_url(VIDEO))
    finally:
        config._cookies_path = saved
        resolve.asyncio.sleep = saved_sleep

    assert result.client == config.WEB_CLIENTS, result.client
    assert result.used_cookies is True, (
        'reincercarea a folosit jar-ul, dar rezultatul zice ca nu')
    assert modes and modes[0] is True, (
        f'descarcarea a pornit cu modul care dăduse 0 formate: {modes}')


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


def test_a_human_request_is_not_filtered_against_the_previous_track():
    """Bug-ul prins de emulator: nu puteai pune aceeasi piesa a doua oara.

    `is_clean` respinge orice titlu care conține primele 15 caractere ale piesei
    anterioare — regula gandita pentru autoplay, ca radioul sa nu re-propuna ce a
    dat deja. `process_play` o aplica insa peste ORICE cautare, deci un om care
    cerea a doua data aceeasi piesa (sau o alta piesa a aceluiasi artist, fiindca
    "Luis Gabriel - " se potriveste cu toate ale lui) primea "toate rezultatele au
    fost filtrate".
    """
    import ast
    import inspect

    from music import player

    src = inspect.getsource(player.process_play)
    for node in ast.walk(ast.parse(src.strip())):
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func) != 'search_to_url':
            continue
        avoid = [kw.value for kw in node.keywords if kw.arg == 'avoid_title']
        assert avoid, 'search_to_url nu mai primeste avoid_title deloc'
        expr = ast.unparse(avoid[0])
        assert 'is_radio' in expr, (
            f'avoid_title se aplica si cererilor explicite: {expr}')
        return
    raise AssertionError('process_play nu mai cheama search_to_url')


def test_the_rejection_message_names_the_real_filter():
    """Mesajul fix "live, prea scurte sau prea lungi" minea in majoritatea cazurilor."""
    from music.utils import reject_reason

    assert reject_reason('Piesa', 5, '') == 'prea scurta'
    assert reject_reason('Piesa', 99999, '') == 'prea lunga'
    assert reject_reason('', 100, '') == 'fara titlu'
    assert reject_reason('Luis Gabriel - Piesa 2', 100,
                         'Luis Gabriel - Piesa 1') == (
        'prea asemanatoare cu piesa anterioara')
    assert reject_reason('Luis Gabriel - Piesa 2', 100, '') == ''


def test_the_rejection_message_reaches_the_user_with_the_reason():
    """Explicația trebuie sa ajunga in textul refuzului, nu doar in loguri."""
    import asyncio

    from music import resolve as resolve_mod

    async def fake_extract(opts, query, download=False, loop=None, stage=''):
        return {'entries': [
            {'id': 'a', 'title': 'Artistul - Aceeasi Piesa Lunga', 'duration': 200,
             'live_status': None},
            {'id': 'b', 'title': 'Ceva scurt', 'duration': 5, 'live_status': None},
        ]}

    saved = resolve_mod.ytdlp.extract
    resolve_mod.ytdlp.extract = fake_extract
    try:
        url, reject = asyncio.run(resolve_mod.search_to_url(
            'artistul', avoid_title='Artistul - Aceeasi Piesa Lunga'))
    finally:
        resolve_mod.ytdlp.extract = saved
    assert url is None, url
    assert 'prea asemanatoare' in reject, reject
    assert 'prea scurta' in reject, reject


def test_a_muxed_video_is_the_last_resort_not_the_first_fallback():
    """`best` e un selector MUXAT: descarca video ca sa ia sunetul.

    Vazut real, contra YouTube-ului adevarat: itag 18, 360p avc1 cu mp4a la
    44.1kHz. Zeci de megaocteti de video pe un bot audio, plus o reencodare din
    AAC in loc de `-c:a copy`. Iar HLS-ul audio-only, care era a doua incercare,
    nici nu ajungea sa fie incercat: prima reusea cu video.
    """
    from music.resolve import DOWNLOAD_ATTEMPTS

    selectors = [fmt for fmt, _cap in DOWNLOAD_ATTEMPTS]
    assert 'best' not in selectors[0].split('/'), (
        f'prima incercare cade pe un format muxat: {selectors[0]}')
    audio_only = [i for i, s in enumerate(selectors) if 'bestaudio' in s]
    muxed = [i for i, s in enumerate(selectors)
             if s.split('/')[-1].strip() == 'best']
    assert audio_only, selectors
    assert muxed, 'nu mai exista nicio plasa de siguranța muxata'
    assert min(muxed) > max(audio_only), (
        f'video-ul muxat e incercat inaintea unei variante strict audio: '
        f'{selectors}')


def test_every_download_attempt_has_a_size_cap():
    """Un selector fara plafon poate trage un fisier de orice marime."""
    from music.config import MAX_DOWNLOAD_BYTES
    from music.resolve import DOWNLOAD_ATTEMPTS

    for fmt, cap in DOWNLOAD_ATTEMPTS:
        assert isinstance(cap, int) and 0 < cap <= MAX_DOWNLOAD_BYTES, (fmt, cap)


def _formats(*specs):
    out = []
    for acodec, vcodec in specs:
        out.append({'acodec': acodec, 'vcodec': vcodec, 'url': 'https://x/f',
                    'protocol': 'https', 'format_id': acodec})
    return out


def test_opus_is_recognised_only_when_it_is_audio_only_and_playable():
    from music.config import has_opus_audio

    assert has_opus_audio(_formats(('opus', 'none'))) is True
    assert has_opus_audio(_formats(('mp4a.40.2', 'none'))) is False
    # Muxat: audio-ul lui e opus, dar ar aduce si video pe un bot audio.
    assert has_opus_audio(_formats(('opus', 'avc1.42001E'))) is False
    assert has_opus_audio([]) is False
    assert has_opus_audio(None) is False
    assert has_opus_audio([{'acodec': 'opus', 'vcodec': 'none'}]) is False


def _pick(chain_results):
    """Ruleaza `_pick_format_source` cu extractii controlate per veriga."""
    import asyncio

    from music import resolve as resolve_mod

    calls = []

    async def fake_extract(opts, query, download=False, loop=None, stage=''):
        calls.append(stage)
        return chain_results[len(calls) - 1]

    saved = (resolve_mod.ytdlp.extract, resolve_mod.cookies_available)
    resolve_mod.ytdlp.extract = fake_extract
    resolve_mod.cookies_available = lambda: True
    try:
        out = asyncio.run(resolve_mod._pick_format_source(
            'https://www.youtube.com/watch?v=x', None))
    finally:
        resolve_mod.ytdlp.extract, resolve_mod.cookies_available = saved
    return out, calls


def test_an_aac_only_result_is_not_accepted_while_a_chain_link_remains():
    """Contul din cookies primeste SABR-only, care Șterge formatele opus.

    Aceeasi piesa cerută ca guest are itag 251 opus la ~139 kbps — masurat contra
    YouTube-ului real. Opus trece prin `-c:a copy`; AAC inseamna reencodare, si se
    aude. Lanțul are exact doua verigi, deci costul e O extractie in plus, si numai
    cand prima nu a dat opus.
    """
    aac = {'id': 'x', 'formats': _formats(('mp4a.40.2', 'none'))}
    opus = {'id': 'x', 'formats': _formats(('opus', 'none'))}
    (selected, clients, use_cookies, _err), calls = _pick([aac, opus])
    assert len(calls) == 2, f'nu a mai incercat a doua veriga: {calls}'
    assert use_cookies is False, 'a rămas pe veriga cu cookies, fara opus'
    assert selected is opus


def test_opus_on_the_first_link_costs_no_extra_request():
    opus = {'id': 'x', 'formats': _formats(('opus', 'none'))}
    (selected, clients, use_cookies, _err), calls = _pick([opus, opus])
    assert len(calls) == 1, f'a cerut o extractie in plus degeaba: {calls}'
    assert use_cookies is True
    assert selected is opus


def test_aac_everywhere_is_still_played():
    """Mai bine reencodat decat nimic: fallback-ul nu are voie sa dispara."""
    aac = {'id': 'x', 'formats': _formats(('mp4a.40.2', 'none'))}
    (selected, clients, use_cookies, _err), calls = _pick([aac, dict(aac)])
    assert len(calls) == 2, calls
    assert selected is not None, 'a refuzat sa redea desi exista un format redabil'
    assert clients is not None, 'a pierdut clientul care a functionat'


# --- o singura definitie a extragerii de id ------------------------------------

def test_only_one_module_extracts_a_video_id():
    """Erau trei variante scrise de mana, cu capabilitati diferite.

    resolve stia cinci forme de link, autoplay doua, player doar `v=`. Consecinta
    concreta: un link de `/shorts/` intra in history fara sa ajunga in `skip_ids`,
    deci radioul putea relua exact piesa abia ascultata, iar un seed de shorts oprea
    autoplay-ul cu "can't extract ID". Verificarea e mecanica pentru ca disciplina nu
    se ține minte: a patra copie ar aparea la fel de firesc ca primele trei.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / 'music'
    offenders = []
    for path in sorted(root.glob('*.py')):
        if path.name == 'utils.py':          # singurul loc permis
            continue
        text = path.read_text(encoding='utf-8')
        for needle in ("split('v=')", 'split("v=")', "split('youtu.be/')"):
            if needle in text:
                offenders.append(f'{path.name}: {needle}')
    assert not offenders, f'extragere de id scrisa de mana in: {offenders}'


def test_every_module_gets_the_same_answer():
    """Aliasurile trebuie sa fie chiar acelasi obiect, nu copii care pot divergea."""
    from music import autoplay, player, resolve as resolve_mod, utils

    assert utils.video_id is resolve_mod.video_id
    assert utils.video_id is autoplay.video_id
    assert utils.video_id is player.video_id


def test_autoplay_skips_a_shorts_link_it_already_played():
    """history-ul alimenteaza `skip_ids`; daca id-ul nu se extrage, piesa revine.

    Comportamental, nu pe forma codului: pun in history un link de `/shorts/` si cer
    prefill-ului sa nu il mai adauge. Cu vechiul extractor din autoplay, `skip_ids`
    rămânea gol si piesa intra din nou in coada.
    """
    from music import autoplay
    from music.state import GuildState

    st = GuildState()
    st.last_url = 'https://www.youtube.com/watch?v=seed00'
    st.last_title = 'Artist - Piesa'
    st.history.append({'url': 'https://www.youtube.com/shorts/deja123',
                       'title': 'Altcineva - Deja Ascultata', 'channel': ''})

    async def fake_mix(state, loop, origin_id, skip_ids, needed, artist_counts=None):
        # Exact ce face un Mix real: intoarce si piese deja ascultate.
        added = 0
        for vid, title in (('deja123', 'Altcineva - Deja Ascultata'),
                           ('nou456', 'Cineva - Noua')):
            if autoplay._add_to_queue(state, vid, title, skip_ids, artist_counts):
                added += 1
        return added

    saved = autoplay._try_ytdlp_mix
    autoplay._try_ytdlp_mix = fake_mix
    try:
        _run(autoplay.prefill_autoplay_queue(st, None, target=2))
    finally:
        autoplay._try_ytdlp_mix = saved

    queued = [item['query'] for item in st.queue]
    assert not any('deja123' in q for q in queued), (
        f'a readaugat o piesa din history: {queued}')
    assert any('nou456' in q for q in queued), queued


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
