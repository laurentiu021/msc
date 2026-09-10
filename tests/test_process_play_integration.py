"""process_play, capat la capat, cu yt-dlp si Discord inlocuite.

Batch 3 a rescris calea de redare: opts construite proaspat, un singur modul de
cereri, doua formate in loc de sase, preload eliminat, refolosirea fisierului la
loop. Un refactor pe exact calea care produce audio functional are nevoie de o
plasa care sa nu depinda de un !play manual pe Discord.

Verifica fluxul, nu implementarea: comanda ajunge la redare, fisierul e reținut,
steagul de incarcare e eliberat, iar repetarea nu mai cere nimic de la YouTube.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_process_play_integration.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import (config, player, resolve, state as state_mod, utils,
                   ytdlp as ytdlp_mod)
from music.state import GuildState


class _FakeVoiceClient:
    def __init__(self):
        self.playing = False
        self.played = []
        self.after = None

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def stop(self):
        self.playing = False

    def play(self, source, after=None):
        self.playing = True
        self.played.append(source)
        self.after = after


class _FakeCtx:
    def __init__(self, vc):
        self.voice_client = vc
        self.guild = type('G', (), {'id': 77})()
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append(a[0] if a else k)
        return None


class _Harness:
    """Inlocuieste seams-urile externe ale player-ului si le pune la loc."""

    ATTRS = ('update_player_ui', 'start_timeout', 'cancel_timeout', 'play_next',
             'cleanup_file', '_loop')

    def __init__(self, download_target, full_info=None, flat_entries=None):
        self.download_target = download_target
        self.full_info = full_info
        self.flat_entries = flat_entries
        self.extract_calls = []
        self.download_calls = []
        self.cleaned = []
        self.play_next_calls = []
        self.timeouts = []
        # Ce intoarce descarcarea ca metadata. Testele de reutilizare a
        # metadatelor il inlocuiesc, ca sa verifice ce ajunge in stare.
        self.download_info = {'id': 'vid123', 'ext': 'opus'}

    def __enter__(self):
        self.saved = {a: getattr(player, a, None) for a in self.ATTRS}
        self.saved_ytdlp = (ytdlp_mod.extract,
                            ytdlp_mod.extract_and_prepare_filename)
        self.saved_ffmpeg = player.discord.FFmpegOpusAudio

        async def fake_extract(opts, query, download=False, loop=None, stage=''):
            self.extract_calls.append((stage, dict(opts)))
            if stage == 'search_flat':
                # Cautarea de text e acum FLAT: doar metadata de lista, apoi o
                # singura extractie completa a videoclipului ales.
                return {'entries': self.flat_entries if self.flat_entries is not None else [{
                    'id': 'vid123', 'title': 'Artistul - Piesa', 'duration': 200,
                    'live_status': None,
                    'url': 'https://www.youtube.com/watch?v=vid123',
                }]}
            if self.full_info is not None:
                return self.full_info
            return {
                'id': 'vid123',
                'title': 'Artistul - Piesa',
                'duration': 200,
                'webpage_url': 'https://www.youtube.com/watch?v=vid123',
                'channel': 'Canalul',
                'formats': [{'acodec': 'opus', 'url': 'https://x/audio',
                             'protocol': 'https'}],
            }

        async def fake_download(opts, query, loop=None, stage=''):
            self.download_calls.append((stage, dict(opts)))
            return dict(self.download_info), self.download_target

        class _FakeSource:
            @classmethod
            async def from_probe(cls, filename, **kwargs):
                return cls()

        async def noop(*a, **k):
            return None

        ytdlp_mod.extract = fake_extract
        ytdlp_mod.extract_and_prepare_filename = fake_download
        player.discord.FFmpegOpusAudio = _FakeSource
        player.update_player_ui = noop
        player.start_timeout = lambda *a, **k: self.timeouts.append(a)
        player.cancel_timeout = lambda *a, **k: None
        player.play_next = lambda *a, **k: self.play_next_calls.append(a)
        player.cleanup_file = lambda f, *a, **k: self.cleaned.append(f)
        player._loop = None
        return self

    def __exit__(self, *exc):
        for a, v in self.saved.items():
            setattr(player, a, v)
        ytdlp_mod.extract, ytdlp_mod.extract_and_prepare_filename = self.saved_ytdlp
        player.discord.FFmpegOpusAudio = self.saved_ffmpeg
        return False


def _fresh_state():
    st = GuildState()
    state_mod.guild_states[77] = st
    return st


def test_a_track_plays_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        open(target, 'wb').write(b'audio')
        st = _fresh_state()
        vc = _FakeVoiceClient()
        ctx = _FakeCtx(vc)
        with _Harness(target) as h:
            asyncio.run(player.process_play(ctx, 'artistul piesa'))

        assert vc.played, 'nu s-a chemat vc.play: nimic nu ar cânta'
        assert st.is_loading is False, 'is_loading a rămas blocat'
        assert st.last_title == 'Artistul - Piesa', st.last_title
        assert st.current_file == target
        assert st.last_url.endswith('vid123')
        assert st._consecutive_errors == 0
        assert st.history and st.history[-1]['title'] == 'Artistul - Piesa'
        assert h.download_calls, 'nu s-a descarcat nimic'


def test_download_opts_carry_the_client_that_worked_at_search():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        open(target, 'wb').write(b'audio')
        _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Harness(target) as h:
            asyncio.run(player.process_play(ctx, 'ceva'))
        _, opts = h.download_calls[0]
        clients = opts['extractor_args']['youtube']['player_client']
        assert clients, 'descarcarea nu duce selectia de client'
        assert opts['format'], 'lipseste formatul'


def test_repeat_of_the_same_url_downloads_nothing():
    """loop pe piesa: fisierul e pe disc, deci zero cereri catre YouTube."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        open(target, 'wb').write(b'audio')
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Harness(target) as h:
            asyncio.run(player.process_play(ctx, 'ceva'))
            first_extracts = len(h.extract_calls)
            first_downloads = len(h.download_calls)
            # a doua redare, acelasi URL, exact ce face loop_mode 1
            asyncio.run(player.process_play(ctx, st.last_url))
            assert len(h.extract_calls) == first_extracts, 'a re-extras degeaba'
            assert len(h.download_calls) == first_downloads, 'a re-descarcat degeaba'


