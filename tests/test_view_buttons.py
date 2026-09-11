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


def _press_with_player(name, state, *, playing=False, paused=False,
                       history=None, values=None):
    """Apasa un buton cu `resume_if_idle` si `play_next` observate."""
    import music.player as player_mod

    calls = {'resume': 0, 'stopped': False}

    class _VC:
        channel = None

        def is_connected(self):
            return True

        def is_playing(self):
            return playing

        def is_paused(self):
            return paused

        def stop(self):
            calls['stopped'] = True

        async def disconnect(self, **kw):
            return None

    def fake_resume(ctx):
        calls['resume'] += 1
        return 'pornit'

    saved = (player_mod.resume_if_idle, player_mod.cancel_timeout,
             player_mod.start_timeout)
    player_mod.resume_if_idle = fake_resume
    player_mod.cancel_timeout = lambda *a, **k: None
    player_mod.start_timeout = lambda *a, **k: None
    if history is not None:
        state.history = history
    try:
        ctx = _FakeCtx(_VC())
        view = views.MusicControlView(ctx)
        interaction = _FakeInteraction()
        if values is not None:
            interaction.data = {'values': values}
            asyncio.run(view._jump_callback(interaction))
        else:
            asyncio.run(getattr(view, name).callback(interaction))
        return calls
    finally:
        (player_mod.resume_if_idle, player_mod.cancel_timeout,
         player_mod.start_timeout) = saved


def test_skip_while_nothing_plays_still_advances_the_queue():
    """Butonul doar confirma interactiunea si ieșea: coada rămânea pe loc.

    Nu exista niciun `after_play` cand nimic nu iese pe voce, deci `vc.stop()`
    singur nu putea avansa nimic.
    """
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=a', 'title': 'A'}]
    calls = _press_with_player('skip_btn', st, playing=False, paused=False)
    assert calls['resume'] == 1, 'skip pe idle nu a incercat sa porneasca coada'


def test_skip_while_playing_stops_and_marks_the_skip():
    st = _fresh_state()
    st.queue = [{'query': 'x', 'title': 'X'}]
    calls = _press_with_player('skip_btn', st, playing=True)
    assert calls['stopped'], 'nu a oprit redarea curenta'
    assert st.skip_request is True, (
        'fara skip_request, loop-ul re-adauga exact piesa sarita')
    assert calls['resume'] == 0, 'a pornit coada peste o redare activa'


def test_back_while_paused_actually_goes_back():
    """Singurul buton care verifica doar `is_playing()`: in pauza punea piesa
    anterioara in coada si nu pornea nimic."""
    st = _fresh_state()
    history = [{'url': 'https://y/1', 'title': 'Veche'},
               {'url': 'https://y/2', 'title': 'Curenta'}]
    calls = _press_with_player('back_btn', st, playing=False, paused=True,
                               history=history)
    assert st.queue and st.queue[0]['query'] == 'https://y/1', st.queue
    assert calls['stopped'], 'in pauza nu a oprit nimic, deci nu a avansat'
    assert st.skip_request is True


def test_back_refuses_a_history_entry_without_a_url():
    """Altfel history e golit si in coada intra o cerere goala."""
    st = _fresh_state()
    history = [{'url': '', 'title': 'Fara URL'},
               {'url': 'https://y/2', 'title': 'Curenta'}]
    _press_with_player('back_btn', st, playing=True, history=history)
    assert st.queue == [], f'a pus o cerere goala in coada: {st.queue}'


def test_jumping_keeps_the_whole_queue():
    """`queue[idx:]` arunca tot ce era inainte, deci lista din care alegi se
    micșora la fiecare alegere — exact cand voiai sa alegi."""
    st = _fresh_state()
    st.queue = [{'query': f'q{i}', 'title': f'T{i}'} for i in range(5)]
    calls = _press_with_player(None, st, playing=True, values=['q3'])
    assert [it['query'] for it in st.queue] == ['q3', 'q4', 'q0', 'q1', 'q2'], (
        f'coada nu a fost rotita: {[it["query"] for it in st.queue]}')
    assert calls['stopped'] and st.skip_request is True


def test_jumping_while_idle_starts_playing():
    st = _fresh_state()
    st.queue = [{'query': f'q{i}', 'title': f'T{i}'} for i in range(3)]
    calls = _press_with_player(None, st, playing=False, values=['q2'])
    assert calls['resume'] == 1, 'alegerea din lista nu a pornit nimic'
    assert st.queue[0]['query'] == 'q2', st.queue


def test_the_queue_dropdown_never_offers_duplicate_values():
    """Discord refuza tot componentul cu 400 la valori duplicate, iar edit-ul
    panoului eșua apoi tacut: panoul rămânea inghetat pe piesa veche."""
    st = _fresh_state()
    st.show_queue = True
    st.queue = [{'query': 'acelasi', 'title': 'A'},
                {'query': 'acelasi', 'title': 'A din nou'},
                {'query': '', 'title': 'Fara query'},
                {'query': 'altul', 'title': 'B'}]
    view = views.MusicControlView(_FakeCtx(_FakeVoiceClient()))
    selects = [c for c in view.children if isinstance(c, views.discord.ui.Select)]
    assert len(selects) == 1, selects
    values = [o.value for o in selects[0].options]
    assert values == ['acelasi', 'altul'], values
    assert all(v for v in values), 'o valoare goala e respinsa de Discord'


def test_no_dropdown_at_all_when_every_entry_is_unusable():
    """Un select cu zero opțiuni e si el respins cu 400."""
    st = _fresh_state()
    st.show_queue = True
    st.queue = [{'query': '', 'title': 'X'}, {'title': 'Fara cheie'}]
    view = views.MusicControlView(_FakeCtx(_FakeVoiceClient()))
    assert not [c for c in view.children
                if isinstance(c, views.discord.ui.Select)], (
        'a construit un select fara nicio opțiune valida')


