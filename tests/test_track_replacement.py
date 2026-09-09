"""Inlocuirea unei piese trebuie sa preia proprietatea celei vechi: thread SI fisier.

Trei defecte gasite de al doilea audit, toate invizibile in functionare:

1. Pauza. discord.py raporteaza `is_playing() == False` cat timp e pauzat, iar
   `VoiceClient.play` verifica doar `is_playing()`. Deci `!nplay` peste o piesa
   pauzata suprascria `_player` si lasa thread-ul vechi parcat in
   `_resumed.wait()`, cu procesul lui ffmpeg viu pana la oprirea containerului.
   Audio-ul se auzea normal, deci nimic nu semnala problema.

2. Token-ul de generatie a suprimat singurul cleanup al fisierului inlocuit:
   callback-ul invechit iese inainte de `cleanup_file`, iar `state.current_file`
   e deja suprascris cu piesa noua, deci calea celei vechi se pierde.

3. `state.last_raw_error` nu se cura niciodata, deci eroarea unei piese era
   raportata drept cauza pentru alta, mult mai tarziu.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_track_replacement.py
"""
import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import player, state as state_mod
from music.state import GuildState


class _VoiceClient:
    """Modeleaza semantica reala din discord.py 2.7.1.

    is_playing() -> _resumed set AND not _end set, deci FALSE cand e pauzat.
    play() se plânge doar daca is_playing(), deci trece cand e pauzat.
    stop() e singurul care elibereaza player-ul curent.
    """

    def __init__(self):
        self.player = None
        self.paused = False
        self.orphaned = []
        self.calls = []

    def is_connected(self):
        return True

    def is_playing(self):
        return self.player is not None and not self.paused

    def is_paused(self):
        return self.player is not None and self.paused

    def pause(self):
        self.paused = True

    def stop(self):
        self.calls.append('stop')
        self.player = None
        self.paused = False

    def play(self, source, after=None):
        self.calls.append('play')
        if self.player is not None:
            # exact ce face discord.py: suprascrie _player fara sa se plânga,
            # iar thread-ul vechi nu mai poate fi atins de nimeni
            self.orphaned.append(self.player)
        self.player = source
        self.paused = False
        self.after = after


class _Ctx:
    def __init__(self, vc):
        self.voice_client = vc
        self.guild = type('G', (), {'id': 99})()
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append(a[0] if a else k)
        return None


class _Harness:
    def __init__(self, target):
        self.target = target
        self.cleaned = []

    def __enter__(self):
        self.saved = {a: getattr(player, a, None) for a in
                      ('update_player_ui', 'start_timeout', 'cancel_timeout',
                       'play_next', 'cleanup_file', '_loop')}
        self.saved_ytdlp = (player.ytdlp.extract,
                            player.ytdlp.extract_and_prepare_filename)
        self.saved_ffmpeg = player.discord.FFmpegOpusAudio

        async def fake_extract(opts, query, download=False, loop=None, stage=''):
            if stage == 'search_flat':
                return {'entries': [{'id': 'vid999', 'title': 'Artist - Noua',
                                     'duration': 200, 'live_status': None,
                                     'url': 'https://www.youtube.com/watch?v=vid999'}]}
            return {'id': 'vid999', 'title': 'Artist - Noua', 'duration': 200,
                    'webpage_url': 'https://www.youtube.com/watch?v=vid999',
                    'channel': 'Canal',
                    'formats': [{'acodec': 'opus', 'url': 'https://x', 'protocol': 'https'}]}

        async def fake_download(opts, query, loop=None, stage=''):
            return {'id': 'vid999'}, self.target

        class _Src:
            @classmethod
            async def from_probe(cls, filename, **kw):
                return cls()

        async def noop(*a, **k):
            return None

        player.ytdlp.extract = fake_extract
        player.ytdlp.extract_and_prepare_filename = fake_download
        player.discord.FFmpegOpusAudio = _Src
        player.update_player_ui = noop
        player.start_timeout = lambda *a, **k: None
        player.cancel_timeout = lambda *a, **k: None
        player.play_next = lambda *a, **k: None
        player.cleanup_file = lambda f, *a, **k: self.cleaned.append(f)
        player._loop = None
        return self

    def __exit__(self, *e):
        for a, v in self.saved.items():
            setattr(player, a, v)
        player.ytdlp.extract, player.ytdlp.extract_and_prepare_filename = self.saved_ytdlp
        player.discord.FFmpegOpusAudio = self.saved_ffmpeg
        return False