def test_looped_file_is_not_deleted_by_the_callback():
    st = _fresh_state()
    st.loop_mode = 1
    deleted = []
    saved = (player.cleanup_file, player.play_next, player._loop)
    player.cleanup_file = lambda f, *a, **k: deleted.append(f)
    player.play_next = lambda *a, **k: None
    player._loop = None
    try:
        cb = player.make_after_play(_FakeCtx(_FakeVoiceClient()), st, 'buclat.opus')
        cb(None)
        assert deleted == [], 'fisierul buclat a fost sters, deci se re-descarca'
    finally:
        player.cleanup_file, player.play_next, player._loop = saved


def test_missing_file_is_reported_not_silently_played():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, 'nu-exista.opus')
        st = _fresh_state()
        vc = _FakeVoiceClient()
        ctx = _FakeCtx(vc)
        with _Harness(missing):
            asyncio.run(player.process_play(ctx, 'ceva'))
        assert not vc.played, 'a incercat sa redea un fisier inexistent'
        assert st.is_loading is False
        assert st._consecutive_errors == 1
        assert ctx.sent, 'utilizatorul nu a fost anuntat'


def _run_with_api_stub(download_info):
    """process_play cu Data API instrumentat, ca sa vedem daca il mai cheama."""
    import tempfile as _tempfile

    api_calls = []

    with _tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        open(target, 'wb').write(b'audio')
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        saved = (player.yt_api.is_available, player.yt_api.get_video_details)
        player.yt_api.is_available = lambda: True
        player.yt_api.get_video_details = lambda ids: (
            api_calls.append(tuple(ids)) or
            {ids[0]: {'views': 999, 'likes': 99, 'channel': 'Din API',
                      'thumbnail': 'http://api', 'duration': 111}})
        async def drive():
            # Completarea din Data API ruleaza prin run_in_executor, deci are
            # nevoie de o bucla reala; harness-ul pune _loop = None.
            player._loop = asyncio.get_running_loop()
            await player.process_play(ctx, 'ceva')

        try:
            with _Harness(target) as h:
                h.download_info = download_info
                asyncio.run(drive())
        finally:
            player.yt_api.is_available, player.yt_api.get_video_details = saved
    return st, api_calls


