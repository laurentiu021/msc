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

    def __init__(self, download_target):
        self.download_target = download_target
        self.extract_calls = []
        self.download_calls = []
        self.cleaned = []

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
                return {'entries': [{
                    'id': 'vid123', 'title': 'Artistul - Piesa', 'duration': 200,
                    'live_status': None,
                    'url': 'https://www.youtube.com/watch?v=vid123',
                }]}
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
            return {'id': 'vid123', 'ext': 'opus'}, self.download_target

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
        player.start_timeout = lambda *a, **k: None
        player.cancel_timeout = lambda *a, **k: None
        player.play_next = lambda *a, **k: None
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


if __name__ == '__main__':
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
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
