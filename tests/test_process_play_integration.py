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

from music import player, state as state_mod
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
        self.saved_ytdlp = (player.ytdlp.extract,
                            player.ytdlp.extract_and_prepare_filename)
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

        player.ytdlp.extract = fake_extract
        player.ytdlp.extract_and_prepare_filename = fake_download
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
        player.ytdlp.extract, player.ytdlp.extract_and_prepare_filename = self.saved_ytdlp
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
    assert player._unplayable_reason(
        {'duration': 200, 'live_status': None}) is None
    assert player._unplayable_reason({'duration': 200}) is None
    # Fara durata (unele extractii nu o dau) nu inventam un refuz.
    assert player._unplayable_reason({'title': 'x'}) is None


def test_an_explicit_short_link_is_still_played():
    """Durata MINIMA e o regula pentru alegerea automata, nu pentru un link dat.

    is_clean o aplica la cautare si autoplay ca sa nu culegem shorts si teasere.
    Pe un link explicit de 20 de secunde, singurul lucru corect e sa il redam.
    """
    assert player._unplayable_reason({'duration': 20, 'live_status': None}) is None


def test_unplayable_reason_catches_upcoming_premieres():
    assert player._unplayable_reason({'live_status': 'is_upcoming'})


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