def test_download_metadata_is_reused_instead_of_buying_it():
    """yt-dlp da view_count si like_count la o extractie completa.

    dl_info era atribuit si niciodata citit, iar panoul se umplea din extractia
    de selectie; apoi fiecare piesa mai platea o unitate de cota, o runda HTTPS si
    un al doilea update de panou pentru date deja aflate in memorie.
    """
    st, api_calls = _run_with_api_stub({
        'id': 'vid123', 'ext': 'opus', 'title': 'Din Download',
        'duration': 222, 'view_count': 12345, 'like_count': 678,
        'channel': 'Canal Download', 'thumbnail': 'http://dl',
    })
    assert st.last_views == 12345 and st.last_likes == 678, (st.last_views, st.last_likes)
    assert st.last_title == 'Din Download', st.last_title
    assert st.last_duration == 222
    assert st.last_channel == 'Canal Download'
    assert api_calls == [], f'a chemat Data API degeaba: {api_calls}'


def test_the_api_still_fills_in_what_ytdlp_did_not_give():
    """Cand extractia nu aduce statistici, completarea rămâne justificata."""
    st, api_calls = _run_with_api_stub({'id': 'vid123', 'ext': 'opus'})
    assert api_calls, 'nu a completat statisticile lipsa'
    assert st.last_views == 999 and st.last_likes == 99


def test_history_entries_carry_the_channel():
    """artist_key cade pe canal cand titlul nu are separator, dar history nu il purta."""
    st, _ = _run_with_api_stub({
        'id': 'vid123', 'ext': 'opus', 'title': 'Manele',
        'channel': 'Canalul Lui', 'view_count': 5, 'like_count': 1,
    })
    assert st.history[-1]['channel'] == 'Canalul Lui', st.history[-1]

    from music.autoplay import artist_key
    assert artist_key(st.history[-1]['title'],
                      st.history[-1]['channel']) == 'canalul lui'


def test_a_cached_file_costs_zero_youtube_requests():
    """Proba centrala a cache-ului, capat la capat.

    Cu fisierul deja pe disc si piesa in history, un !play pe acelasi link nu are
    voie sa faca nici extractie, nici descarcare.
    """
    import music.utils as utils_mod

    with tempfile.TemporaryDirectory() as tmp:
        cached = os.path.join(tmp, 'vid123.opus')
        with open(cached, 'wb') as fh:
            fh.write(b'audio')

        st = _fresh_state()
        st.history = [{'url': 'https://www.youtube.com/watch?v=vid123',
                       'title': 'Artistul - Piesa', 'channel': 'Canalul'}]
        vc = _FakeVoiceClient()
        ctx = _FakeCtx(vc)

        saved_dir = utils_mod.DOWNLOAD_DIR
        utils_mod.DOWNLOAD_DIR = tmp
        saved_trim = player.trim_cache
        player.trim_cache = lambda: None
        try:
            with _Harness('/nu/se/foloseste') as h:
                asyncio.run(player.process_play(
                    ctx, 'https://www.youtube.com/watch?v=vid123'))
        finally:
            utils_mod.DOWNLOAD_DIR = saved_dir
            player.trim_cache = saved_trim

        assert vc.played, 'nu a redat nimic din cache'
        assert st.current_file == cached, st.current_file
        assert h.download_calls == [], f'a descarcat degeaba: {h.download_calls}'
        assert h.extract_calls == [], f'a extras degeaba: {h.extract_calls}'
        assert st.last_title == 'Artistul - Piesa', st.last_title


