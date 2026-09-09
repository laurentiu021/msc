"""Nicio comanda nu are voie sa porneasca o a doua rezolvare peste una in curs.

Trei intrari nu se uitau la nimic ocupat:

- `!nplay` nu avea NICIUN gard: `skip_request = True; await process_play(...)`,
  deci `!play X` urmat de `!nplay Y` la doua secunde distanta insemna doua
  rezolvari cu cookies in paralel, pe un IP care ne limiteaza.
- ramura de playlist a lui `!play` extrage pana la 30 de intrari INAINTE de
  verificarea de ocupat, deci un `!play` dat in acel timp nu vedea nimic ocupat.
- butonul Autoplay cere un Mix de pana la 50 de intrari, cu cookies, fara sa
  consulte nimic.

Testele conduc comenzile reale prin `setup_music_commands`, care primeste toate
dependentele prin injectie — fara Discord, fara retea.

    python tests/test_command_gating.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import commands as commands_mod, state as state_mod, views
from music.state import GuildState, begin_loading

GUILD_ID = 11


class _FakeVoiceClient:
    channel = None

    def __init__(self, playing=False, paused=False):
        self.playing = playing
        self.paused = paused

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused


class _FakeCtx:
    def __init__(self, vc):
        self.guild = type('G', (), {'id': GUILD_ID})()
        self.voice_client = vc
        self.author = type('A', (), {'voice': type('V', (), {'channel': None})()})()
        self.message = None
        self.bot = type('B', (), {'loop': None})()
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append(a[0] if a else k)
        return None


class _FakeBot:
    """Colecteaza comenzile inregistrate de setup_music_commands."""

    loop = None

    def __init__(self):
        self.registry = {}

    def command(self, *args, **kwargs):
        def deco(fn):
            self.registry[kwargs.get('name', fn.__name__)] = fn
            return fn
        return deco


class _Wiring:
    """setup_music_commands cu toate dependentele inlocuite."""

    def __init__(self):
        self.plays = []
        self.ui_calls = 0
        self.bot = _FakeBot()

    def __enter__(self):
        async def process_play(ctx, query, is_radio=False):
            self.plays.append(query)

        async def update_player_ui(ctx, send_new=False):
            self.ui_calls += 1

        self.saved_delete = commands_mod.safe_delete

        async def no_delete(_msg):
            return None

        commands_mod.safe_delete = no_delete
        commands_mod.setup_music_commands(
            self.bot, process_play, lambda *a, **k: None, update_player_ui,
            lambda *a, **k: None, lambda *a, **k: None)
        return self

    def __exit__(self, *exc):
        commands_mod.safe_delete = self.saved_delete
        return False


def _fresh_state():
    st = GuildState()
    state_mod.guild_states[GUILD_ID] = st
    return st


def test_nplay_while_loading_queues_instead_of_resolving_again():
    st = _fresh_state()
    begin_loading(st)                      # o rezolvare e deja in curs
    ctx = _FakeCtx(_FakeVoiceClient())
    with _Wiring() as w:
        asyncio.run(w.bot.registry['nplay'](ctx, search='alta melodie'))

    assert w.plays == [], 'a pornit o a doua rezolvare peste una in curs'
    assert st.queue and st.queue[0]['query'] == 'alta melodie', st.queue
    assert ctx.sent, 'utilizatorul nu a fost anuntat'
    assert st.skip_request is False, 'a marcat un skip pentru o piesa neincarcata'


def test_nplay_when_idle_still_plays_immediately():
    """Gardul nu are voie sa strice comportamentul normal al comenzii."""
    st = _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    with _Wiring() as w:
        asyncio.run(w.bot.registry['nplay'](ctx, search='melodia mea'))

    assert w.plays == ['melodia mea'], w.plays
    assert st.skip_request is True, 'nplay trebuie sa inlocuiasca piesa curenta'
    assert st.queue == []


def test_play_while_loading_queues_even_when_nothing_is_playing():
    """Exact fereastra deschisa de ramura de playlist si de butonul Autoplay."""
    st = _fresh_state()
    begin_loading(st)
    ctx = _FakeCtx(_FakeVoiceClient(playing=False))
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='inca una'))

    assert w.plays == [], 'a pornit o rezolvare in paralel cu una in curs'
    assert st.queue and st.queue[-1]['query'] == 'inca una'


def test_the_playlist_branch_marks_itself_busy_before_extracting():
    """Extractia unui playlist de 30 de intrari trebuie sa fie vizibila ca ocupat."""
    st = _fresh_state()
    seen = []

    async def fake_extract(opts, query, download=False, loop=None, stage=''):
        seen.append(st.is_loading)
        return {'entries': [
            {'id': 'a', 'title': 'A', 'url': 'https://www.youtube.com/watch?v=a'},
            {'id': 'b', 'title': 'B', 'url': 'https://www.youtube.com/watch?v=b'},
        ]}

    saved = commands_mod.ytdlp.extract
    commands_mod.ytdlp.extract = fake_extract
    try:
        ctx = _FakeCtx(_FakeVoiceClient())
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](
                ctx, search='https://www.youtube.com/watch?v=a&list=PL123'))
    finally:
        commands_mod.ytdlp.extract = saved

    assert seen == [True], f'extractia a rulat cu is_loading={seen}'
    assert st.is_loading is False, 'steagul a rămas aprins dupa playlist'


def test_the_autoplay_button_does_not_prefill_while_loading():
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    begin_loading(st)
    prefills = []

    async def fake_prefill(state, loop=None):
        prefills.append(True)

    saved = views.prefill_autoplay_queue
    views.prefill_autoplay_queue = fake_prefill
    saved_ui = None
    try:
        import music.ui as ui_mod
        saved_ui = ui_mod.update_player_ui

        async def no_ui(ctx, send_new=False):
            return None

        ui_mod.update_player_ui = no_ui

        from music import player
        saved_player = (player.cancel_timeout, player.start_timeout)
        player.cancel_timeout = lambda *a, **k: None
        player.start_timeout = lambda *a, **k: None

        class _Inter:
            class response:
                @staticmethod
                def is_done():
                    return False

                @staticmethod
                async def defer():
                    return None

        view = views.MusicControlView(_FakeCtx(_FakeVoiceClient()))
        asyncio.run(view.autoplay_btn.callback(_Inter()))
        player.cancel_timeout, player.start_timeout = saved_player
    finally:
        views.prefill_autoplay_queue = saved
        if saved_ui is not None:
            import music.ui as ui_mod
            ui_mod.update_player_ui = saved_ui

    assert prefills == [], 'a cerut un Mix de 50 de intrari in timpul unei incarcari'
    assert st.autoplay is True, 'butonul trebuie totusi sa comute steagul'


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
