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

    def is_connected(self):
        return not self.disconnected

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


def test_stop_button_releases_the_file_without_deleting_it():
    """Butonul curata SESIUNEA; fisierul rămâne intrare de cache.

    Inainte nu facea niciuna din cele doua: stergea un camp mort
    (`state.preloaded`, mereu None) si nu atingea `state.current_file`, deci
    fiecare oprire din panou lasa un fisier pe care nimeni nu-l mai revendica.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'piesa.opus')
        with open(path, 'wb') as fh:
            fh.write(b'audio')
        st = _fresh_state()
        st.current_file = path
        trims = []
        saved = player.trim_cache
        player.trim_cache = lambda: trims.append(True)
        try:
            _press('stop_btn', st)
        finally:
            player.trim_cache = saved
        assert st.current_file is None, 'current_file a rămas setat'
        assert os.path.exists(path), 'a sters o intrare buna de cache'
        assert trims, 'nu s-a chemat evacuarea cache-ului'


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


def test_the_autoplay_button_starts_playing_after_its_prefill():
    """Prefill-ul ia `is_loading` fara sa porneasca nicio redare.

    `is_loading` inseamna pentru toti ceilalti "o incarcare e in curs si va
    scurge coada", deci un `!play` intrat in fereastra de prefill era pus in coada
    si nimic nu il mai scotea: `!play` anulase si timer-ul, iar `after_play` are
    nevoie de o piesa care chiar cânta. Botul rămânea in canal, tacut, cu panoul
    aratand "N in coada".
    """
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    started = []

    async def fake_prefill(state, loop=None):
        # Exact interleaving-ul din raport: un !play aterizeaza in fereastra si
        # se pune in coada crezand ca incarcarea in curs il va porni.
        state.queue.append({'query': 'piesa cerută', 'title': 'X'})

    saved_prefill = views.prefill_autoplay_queue
    saved_next = player.play_next
    views.prefill_autoplay_queue = fake_prefill
    player.play_next = lambda ctx: started.append(True)
    try:
        import music.ui as ui_mod
        saved_ui = ui_mod.update_player_ui

        async def no_ui(ctx, send_new=False):
            return None

        ui_mod.update_player_ui = no_ui
        try:
            _press('autoplay_btn', st)
        finally:
            ui_mod.update_player_ui = saved_ui
    finally:
        views.prefill_autoplay_queue = saved_prefill
        player.play_next = saved_next

    assert st.autoplay is True, 'butonul nu a pornit autoplay'
    assert st.is_loading is False, 'steagul a rămas aprins dupa prefill'
    assert started, (
        'prefill-ul a umplut coada si a eliberat steagul fara sa porneasca '
        'nimic: piesa rămâne in coada pentru totdeauna')


def _defer_failure(exc):
    """Ruleaza `_safe_defer` peste o confirmare care eșueaza. Intoarce logurile."""
    import io
    import logging

    from music.config import log as music_log

    class _Broken:
        data = {'custom_id': 'skip'}

        class response:
            @staticmethod
            async def defer():
                raise exc

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    level = music_log.level
    music_log.addHandler(handler)
    music_log.setLevel(logging.INFO)
    try:
        view = views.MusicControlView(_FakeCtx(_FakeVoiceClient()))
        asyncio.run(view._safe_defer(_Broken()))
    finally:
        music_log.removeHandler(handler)
        music_log.setLevel(level)
    return stream.getvalue()


def test_a_failed_acknowledgement_is_never_silent():
    """Utilizatorul vedea "Gogu didn't respond in time" si logurile erau goale.

    Vechiul `except (HTTPException, DiscordServerError): pass` acoperea o singura
    clasa reala (a doua e subclasa primei) si nu scria nimic. Un transport inchis
    de Discord — `OSError` sau `aiohttp.ClientError`, pe care discord.py nu le
    invelește — scapa cu totul si ajungea in `View.on_error`.
    """
    import aiohttp
    import discord

    for exc in (OSError('conexiune inchisa'),
                aiohttp.ClientError('transport'),
                asyncio.TimeoutError(),
                discord.DiscordServerError.__new__(discord.DiscordServerError)):
        out = _defer_failure(exc)
        assert 'skip' in out, (
            f'{type(exc).__name__} a fost inghitit in silențiu: {out!r}')


def test_a_double_acknowledgement_is_reported_and_contained():
    """`InteractionResponded` e un defect al NOSTRU, dar nu are voie sa propage:
    ar ajunge in `View.on_error` si butonul ar rămâne mort."""
    import discord

    out = _defer_failure(discord.InteractionResponded(_FakeInteraction()))
    assert 'deja confirmata' in out, out


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