def test_a_cached_file_without_history_still_gets_its_metadata():
    """Fara titlu in history nu putem afisa nimic, deci extragem — dar tot o data."""
    import music.utils as utils_mod

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'vid123.opus'), 'wb') as fh:
            fh.write(b'audio')
        target = os.path.join(tmp, 'descarcat.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')

        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        saved_dir = utils_mod.DOWNLOAD_DIR
        utils_mod.DOWNLOAD_DIR = tmp
        saved_trim = player.trim_cache
        player.trim_cache = lambda: None
        try:
            with _Harness(target) as h:
                asyncio.run(player.process_play(
                    ctx, 'https://www.youtube.com/watch?v=vid123'))
        finally:
            utils_mod.DOWNLOAD_DIR = saved_dir
            player.trim_cache = saved_trim

        assert st.last_title, 'nicio metadata pentru panou'
        assert h.extract_calls, 'nu a luat metadata'


def _reject_run(full_info, query='https://www.youtube.com/watch?v=vid123',
                state=None):
    """Ruleaza process_play cu un videoclip care trebuie refuzat."""
    st = state or _fresh_state()
    vc = _FakeVoiceClient()
    ctx = _FakeCtx(vc)
    with _Harness('/nu/conteaza', full_info=full_info) as h:
        asyncio.run(player.process_play(ctx, query))
    return st, vc, ctx, h


LIVE_INFO = {
    'id': 'live1', 'title': 'Radio non-stop', 'live_status': 'is_live',
    'is_live': True, 'duration': None,
    'webpage_url': 'https://www.youtube.com/watch?v=live1',
    'formats': [{'acodec': 'opus', 'url': 'https://x/a', 'protocol': 'https'}],
}

LONG_INFO = {
    'id': 'long1', 'title': 'Podcast integral', 'duration': 3 * 3600,
    'webpage_url': 'https://www.youtube.com/watch?v=long1',
    'formats': [{'acodec': 'opus', 'url': 'https://x/a', 'protocol': 'https'}],
}


def test_direct_live_url_is_refused_before_any_download():
    """Regula se aplica si pe URL direct, nu doar pe rezultatele de cautare.

    Inainte, un link de live trecea toata extractia, intra in bucla de
    descarcare si era respins tacut de match_filter; utilizatorul primea
    "Niciun format nu a reusit descarcarea", adica un mesaj de defectiune.
    """
    st, vc, ctx, h = _reject_run(LIVE_INFO)
    assert not h.download_calls, 'a descarcat un live'
    assert not vc.played
    assert st._consecutive_errors == 0, 'un refuz nu e o eroare'
    assert st._consecutive_rejects == 1
    assert ctx.sent, 'utilizatorul nu a fost anuntat'
    assert 'live' in str(ctx.sent[0]).lower(), ctx.sent[0]
    assert st.is_loading is False


def test_direct_overlong_url_is_refused_with_the_real_reason():
    st, vc, ctx, h = _reject_run(LONG_INFO)
    assert not h.download_calls
    assert st._consecutive_errors == 0
    text = str(ctx.sent[0])
    assert '180' in text or 'minute' in text, text
    assert 'necunoscuta' not in text.lower(), 'refuzul a ajuns la diagnose_error'


def test_refusal_advances_the_queue_instead_of_stalling():
    st = _fresh_state()
    st.queue = [{'query': 'altceva', 'title': 'Altceva'}]
    _, _, _, h = _reject_run(LIVE_INFO, state=st)
    assert h.play_next_calls, 'coada a rămas blocata dupa un refuz'


def test_refusal_with_empty_queue_starts_the_idle_timer():
    st = _fresh_state()
    _, _, _, h = _reject_run(LIVE_INFO, state=st)
    assert not h.play_next_calls
    assert h.timeouts, 'nici avans, nici timer: sesiunea rămâne suspendata'


