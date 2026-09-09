"""Butoanele din panou trebuie sa faca exact ce face comanda echivalenta.

Butonul Stop curata acum si fisierul audio curent. Inainte curata doar un camp
mort (`state.preloaded`, care nu primea niciodata altceva decat None) si lasa
fisierul pe disc, spre deosebire de `!stop` — deci fiecare oprire din panou
pierdea un fisier pana la repornirea containerului.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_view_buttons.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import player, state as state_mod, views
from music.state import GuildState

GUILD_ID = 5


class _FakeVoiceClient:
    channel = None

    def __init__(self):
        self.disconnected = False

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self, **kwargs):
        self.disconnected = True


class _FakeCtx:
    def __init__(self, vc):
        self.guild = type('G', (), {'id': GUILD_ID})()
        self.voice_client = vc
        self.bot = type('B', (), {'loop': None})()


class _FakeInteraction:
    class response:
        @staticmethod
        def is_done():
            return False

        @staticmethod
        async def defer():
            return None


def _fresh_state():
    st = GuildState()
    state_mod.guild_states[GUILD_ID] = st
    return st


def _press(name, state):
    """Apasa un buton al panoului, cu seams-urile player-ului inlocuite."""
    saved = (player.cancel_timeout, player.start_timeout)
    player.cancel_timeout = lambda *a, **k: None
    player.start_timeout = lambda *a, **k: None
    try:
        vc = _FakeVoiceClient()
        view = views.MusicControlView(_FakeCtx(vc))
        asyncio.run(getattr(view, name).callback(_FakeInteraction()))
        return vc, view
    finally:
        player.cancel_timeout, player.start_timeout = saved


def test_stop_button_deletes_the_current_audio_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'piesa.opus')
        with open(path, 'wb') as fh:
            fh.write(b'audio')
        st = _fresh_state()
        st.current_file = path
        _press('stop_btn', st)
        # loop=None, deci cleanup_file sterge sincron.
        assert not os.path.exists(path), 'fisierul a rămas pe disc'
        assert st.current_file is None, 'current_file a rămas setat'


def test_stop_button_leaves_the_session_clean():
    st = _fresh_state()
    st.queue = [{'query': 'x', 'title': 'X'}]
    st.autoplay = True
    st.loop_mode = 2
    st.always_on = True
    st.is_loading = True
    generation_before = st.play_generation
    vc, _ = _press('stop_btn', st)
    assert st.queue == [] and st.autoplay is False and st.loop_mode == 0
    assert st.always_on is False and st.is_loading is False
    assert st.play_generation > generation_before, (
        'fara bump, callback-ul piesei oprite avanseaza coada pe care am golit-o')
    assert vc.disconnected, 'botul a rămas in canal'
    assert st.current_msg is None


def test_stop_button_without_a_file_does_not_raise():
    st = _fresh_state()
    st.current_file = None
    _press('stop_btn', st)
    assert st.current_file is None


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