def test_autoplay_on_starts_a_queue_that_is_already_full():
    """`resume_if_idle` era INAUNTRUL blocului de prefill.

    Singurul caz tratat era "coada goala, deci am adus piese". Cu piese deja in
    coada si nimic care cânta, butonul aprindea autoplay si nu pornea nimic — nu
    exista niciun `after_play` care sa scurga coada. `!247` il cheama
    necondiționat, deci butonul se purta altfel decat comanda.
    """
    import music.player as player_mod

    st = _fresh_state()
    st.autoplay = False
    st.queue = [{'query': 'https://y/1', 'title': 'A'}]
    st.last_url = 'https://y/0'
    resumed = []
    prefills = []

    async def fake_prefill(state, loop=None):
        prefills.append(True)

    async def no_ui(ctx, send_new=False):
        return None

    import music.ui as ui_mod
    saved = (player_mod.resume_if_idle, views.prefill_autoplay_queue,
             ui_mod.update_player_ui)
    player_mod.resume_if_idle = lambda ctx: (resumed.append(True), 'pornit')[1]
    views.prefill_autoplay_queue = fake_prefill
    ui_mod.update_player_ui = no_ui
    try:
        _press('autoplay_btn', st)
    finally:
        (player_mod.resume_if_idle, views.prefill_autoplay_queue,
         ui_mod.update_player_ui) = saved

    assert st.autoplay is True
    assert prefills == [], 'a cerut un Mix desi coada avea deja piese'
    assert resumed, (
        'autoplay ON cu coada plina si nimic care cânta nu a pornit nimic')


def test_a_button_that_starts_nothing_still_refreshes_the_panel():
    """Altfel panoul rămâne pe piesa dinainte, cu o coada care nu mai e a lui."""
    import music.player as player_mod
    import music.ui as ui_mod

    st = _fresh_state()
    st.queue = []
    refreshes = []

    async def fake_ui(ctx, send_new=False):
        refreshes.append(send_new)

    saved = (player_mod.resume_if_idle, ui_mod.update_player_ui,
             player_mod.cancel_timeout, player_mod.start_timeout)
    player_mod.resume_if_idle = lambda ctx: 'coada goala'
    ui_mod.update_player_ui = fake_ui
    player_mod.cancel_timeout = lambda *a, **k: None
    player_mod.start_timeout = lambda *a, **k: None
    try:
        view = views.MusicControlView(_FakeCtx(_FakeVoiceClient()))
        asyncio.run(view.skip_btn.callback(_FakeInteraction()))
    finally:
        (player_mod.resume_if_idle, ui_mod.update_player_ui,
         player_mod.cancel_timeout, player_mod.start_timeout) = saved

    assert refreshes, 'panoul a rămas sa arate piesa care nu mai cânta'


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


# --- Loop nu are voie sa anuleze radioul pentru totdeauna ----------------------

def _no_ui():
    """Inlocuieste trimiterea panoului; butoanele o cheama la final."""
    from music import ui as ui_mod

    saved = ui_mod.update_player_ui

    async def stub(ctx, send_new=False):
        return None

    ui_mod.update_player_ui = stub
    return ui_mod, saved


def test_the_loop_button_does_not_veto_the_radio_for_good():
    """Loop cicleaza off -> piesa -> coada -> off.

    Prima apasare oprea radioul cu `by_user=True`, adica scria si veto-ul "omul a
    oprit radioul". Dupa trei apasari, loop-ul e din nou 0 — dar veto-ul rămâne, si
    de atunci tick-ul de 24/7 nu mai reumple niciodata coada: botul sta in canal,
    tacut, toata viata procesului, desi nimeni nu ceruse oprirea radioului.
    """
    from music.idle import RADIO, decide_idle_action

    st = _fresh_state()
    st.always_on = True
    st.autoplay = True
    st.last_url = 'https://www.youtube.com/watch?v=x'
    ui_mod, saved = _no_ui()
    try:
        for _ in range(3):
            _press('loop_btn', st)
    finally:
        ui_mod.update_player_ui = saved

    assert st.loop_mode == 0, f'Loop nu a revenit pe off: {st.loop_mode}'
    decision = decide_idle_action(st, connected=True, playing=False, paused=False,
                                  now=1000.0)
    assert decision.action == RADIO, (
        f'24/7 nu mai reporneste radioul: {decision.reason}')


def test_the_loop_button_still_turns_the_radio_off_while_looping():
    """Comportamentul dorit rămâne: cat timp se repeta, radioul tace."""
    st = _fresh_state()
    st.autoplay = True
    ui_mod, saved = _no_ui()
    try:
        _press('loop_btn', st)
    finally:
        ui_mod.update_player_ui = saved
    assert st.loop_mode == 1, st.loop_mode
    assert st.autoplay is False, 'radioul a rămas pornit peste loop'


def test_the_autoplay_button_still_vetoes_the_radio():
    """Veto-ul nu se pierde: pe butonul care CHIAR e despre radio, el rămâne."""
    from music.idle import NOTHING, decide_idle_action

    st = _fresh_state()
    st.always_on = True
    st.autoplay = True
    st.last_url = 'https://www.youtube.com/watch?v=x'
    ui_mod, saved = _no_ui()
    try:
        _press('autoplay_btn', st)
    finally:
        ui_mod.update_player_ui = saved

    assert st.autoplay is False, 'butonul nu a stins radioul'
    decision = decide_idle_action(st, connected=True, playing=False, paused=False,
                                  now=1000.0)
    assert decision.action == NOTHING, decision
    assert 'utilizator' in decision.reason, decision.reason


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