def test_a_queue_full_of_lives_stops_instead_of_grinding_through_it():
    st = _fresh_state()
    st.autoplay = True
    st.queue = [{'query': f'q{i}', 'title': f'T{i}'} for i in range(20)]
    for i in range(player.MAX_CONSECUTIVE_REJECTS):
        _, _, ctx, h = _reject_run(LIVE_INFO, state=st)
        last = (ctx, h)
    ctx, h = last
    assert st.autoplay is False, 'ar continua sa ceara extractii pentru live-uri'
    assert not h.play_next_calls, 'a avansat dupa ce a atins limita'
    assert h.timeouts
    assert 'refuzate' in str(ctx.sent[0]), ctx.sent[0]
    assert st._consecutive_rejects == 0, 'contorul nu s-a resetat la oprire'


def test_a_successful_play_resets_the_reject_counter():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        open(target, 'wb').write(b'audio')
        st = _fresh_state()
        st._consecutive_rejects = 3
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Harness(target):
            asyncio.run(player.process_play(ctx, 'ceva'))
        assert st._consecutive_rejects == 0


def test_search_with_everything_filtered_says_so_plainly():
    st = _fresh_state()
    entries = [
        {'id': 'a', 'title': 'Live acum', 'duration': 20000, 'live_status': 'is_live'},
        {'id': 'b', 'title': 'Scurt', 'duration': 4, 'live_status': None},
    ]
    vc = _FakeVoiceClient()
    ctx = _FakeCtx(vc)
    with _Harness('/nu/conteaza', flat_entries=entries) as h:
        asyncio.run(player.process_play(ctx, 'ceva text'))
    assert not h.download_calls
    assert st._consecutive_errors == 0, 'un filtru complet nu e o defectiune'
    text = str(ctx.sent[0])
    assert 'filtrate' in text, text
    assert 'necunoscuta' not in text.lower(), text


def test_unplayable_reason_passes_normal_tracks():
    assert resolve.unplayable_reason(
        {'duration': 200, 'live_status': None}) is None
    assert resolve.unplayable_reason({'duration': 200}) is None


def test_the_pre_check_agrees_exactly_with_the_download_filter():
    """Doua propozitii despre acelasi lucru nu erau echivalente.

    Verificarea zicea `duration > MAX` iar filtrul de la descarcare
    `duration < MAX`, deci o piesa de EXACT MAX_TRACK_SECONDS — si orice piesa fara
    durata raportata — trecea verificarea, plătea o extractie si o descarcare
    completa, si era apoi refuzata tacut de yt-dlp: ajungea la utilizator ca
    "niciun format nu a reusit descarcarea", adica o defectiune, nu o regula.

    Verificat pe yt-dlp 2026.8.19: filtrul respinge 660, cheia lipsa si None.
    """
    import yt_dlp

    from music.config import MATCH_FILTER_EXPR, MAX_TRACK_SECONDS

    ytdlp_filter = yt_dlp.utils.match_filter_func(MATCH_FILTER_EXPR)
    cases = [
        {'id': 'a', 'title': 'T', 'duration': MAX_TRACK_SECONDS - 1},
        {'id': 'b', 'title': 'T', 'duration': MAX_TRACK_SECONDS},
        {'id': 'c', 'title': 'T', 'duration': MAX_TRACK_SECONDS + 1},
        {'id': 'd', 'title': 'T'},                       # cheia lipseste
        {'id': 'e', 'title': 'T', 'duration': None},
        {'id': 'f', 'title': 'T', 'duration': 20},       # link scurt explicit
    ]
    for info in cases:
        refused_by_ytdlp = ytdlp_filter(dict(info)) is not None
        refused_by_us = resolve.unplayable_reason(dict(info)) is not None
        assert refused_by_us == refused_by_ytdlp, (
            f"durata={info.get('duration', 'LIPSA')}: verificarea zice "
            f"{refused_by_us}, yt-dlp zice {refused_by_ytdlp}")


def test_an_explicit_short_link_is_still_played():
    """Durata MINIMA e o regula pentru alegerea automata, nu pentru un link dat.

    is_clean o aplica la cautare si autoplay ca sa nu culegem shorts si teasere.
    Pe un link explicit de 20 de secunde, singurul lucru corect e sa il redam.
    """
    assert resolve.unplayable_reason({'duration': 20, 'live_status': None}) is None