def _state():
    st = GuildState()
    state_mod.guild_states[99] = st
    return st


def test_replacing_a_paused_track_stops_it_first():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'noua.opus')
        open(target, 'wb').write(b'audio')
        st = _state()
        st.current_file = os.path.join(tmp, 'veche.opus')
        open(st.current_file, 'wb').write(b'audio')
        vc = _VoiceClient()
        vc.play(object())          # piesa veche
        vc.pause()                 # utilizatorul apasa Pause
        vc.calls.clear()           # ne interesa doar ce face process_play
        with _Harness(target):
            asyncio.run(player.process_play(_Ctx(vc), 'ceva nou'))
        assert 'stop' in vc.calls, (
            'nu s-a chemat stop pe piesa pauzata: thread-ul si ffmpeg-ul ei rămân orfani')
        assert vc.orphaned == [], f'player orfan: {vc.orphaned}'


def test_replacing_a_playing_track_also_stops_it():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'noua.opus')
        open(target, 'wb').write(b'audio')
        st = _state()
        vc = _VoiceClient()
        vc.play(object())
        vc.calls.clear()
        with _Harness(target):
            asyncio.run(player.process_play(_Ctx(vc), 'ceva nou'))
        assert vc.calls[0] == 'stop', f'ordinea apelurilor: {vc.calls}'
        assert vc.orphaned == []


def test_displaced_file_is_deleted():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'noua.opus')
        old = os.path.join(tmp, 'veche.opus')
        for f in (target, old):
            open(f, 'wb').write(b'audio')
        st = _state()
        st.current_file = old
        vc = _VoiceClient()
        vc.play(object())
        vc.calls.clear()
        with _Harness(target) as h:
            asyncio.run(player.process_play(_Ctx(vc), 'ceva nou'))
        assert old in h.cleaned, (
            f'fisierul inlocuit nu a fost sters: {h.cleaned}; fiecare !nplay lasa unul pe disc')


def test_displaced_file_is_kept_when_it_is_the_same_file():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'aceeasi.opus')
        open(target, 'wb').write(b'audio')
        st = _state()
        st.current_file = target
        st.last_url = 'https://www.youtube.com/watch?v=vid999'
        vc = _VoiceClient()
        vc.play(object())
        vc.calls.clear()
        with _Harness(target) as h:
            asyncio.run(player.process_play(_Ctx(vc), st.last_url))
        assert target not in h.cleaned, 'a sters fisierul pe care tocmai il reda'


def test_last_raw_error_is_cleared_on_success():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, 'noua.opus')
        open(target, 'wb').write(b'audio')
        st = _state()
        st.last_raw_error = 'HTTP Error 429: Too Many Requests'
        with _Harness(target):
            asyncio.run(player.process_play(_Ctx(_VoiceClient()), 'ceva'))
        assert st.last_raw_error is None, (
            'eroarea veche rămâne si va fi raportata drept cauza pentru alta piesa')


def test_silent_filter_rejection_produces_a_reason():
    """match_filter respinge fara sa ridice nimic si fara sa scrie in log."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, 'niciodata-scris.opus')
        st = _state()
        st.last_raw_error = None
        ctx = _Ctx(_VoiceClient())
        with _Harness(missing):
            asyncio.run(player.process_play(ctx, 'ceva'))
        assert st.last_raw_error, 'nicio explicatie pentru un fisier care nu s-a scris'
        assert 'filtru' in st.last_raw_error.lower() or 'durata' in st.last_raw_error.lower()


def test_only_the_latest_owner_clears_the_loading_flag():
    """Doua process_play suprapuse: primul nu are voie sa deblocheze al doilea."""
    st = _state()
    st.is_loading = True
    st.load_token = 5
    src = inspect.getsource(player.process_play)
    assert 'state.load_token == my_load_token' in src, (
        'finally-ul elibereaza is_loading fara sa verifice proprietatea')


def test_interrupted_playback_is_not_counted_as_a_failure():
    assert issubclass(player.PlaybackInterrupted, Exception)
    assert not issubclass(player.PlaybackInterrupted, ConnectionError), (
        'un tip propriu: ConnectionError acopera si erorile reale de retea, '
        'care trebuie sa rămâna vizibile')
    src = inspect.getsource(player.process_play)
    assert 'except PlaybackInterrupted' in src


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