def test_unplayable_reason_catches_upcoming_premieres():
    assert resolve.unplayable_reason({'live_status': 'is_upcoming'})


def test_a_cache_hit_keeps_the_duration_and_thumbnail():
    """Un hit pornea cu durata 0 si fara thumbnail.

    Panoul pierdea lungimea si tot rândul de timp rămas (ui.py conditioneaza tot
    blocul pe `state.last_duration > 0`), `!seek` rămânea fara plafon (garda e
    scrisa `if state.last_duration and ...`), iar fiindca lipseau si views/likes
    fiecare hit cumpara o unitate de Data API plus un al doilea edit de panou.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        saved_dir = utils.DOWNLOAD_DIR
        utils.DOWNLOAD_DIR = tmp
        try:
            with _Harness(target) as h:
                h.download_info = {'id': 'vid123', 'ext': 'opus', 'duration': 213,
                                   'thumbnail': 'http://t/max.jpg',
                                   'view_count': 4321, 'like_count': 99}
                asyncio.run(player.process_play(ctx, 'ceva'))
                assert st.last_duration == 213, st.last_duration

                # A doua redare, prin cache: history se goleste ca la !stop, deci
                # singura sursa rămâne insoțitorul de langa fisier.
                st.history.clear()
                st.last_url = None
                st.current_file = None
                st.last_duration = 0
                st.last_thumbnail = None
                st.last_views = 0
                extracts = len(h.extract_calls)
                downloads = len(h.download_calls)
                asyncio.run(player.process_play(
                    ctx, 'https://www.youtube.com/watch?v=vid123'))
                assert len(h.download_calls) == downloads, 'a re-descarcat'
                assert len(h.extract_calls) == extracts, 'a re-extras'
        finally:
            utils.DOWNLOAD_DIR = saved_dir

        assert st.last_duration == 213, (
            f'hit-ul de cache a pierdut durata: {st.last_duration}')
        assert st.last_thumbnail == 'http://t/max.jpg', st.last_thumbnail
        assert st.last_views == 4321, (
            f'hit-ul a pierdut statisticile, deci cumpara o unitate de API: '
            f'{st.last_views}')


def test_a_failure_reports_its_own_reason_not_the_previous_track_s():
    """Verdictul unei piese nu are voie sa supravietuiasca piesei.

    "0 formate reale" trăia doar ca mesaj de excepție; de cand se scrie in stare,
    urmatoarea piesa care eșua fara text brut propriu era diagnosticata cu textul
    rămas — si pentru ca diagnoza cadea pe acelasi `error_type`, dedup-ul inghitea
    si mesajul catre utilizator: al doilea eșec nu producea NIMIC pe Discord.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        stale = "Sign in to confirm you're not a bot. Use --cookies"
        st = _fresh_state()
        st.last_raw_error = stale
        ctx = _FakeCtx(_FakeVoiceClient())
        # Extractia nu intoarce niciun candidat si nu ridica nicio excepție: exact
        # calea pe care `resolved.raw_error` rămâne None, deci `state.last_raw_error`
        # nu e suprascris si textul vechi ajunge la diagnoza.
        with _Harness(target, full_info={}):
            asyncio.run(player.process_play(
                ctx, 'https://www.youtube.com/watch?v=altceva'))

        assert st.last_raw_error != stale, (
            'verdictul piesei precedente a supravietuit si e raportat ca cauza')
        text = ' | '.join(str(m) for m in ctx.sent)
        assert text, 'al doilea eșec nu a produs nimic pe Discord'
        assert 'Cookies YouTube expirate' not in text, (
            f'a raportat cauza piesei precedente: {text[:200]}')
        assert 'gasit nimic' in text or 'rezultat' in text.lower(), (
            f'nu a raportat cauza REALA (nimic gasit): {text[:200]}')


# --- promovarea copiei bune de cookies --------------------------------------
# `cookies_valid` e pur STRUCTURALA: se uita doar la numele din jar. Putrezirea
# tipica pastreaza numele si omoara valorile, deci un jar deja refuzat de YouTube
# trece verificarea. Singura aparare rămâne MOMENTUL promovarii: doar o cerere
# care a dus jar-ul si a reusit dovedeste ceva despre el.

_LIVE_JAR = ('# Netscape HTTP Cookie File\n'
             '.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-1PSID\tVALORI-VII\n')
_DEAD_JAR = ('# Netscape HTTP Cookie File\n'
             '.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-1PSID\tvalori-moarte\n')


class _Jar:
    """Un jar mort pe disc si o copie buna alaturi, exact ca in producție."""

    def __init__(self, tmp, current=_DEAD_JAR, good=_LIVE_JAR):
        self.path = os.path.join(tmp, 'cookies.txt')
        self.good_path = self.path + '.good'
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(current)
        with open(self.good_path, 'w', encoding='utf-8') as fh:
            fh.write(good)

    def __enter__(self):
        self._saved = config._cookies_path
        config._cookies_path = self.path
        return self

    def __exit__(self, *exc):
        config._cookies_path = self._saved
        return False

    def good(self) -> str:
        with open(self.good_path, encoding='utf-8') as fh:
            return fh.read()


def test_a_reused_file_never_stamps_the_cookie_jar_as_good():
    """O redare cu ZERO cereri catre YouTube nu dovedeste nimic despre jar.

    Reprodus inainte de fix: loop pe acelasi URL (0 extractii, 0 descarcari)
    copia jar-ul mort peste singura copie care autentificase vreodata. Apoi prima
    piesa noua eșua, revenirea restaura jar-ul mort, `_rolled_back` era consumat,
    si ultimele credentiale bune nu mai existau nicaieri pe disc.
    """
    with tempfile.TemporaryDirectory() as tmp, _Jar(tmp) as jar:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Harness(target) as h:
            asyncio.run(player.process_play(ctx, 'ceva'))
            # Prima redare a folosit jar-ul, deci promovarea ei e legitima; o
            # anulam ca sa masuram exact ce face A DOUA.
            with open(jar.good_path, 'w', encoding='utf-8') as fh:
                fh.write(_LIVE_JAR)
            extracts, downloads = len(h.extract_calls), len(h.download_calls)
            asyncio.run(player.process_play(ctx, st.last_url))
            assert len(h.extract_calls) == extracts, 'redarea refolosita a extras'
            assert len(h.download_calls) == downloads, 'redarea refolosita a descarcat'

        assert jar.good() == _LIVE_JAR, (
            'o redare fara nicio cerere a stampilat jar-ul curent drept "bun": '
            'copia buna a fost distrusa de o redare care nu a autentificat nimic')


def test_a_guest_download_never_stamps_the_cookie_jar_as_good():
    """Bucla de descarcare are propriul fallback la guest.

    Cand fisierul e produs de o cerere FARA jar, succesul nu spune nimic despre
    jar — dar vechea poarta (`if cookies_available()`) il promova oricum.
    """
    with tempfile.TemporaryDirectory() as tmp, _Jar(tmp) as jar:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        modes = []
        with _Harness(target) as h:
            saved = ytdlp_mod.extract_and_prepare_filename

            async def only_guest_works(opts, query, loop=None, stage=''):
                # Inregistram AICI, nu din download_calls: incercarea cu jar-ul
                # nu ajunge niciodata la harness, fiindca o refuzam inainte.
                modes.append(bool(opts.get('cookiefile')))
                if opts.get('cookiefile'):
                    raise RuntimeError('HTTP Error 403: Forbidden')
                return await saved(opts, query, loop=loop, stage=stage)

            ytdlp_mod.extract_and_prepare_filename = only_guest_works
            asyncio.run(player.process_play(ctx, 'ceva'))
        assert h.download_calls, 'nu s-a descarcat nimic'

        assert modes and modes[0] is True, (
            f'descarcarea nu a incercat deloc cu jar-ul: {modes}')
        assert False in modes, f'nu s-a ajuns la varianta de guest: {modes}'
        assert jar.good() == _LIVE_JAR, (
            'o descarcare reusita ca GUEST a fost luata drept dovada ca jar-ul '
            'mai autentifica')


def test_a_cookies_failure_rolls_back_the_jar_and_retries_once():
    """Tranzactia de cookies nu era legata de redare de niciun test.

    Fara asta, ambele puncte de integrare (revenirea si reincercarea) se pot sterge
    cu suita verde: in producție, o sesiune invalidata ar arde 5 erori consecutive,
    ar declanșa intrerupatorul de 900s, ar stinge autoplay si ar cere unui om sa
    lipeasca cookie-uri noi pe Railway — desi copia buna era chiar pe volum.
    """
    with tempfile.TemporaryDirectory() as tmp, _Jar(tmp) as jar:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        config._rolled_back = False
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        attempts = []
        with _Harness(target) as h:
            saved = ytdlp_mod.extract

            async def cookies_are_dead(opts, query, download=False, loop=None,
                                       stage=''):
                attempts.append(stage)
                if stage.startswith('extract'):
                    raise RuntimeError(
                        "Sign in to confirm you're not a bot. Use --cookies")
                return await saved(opts, query, download=download, loop=loop,
                                   stage=stage)

            ytdlp_mod.extract = cookies_are_dead
            asyncio.run(player.process_play(ctx, 'ceva'))

        assert jar.good() == _LIVE_JAR, 'copia buna a fost atinsa'
        with open(jar.path, encoding='utf-8') as fh:
            restored = fh.read()
        assert restored == _LIVE_JAR, (
            'jar-ul mort nu a fost inlocuit cu copia buna: revenirea nu a rulat')
        extracts = [s for s in attempts if s.startswith('extract')]
        assert len(extracts) >= 2, (
            f'nu s-a reincercat dupa revenire: {attempts}')
        assert st._consecutive_errors <= 1, (
            f'a numarat reincercarea ca eroare separata: {st._consecutive_errors}')


def test_the_rollback_retry_happens_at_most_once_per_track():
    """Altfel o sesiune moarta ar produce o recursie de reincercari."""
    with tempfile.TemporaryDirectory() as tmp, _Jar(tmp) as jar:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        config._rolled_back = False
        _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        attempts = []
        with _Harness(target):
            async def always_dead(opts, query, download=False, loop=None, stage=''):
                attempts.append(stage)
                raise RuntimeError(
                    "Sign in to confirm you're not a bot. Use --cookies")

            ytdlp_mod.extract = always_dead
            asyncio.run(player.process_play(ctx, 'ceva'))

        searches = [s for s in attempts if s == 'search_flat']
        assert len(searches) == 2, (
            f'reincercarea nu e limitata la una: {len(searches)} incercari')


def test_a_download_that_used_the_jar_does_stamp_it_as_good():
    """Cealalta jumatate: fara ea, "nu promova" ar fi un fix care rupe revenirea.

    Daca nimic nu mai promoveaza, copia buna nu se reinnoieste niciodata si
    rotatia scrisa de yt-dlp (`__Secure-1PSIDTS` se schimba des) nu ajunge in ea.
    """
    with tempfile.TemporaryDirectory() as tmp, _Jar(tmp) as jar:
        target = os.path.join(tmp, 'vid123.opus')
        with open(target, 'wb') as fh:
            fh.write(b'audio')
        _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Harness(target) as h:
            asyncio.run(player.process_play(ctx, 'ceva'))
            modes = [bool(opts.get('cookiefile')) for _, opts in h.download_calls]

        assert modes and modes[0] is True, f'nu s-a descarcat cu jar-ul: {modes}'
        assert jar.good() == _DEAD_JAR, (
            'descarcarea a folosit jar-ul si a reusit, dar copia buna nu s-a '
            'reinnoit: rotatia scrisa de yt-dlp nu ajunge niciodata in ea')


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
