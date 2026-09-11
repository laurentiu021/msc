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
from music.state import GuildState, begin_loading, end_loading

GUILD_ID = 11


class _FakeVoiceClient:
    def __init__(self, playing=False, paused=False, *, connected=True,
                 channel=None, move_lands=True):
        self.playing = playing
        self.paused = paused
        self.disconnects = []
        # `is_connected()` e singurul adevar despre un client de voce. discord.py
        # inregistreaza `guild.voice_client` INAINTE de handshake si il lasa pus pe
        # toata reconectarea interna, deci un fals care raspunde mereu True ascunde
        # exact clasa de bug-uri in care botul iese mut.
        self.connected = connected
        self.channel = channel
        self.moves = []
        self._move_lands = move_lands

    async def disconnect(self, *, force=False):
        self.disconnects.append(force)
        self.connected = False

    def stop(self):
        self.playing = False
        self.paused = False

    def play(self, source, after=None):
        self.playing = True

    async def move_to(self, channel):
        self.moves.append(channel)
        if self._move_lands:
            self.channel = channel

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused


class _FakeVoiceChannel:
    """Canalul de voce al autorului, cu permisiuni si conectare controlate."""

    def __init__(self, *outcomes, connect_perm=True, speak_perm=True):
        self.name = 'General'
        self.bitrate = 64000
        # Membrii conteaza: un canal in care mai e cineva nu poate fi parasit.
        self.members = []
        # Cate secunde a cerut fiecare incercare de conectare.
        self.connect_calls = []
        self._outcomes = list(outcomes)
        self._perms = type('P', (), {'connect': connect_perm,
                                     'speak': speak_perm})()

    def permissions_for(self, member):
        return self._perms

    async def connect(self, *, timeout=None, **kwargs):
        self.connect_calls.append(timeout)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeCtx:
    def __init__(self, vc, interaction=None, timeline=None, channel=None):
        self.guild = type('G', (), {'id': GUILD_ID})()
        # Botul ca membru (pentru permisiuni) si clientul de voce al guild-ului:
        # `_drop_voice` curata prin el inaintea unei reincercari.
        self.guild.me = object()
        self.guild.voice_client = vc
        self.voice_client = vc
        self.author = type('A', (), {'voice': type('V', (), {'channel': channel})()})()
        self.message = None
        self.bot = type('B', (), {'loop': None})()
        self.sent = []
        # None = a venit prin `!play`. Un obiect = prin `/play`, si atunci comanda
        # trebuie sa raspunda in 3 secunde, altfel Discord o declara eșuata.
        self.interaction = interaction
        self.deferred = 0
        # Jurnal ORDONAT, partajat cu _Wiring: doar el arata dacă raspunsul a plecat
        # inainte de munca, nu doar dacă a plecat vreodata.
        self.timeline = [] if timeline is None else timeline

    async def defer(self, *a, **k):
        self.deferred += 1
        self.timeline.append(('defer', None))

    async def send(self, *a, **k):
        msg = a[0] if a else k
        self.sent.append(msg)
        self.timeline.append(('send', msg))
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

    def hybrid_command(self, *args, **kwargs):
        """`!play` si `/play` din aceeasi implementare.

        Aici conteaza doar ca inregistrarea se face la fel: testele conduc callback-ul
        direct, iar decoratorii de app_commands (describe/autocomplete) doar atașeaza
        atribute pe functie, deci nu au nevoie de un tree real.
        """
        return self.command(*args, **kwargs)


class _Wiring:
    """setup_music_commands cu toate dependentele inlocuite."""

    def __init__(self):
        self.plays = []
        self.ui_calls = 0
        self.next_calls = []
        self.starts = []
        self.cancels = []
        self.bot = _FakeBot()
        self.timeline = []

    def __enter__(self):
        async def process_play(ctx, query, is_radio=False):
            self.plays.append(query)
            self.timeline.append(('resolve', query))

        async def update_player_ui(ctx, send_new=False):
            self.ui_calls += 1

        self.saved_delete = commands_mod.safe_delete

        async def no_delete(_msg):
            return None

        commands_mod.safe_delete = no_delete
        commands_mod.setup_music_commands(
            self.bot, process_play,
            lambda *a, **k: self.next_calls.append(a), update_player_ui,
            lambda *a, **k: self.starts.append(a),
            lambda *a, **k: self.cancels.append(a))
        # `resume_if_idle` trece prin globalele modulului player, nu prin
        # dependentele injectate in comenzi, deci le inlocuim si pe ele.
        from music import player
        self._saved_player = (player.play_next, player.start_timeout,
                              player.cancel_timeout)
        player.play_next = lambda ctx: self.next_calls.append(('play_next',))
        player.start_timeout = lambda ctx: self.starts.append(('start',))
        player.cancel_timeout = lambda ctx: self.cancels.append(('cancel',))
        return self

    def __exit__(self, *exc):
        commands_mod.safe_delete = self.saved_delete
        from music import player
        (player.play_next, player.start_timeout,
         player.cancel_timeout) = self._saved_player
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


def test_247_on_actually_starts_playing():
    """Ramura ON anula timer-ul si nu pornea nimic in loc.

    Rezultatul: always_on=True, autoplay=True, coada plina, niciun timer — si
    nimic in tot procesul care sa mai poata porni radioul. `finally`-ul tick-ului
    e singurul care re-armeaza, si el are nevoie de un tick care exista deja.
    """
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    st.queue = [{'query': 'a', 'title': 'A'}]
    ctx = _FakeCtx(_FakeVoiceClient(playing=False))
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))

    assert st.always_on is True, 'nu a pornit 24/7'
    assert w.next_calls or w.starts, (
        '24/7 ON a lasat botul fara redare si fara timer: nimic nu mai poate '
        'porni radioul')


def test_247_on_does_not_prefill_over_a_load_in_flight():
    """Aceeasi regula ca la butonul Autoplay: instanta era reparata, clasa nu."""
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    begin_loading(st)
    prefills = []

    async def fake_prefill(state, loop=None):
        prefills.append(True)

    saved = commands_mod.prefill_autoplay_queue
    commands_mod.prefill_autoplay_queue = fake_prefill
    try:
        ctx = _FakeCtx(_FakeVoiceClient(playing=False))
        with _Wiring() as w:
            asyncio.run(w.bot.registry['247'](ctx))
    finally:
        commands_mod.prefill_autoplay_queue = saved

    assert prefills == [], (
        'a cerut un Mix de pana la 50 de intrari peste o incarcare in curs')


def test_247_on_while_something_plays_does_not_restart_playback():
    """Poarta nu are voie sa taie piesa curenta."""
    st = _fresh_state()
    st.queue = [{'query': 'a', 'title': 'A'}]
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))

    assert w.next_calls == [], 'a pornit o redare peste piesa care cânta'
    assert w.plays == [], w.plays


def test_the_playlist_branch_does_not_steal_a_loading_flag_it_did_not_set():
    """`begin_loading` incrementeaza token-ul, deci `loading()` fura proprietatea.

    Cu un `!play` deja in curs, blocul `with loading(state)` din ramura de
    playlist devenea proprietarul steagul, iar la ieșire il STINGEA — desi prima
    incarcare rula inca. Verificarea de mai jos vedea atunci "liber" si pornea un
    al doilea `process_play` in paralel.
    """
    st = _fresh_state()
    begin_loading(st)                       # un !play e deja in curs

    async def fake_extract(opts, query, download=False, loop=None, stage=''):
        return {'entries': [
            {'id': 'a', 'title': 'A', 'url': 'https://www.youtube.com/watch?v=a'},
            {'id': 'b', 'title': 'B', 'url': 'https://www.youtube.com/watch?v=b'},
        ]}

    saved = commands_mod.ytdlp.extract
    commands_mod.ytdlp.extract = fake_extract
    try:
        ctx = _FakeCtx(_FakeVoiceClient(playing=False))
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](
                ctx, search='https://www.youtube.com/watch?v=a&list=PL123'))
    finally:
        commands_mod.ytdlp.extract = saved

    assert st.is_loading is True, (
        'ramura de playlist a stins steagul incarcarii care rula deja')
    assert w.plays == [], (
        f'a pornit un al doilea process_play peste unul in curs: {w.plays}')
    assert len(st.queue) == 2, f'playlist-ul nu a intrat intreg in coada: {st.queue}'


# --- /play, aceeasi implementare ca !play ------------------------------------

def test_play_is_registered_as_a_hybrid_command():
    """O singura implementare, doua interfete.

    Daca `/play` ar fi o comanda separata, ar trebui intreținuta in paralel cu
    `!play` — iar tot ce s-a reparat azi in calea de redare (garda de playlist,
    frana de refuzuri, promovarea de cookies) ar trebui reparat de doua ori.
    """
    import inspect

    import bot as bot_mod
    from discord.ext import commands as dpy_commands

    cmd = bot_mod.bot.get_command('play')
    assert cmd is not None, 'comanda !play a dispărut'
    assert isinstance(cmd, dpy_commands.HybridCommand), type(cmd)
    assert cmd.app_command is not None, (
        'comanda hibrida nu a produs nicio comanda slash')
    params = inspect.signature(cmd.callback).parameters
    assert 'search' in params, params


def test_a_slash_invocation_is_answered_before_any_work():
    """Discord declara eșuata o comanda neconfirmata in 3 secunde.

    Un mesaj REAL, nu `defer()`: cu defer, interactiunea intra in "Gogu is
    thinking..." si rămâne acolo pana la un followup — iar exista cai care nu
    trimit niciun mesaj (o eroare deduplicata, o redare intrerupta), deci
    "thinking" rămânea pe ecran pentru totdeauna. S-a intamplat in producție.
    """
    st = _fresh_state()
    st.queue = []
    with _Wiring() as w:
        # Nu `playing=True`: pe calea aceea mesajul "am adaugat in coada" conține
        # oricum titlul, deci un test care cere doar "s-a trimis ceva cu titlul"
        # ar trece si cu confirmarea ștearsa. Calea libera merge direct la rezolvare,
        # asa ca ORDINEA din jurnal e singura care demonstreaza raspunsul imediat.
        ctx = _FakeCtx(_FakeVoiceClient(), interaction=object(),
                       timeline=w.timeline)
        asyncio.run(w.bot.registry['play'](ctx, search='titlul cerut'))
    assert 'resolve' in [ev for ev, _ in w.timeline], (
        f'testul nu mai atinge calea de rezolvare: {w.timeline}')
    assert w.timeline[0][0] == 'send', (
        f'interactiunea nu a fost inchisa inainte de munca: {w.timeline}')
    assert 'titlul cerut' in str(w.timeline[0][1]), w.timeline[0][1]
    assert ctx.deferred == 0, (
        'a folosit defer(): interactiunea rămâne in "thinking" pana la un followup, '
        'iar unele cai nu trimit niciunul')


def test_a_prefix_invocation_does_not_announce_itself():
    """`!play` nu are nicio interactiune de inchis, deci nici mesaj de confirmare."""
    st = _fresh_state()
    st.queue = []
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert ctx.deferred == 0, 'a incercat sa confirme o interactiune inexistenta'
    assert not any('Caut' in str(m) for m in ctx.sent), (
        f'a adaugat un mesaj de confirmare inutil la !play: {ctx.sent}')


def _connect_ctx(channel, stale=None):
    """Un ctx fara client de voce, deci care TREBUIE sa se conecteze."""
    ctx = _FakeCtx(None, channel=channel)
    ctx.guild.voice_client = stale
    return ctx


def _no_retry_delay():
    """Reincercarea nu are voie sa faca testul sa aștepte cu adevarat."""
    saved = commands_mod.VOICE_RETRY_DELAY_SEC
    commands_mod.VOICE_RETRY_DELAY_SEC = 0
    return saved


def test_a_missing_connect_permission_is_answered_instantly():
    """Discord nu raspunde cu eroare la o cerere de voce refuzata: o ignora.

    Deci lipsa permisiunii arata exact ca o pana de server de voce — amandoua ca
    timeout. Verificarea locala le separa si nu mai face pe nimeni sa aștepte.
    """
    _fresh_state()
    channel = _FakeVoiceChannel(connect_perm=False)
    ctx = _connect_ctx(channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert channel.connect_calls == [], (
        'a incercat sa se conecteze fara permisiunea Connect')
    assert any('permisiunea' in str(m) for m in ctx.sent), ctx.sent
    assert w.plays == [], 'a mers mai departe cu rezolvarea desi nu poate intra'


def test_a_missing_speak_permission_is_reported_before_connecting():
    """Altfel intra, tace, si nimic nu apare in loguri."""
    _fresh_state()
    channel = _FakeVoiceChannel(speak_perm=False)
    ctx = _connect_ctx(channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert channel.connect_calls == [], channel.connect_calls
    assert any('vorbesc' in str(m) for m in ctx.sent), ctx.sent
    assert w.plays == []


def test_a_voice_endpoint_that_arrives_late_is_retried():
    """Bug-ul din producție: conectat, apoi deconectat imediat.

    Discord trimite VOICE_SERVER_UPDATE cu `endpoint: null` cand reasigneaza
    serverul de voce, si abia apoi pe cel real. O singura incercare renunța
    exact acolo.
    """
    _fresh_state()
    connected = _FakeVoiceClient()
    channel = _FakeVoiceChannel(asyncio.TimeoutError(), connected)
    stale = _FakeVoiceClient()
    ctx = _connect_ctx(channel, stale=stale)
    saved = _no_retry_delay()
    try:
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    finally:
        commands_mod.VOICE_RETRY_DELAY_SEC = saved
    assert len(channel.connect_calls) == 2, (
        f'nu a reincercat dupa timeout: {channel.connect_calls}')
    assert stale.disconnects == [True], (
        'clientul pe jumatate deschis nu a fost inchis forțat: '
        'a doua incercare ar fi refuzata din start cu ClientException')
    assert w.plays == ['ceva'], (
        f'reconectarea a reusit, dar redarea nu a mai pornit: {w.plays}')


def test_every_attempt_failing_tells_the_user_what_happened():
    _fresh_state()
    channel = _FakeVoiceChannel(asyncio.TimeoutError(), asyncio.TimeoutError(),
                                asyncio.TimeoutError())
    ctx = _connect_ctx(channel)
    saved = _no_retry_delay()
    try:
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    finally:
        commands_mod.VOICE_RETRY_DELAY_SEC = saved
    assert len(channel.connect_calls) == commands_mod.VOICE_CONNECT_ATTEMPTS, (
        channel.connect_calls)
    assert any('voce' in str(m) for m in ctx.sent), ctx.sent
    assert w.plays == [], 'a rezolvat o piesa fara sa fie in voce'


def test_the_connect_budget_is_worth_waiting_for():
    """Plafoanele sunt datele fixului, deci nu au voie sa se intoarca in liniște."""
    assert commands_mod.VOICE_CONNECT_ATTEMPTS >= 2, (
        'o singura incercare = bug-ul din producție')
    assert commands_mod.VOICE_CONNECT_TIMEOUT >= 20, (
        f'{commands_mod.VOICE_CONNECT_TIMEOUT}s e prea putin pentru un endpoint '
        f'intarziat; scurtimea de dinainte exista pentru cazul permisiunilor, '
        f'care se rezolva acum local')
    _fresh_state()
    channel = _FakeVoiceChannel(_FakeVoiceClient())
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](_connect_ctx(channel), search='ceva'))
    assert channel.connect_calls == [commands_mod.VOICE_CONNECT_TIMEOUT], (
        f'conectarea nu foloseste plafonul declarat: {channel.connect_calls}')


def test_247_actually_joins_the_voice_channel():
    """Comanda nu se conecta NICIODATA.

    Aprindea autoplay, aducea coada, si `resume_if_idle` raspundea "deconectat" —
    deci botul rămânea in afara canalului, cu 24/7 pornit si coada plina, si nimic
    nu il mai aducea inauntru. Raportat din producție.
    """
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    connected = _FakeVoiceClient()
    channel = _FakeVoiceChannel(connected)
    ctx = _connect_ctx(channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))
    assert channel.connect_calls, '24/7 nu a incercat sa intre in voce'
    assert st.always_on is True, st.always_on
    assert st.autoplay is True, '24/7 nu a aprins autoplay'


def test_247_that_cannot_join_does_not_claim_to_be_on():
    """Un 24/7 "pornit" fara voce e exact starea rupta, doar cu un mesaj fals peste."""
    st = _fresh_state()
    channel = _FakeVoiceChannel(asyncio.TimeoutError(), asyncio.TimeoutError())
    ctx = _connect_ctx(channel)
    saved = _no_retry_delay()
    try:
        with _Wiring() as w:
            asyncio.run(w.bot.registry['247'](ctx))
    finally:
        commands_mod.VOICE_RETRY_DELAY_SEC = saved
    assert st.always_on is False, (
        '24/7 a rămas pornit desi botul nu a putut intra in voce')


def test_247_without_the_author_in_voice_says_so():
    """Nu exista niciun canal in care sa stea, deci steagul nu are voie sa se aprinda."""
    st = _fresh_state()
    ctx = _connect_ctx(None)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))
    assert st.always_on is False, st.always_on
    assert any('voce' in str(m) for m in ctx.sent), ctx.sent


def test_247_already_in_voice_does_not_reconnect():
    """Botul e deja in canal (sesiunea altcuiva): o reconectare ar rupe redarea."""
    st = _fresh_state()
    st.last_url = 'https://www.youtube.com/watch?v=x'
    channel = _FakeVoiceChannel()
    ctx = _FakeCtx(_FakeVoiceClient(playing=True, channel=channel),
                   channel=channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))
    assert channel.connect_calls == [], 's-a reconectat peste o sesiune activa'
    assert st.always_on is True


def test_a_registered_but_unconnected_client_is_not_treated_as_ready():
    """`guild.voice_client` nenul nu inseamna "conectat".

    discord.py il inregistreaza INAINTE de handshake si il lasa pus pe toata
    reconectarea interna. Cu vechea verificare pe adevar-simplu, `!play` ajungea
    la `process_play`, care vede `not vc.is_connected()`, stinge steagul si iese
    fara NICIUN mesaj si fara nicio linie de log — dupa ce comanda Ștersese deja
    mesajul utilizatorului. Comanda dispărea pur si simplu.
    """
    st = _fresh_state()
    channel = _FakeVoiceChannel()
    half_open = _FakeVoiceClient(connected=False, channel=channel)
    ctx = _FakeCtx(half_open, channel=channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert w.plays == [], 'a pornit o rezolvare cu un client de voce neconectat'
    assert ctx.sent, 'comanda a dispărut fara niciun mesaj'
    assert channel.connect_calls == [], (
        'a chemat connect() peste un slot ocupat: ar da ClientException')


def test_247_with_a_half_open_client_does_not_claim_to_be_on():
    """Trecea de `if not ctx.voice_client` si anunța "24/7 ON" dupa ce omorâse
    singurul timer — exact starea pe care 24/7 trebuie sa o facă imposibila."""
    st = _fresh_state()
    channel = _FakeVoiceChannel()
    ctx = _FakeCtx(_FakeVoiceClient(connected=False, channel=channel),
                   channel=channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['247'](ctx))
    assert st.always_on is False, '24/7 pornit peste o conexiune inexistenta'
    assert st.autoplay is False, 'a aprins autoplay fara voce'


def test_play_from_another_channel_moves_the_bot_when_the_old_one_is_empty():
    """Altfel piesa cânta unde nu e nimeni, iar cel care a cerut-o nu o poate nici
    opri din panou: `interaction_check` refuza pe oricine nu e in canalul botului."""
    st = _fresh_state()
    old = _FakeVoiceChannel()
    old.name = 'Vechi'
    new = _FakeVoiceChannel()
    new.name = 'Nou'
    vc = _FakeVoiceClient(channel=old)
    ctx = _FakeCtx(vc, channel=new)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert vc.moves == [new], f'nu s-a mutat in canalul autorului: {vc.moves}'
    assert w.plays == ['ceva'], w.plays


def test_play_from_another_channel_does_not_steal_a_session_in_use():
    st = _fresh_state()
    old = _FakeVoiceChannel()
    old.name = 'Vechi'
    old.members = [type('M', (), {'bot': False})()]
    new = _FakeVoiceChannel()
    new.name = 'Nou'
    vc = _FakeVoiceClient(channel=old)
    ctx = _FakeCtx(vc, channel=new)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert vc.moves == [], 'a furat sesiunea unui canal in care se ascultă'
    assert w.plays == [], 'a redat oricum'
    assert any('Vechi' in str(m) for m in ctx.sent), ctx.sent


def test_a_move_that_silently_fails_is_reported():
    """`move_to` inghite propriul TimeoutError si revine la starea veche, deci
    absenta unei excepții nu dovedește nimic — canalul o dovedește."""
    st = _fresh_state()
    old = _FakeVoiceChannel()
    old.name = 'Vechi'
    new = _FakeVoiceChannel()
    new.name = 'Nou'
    vc = _FakeVoiceClient(channel=old, move_lands=False)
    ctx = _FakeCtx(vc, channel=new)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['play'](ctx, search='ceva'))
    assert vc.moves == [new], vc.moves
    assert w.plays == [], 'a redat desi mutarea nu a reusit'
    assert ctx.sent, 'mutarea eșuata a fost tacuta'


def test_stop_from_another_channel_is_refused():
    """Garda exista de mult, dar numai pe butoane: oricine din server, din orice
    canal, putea opri sesiunea altcuiva scriind `!stop` — chiar dacă butonul cu
    exact acelasi efect il refuza."""
    st = _fresh_state()
    st.queue = [{'query': 'x', 'title': 'X'}]
    bot_channel = _FakeVoiceChannel()
    bot_channel.name = 'Unde cânta'
    other = _FakeVoiceChannel()
    other.name = 'Alt canal'
    vc = _FakeVoiceClient(playing=True, channel=bot_channel)
    ctx = _FakeCtx(vc, channel=other)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['stop'](ctx))
    assert st.queue, 'un strain a golit coada sesiunii'
    assert vc.disconnects == [], 'un strain a scos botul din voce'
    assert any('Unde cânta' in str(m) for m in ctx.sent), ctx.sent


def test_stop_from_the_same_channel_still_works():
    st = _fresh_state()
    st.queue = [{'query': 'x', 'title': 'X'}]
    channel = _FakeVoiceChannel()
    vc = _FakeVoiceClient(playing=True, channel=channel)
    ctx = _FakeCtx(vc, channel=channel)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['stop'](ctx))
    assert st.queue == [], 'garda a blocat pe cine avea dreptul'
    assert vc.disconnects, 'nu a ieșit din voce'


def test_commands_are_free_when_there_is_no_session():
    """Fara client de voce nu e nimic de protejat."""
    st = _fresh_state()
    st.queue = [{'query': 'x', 'title': 'X'}]
    ctx = _FakeCtx(None)
    ctx.guild.voice_client = None
    with _Wiring() as w:
        asyncio.run(w.bot.registry['clear'](ctx))
    assert st.queue == [], 'a refuzat o comanda desi nu exista nicio sesiune'


def test_seek_refuses_a_negative_time_instead_of_restarting():
    """Fara marginea de jos, `!seek -30` repornea piesa de la zero si raspundea
    "Seek la `0:00`" — adica facea altceva decat ce a cerut omul, si spunea ca a
    reusit."""
    import tempfile

    st = _fresh_state()
    channel = _FakeVoiceChannel()
    vc = _FakeVoiceClient(playing=True, channel=channel)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'piesa.opus')
        with open(path, 'wb') as fh:
            fh.write(b'audio')
        st.current_file = path
        st.last_duration = 200
        ctx = _FakeCtx(vc, channel=channel)
        # Sursa audio inlocuita si aici: daca garda cade, testul trebuie sa pice pe
        # aserțiunea lui, nu pe absenta FFmpeg-ului din mediu.
        saved_source = commands_mod.make_opus_source

        async def fake_source(filename, channel_, **kwargs):
            return object()

        commands_mod.make_opus_source = fake_source
        try:
            with _Wiring() as w:
                asyncio.run(w.bot.registry['seek'](ctx, timestamp='-30'))
        finally:
            commands_mod.make_opus_source = saved_source

    assert any('negativ' in str(m) for m in ctx.sent), (
        f'nu a refuzat un timp negativ: {ctx.sent}')
    assert not any('Seek la' in str(m) for m in ctx.sent), (
        f'a raportat un seek care nu s-a intamplat: {ctx.sent}')
    assert st.last_start_time == 0.0, (
        'a mutat momentul de start pentru un timp respins')


def test_seek_on_a_paused_track_is_accepted():
    """discord.py raporteaza `is_playing()==False` cat timp e pauzat."""
    import tempfile

    st = _fresh_state()
    channel = _FakeVoiceChannel()
    vc = _FakeVoiceClient(playing=False, paused=True, channel=channel)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'piesa.opus')
        with open(path, 'wb') as fh:
            fh.write(b'audio')
        st.current_file = path
        st.last_duration = 200
        ctx = _FakeCtx(vc, channel=channel)
        # Sursa audio se inlocuiește: altfel testul porneste FFmpeg-ul real, deci
        # trece sau cade in funcție de ce e instalat pe mașina, nu de cod.
        saved_source = commands_mod.make_opus_source

        async def fake_source(filename, channel_, **kwargs):
            return object()

        commands_mod.make_opus_source = fake_source
        try:
            with _Wiring() as w:
                asyncio.run(w.bot.registry['seek'](ctx, timestamp='0:30'))
        finally:
            commands_mod.make_opus_source = saved_source

    assert not any('Nu se reda nimic' in str(m) for m in ctx.sent), (
        f'a refuzat un seek pe o piesa pusa pe pauza: {ctx.sent}')


def test_the_command_tree_is_no_longer_wiped_before_syncing():
    """Golirea exista cand nu aveam nicio comanda slash.

    Lasata la locul ei, ar sterge exact `/play` inainte de sincronizare si
    autocomplete-ul nu ar apărea niciodata in Discord.
    """
    import ast
    import inspect

    import bot as bot_mod

    src = inspect.getsource(bot_mod.on_ready)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
             if isinstance(n, ast.Call)}
    assert 'bot.tree.sync' in calls, 'nu se mai sincronizeaza arborele'
    assert 'bot.tree.clear_commands' not in calls, (
        'arborele e golit inainte de sync: /play nu ajunge niciodata in Discord')


def test_the_autocomplete_never_touches_the_network():
    """Sugestiile vin din cache-ul de pe volum. Discord da 3 secunde."""
    import ast
    import inspect

    from music import commands as commands_mod

    src = inspect.getsource(commands_mod.autocomplete_choices)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    for forbidden in ('ytdlp.extract', 'yt_api.search', 'yt_api.search_music',
                      'urllib.request.urlopen'):
        assert forbidden not in called, (
            f'autocomplete-ul face o cerere de rețea ({forbidden}) la fiecare tasta')
    assert 'suggest_tracks' in called, called

    # Si adaptorul chiar duce la ea: altfel `/play` ar arata fara nicio sugestie.
    closure = inspect.getsource(commands_mod.setup_music_commands)
    adapter = next(n for n in ast.walk(ast.parse(closure.strip()))
                   if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == '_suggest_played')
    assert 'autocomplete_choices' in {
        ast.unparse(n.func) for n in ast.walk(adapter) if isinstance(n, ast.Call)}


def test_play_next_owns_the_loading_flag_it_sets():
    """`state.is_loading = True` direct ocolea `begin_loading`.

    Fara incrementarea token-ului, un `process_play` care se termina imediat dupa
    isi chema `end_loading` cu token-ul lui — care inca se potrivea — si stingea
    steagul pus de `play_next`. Comanda urmatoare vedea "liber" si pornea o a doua
    rezolvare in paralel. Aceeasi clasa cu bug-ul din ramura de playlist.
    """
    from music import player
    from music.state import end_loading

    st = _fresh_state()
    # O incarcare mai veche a luat si a eliberat steagul: `load_token` a rămas la
    # valoarea ei, deci un `end_loading` intarziat al ei poate inca sa se
    # potriveasca. Steagul trebuie sa fie LIBER cand intra `play_next`, altfel
    # garda contra rezolvarilor paralele il opreste din start — corect, dar atunci
    # testul nu ar mai spune nimic despre proprietatea token-ului.
    vechi = begin_loading(st)
    end_loading(st, vechi)
    ctx = _FakeCtx(_FakeVoiceClient(playing=False))

    saved = player._loop
    scheduled = []
    player._loop = type('L', (), {})()     # nu None, ca sa nu caute o bucla
    saved_sched = player.asyncio.run_coroutine_threadsafe
    player.asyncio.run_coroutine_threadsafe = (
        lambda coro, loop: scheduled.append(coro) or coro.close())
    try:
        player.play_next(ctx)
        assert st.is_loading is True, 'nu a marcat ocupat'
        # Incarcarea VECHE se termina si isi elibereaza token-ul.
        end_loading(st, vechi)
        assert st.is_loading is True, (
            'un end_loading vechi a stins steagul pus de play_next: comanda '
            'urmatoare va porni o a doua rezolvare in paralel')
    finally:
        player.asyncio.run_coroutine_threadsafe = saved_sched
        player._loop = saved
    assert scheduled, 'nu a programat nimic'


def test_play_next_refuses_to_start_a_second_resolve():
    """Un `!skip` sau sfarșitul unei piese in timpul unui `!nplay` pornea a doua
    rezolvare in paralel: consuma capul cozii, tăia piesa din aer si scria in
    history o piesa pe care nimeni nu a ascultat-o.

    Garda trebuie sa fie INAINTE de `begin_loading` — acela aprinde steagul, deci
    aceeasi verificare pusa mai jos ar vedea mereu True. Si nu are voie sa fure
    token-ul: altfel `finally`-ul incarcarii vii nu ar mai stinge steagul niciodata.
    """
    from music import player

    st = _fresh_state()
    st.queue = [{'query': 'https://y/1', 'title': 'A'}]
    in_flight = begin_loading(st)          # un `!nplay` rezolva chiar acum
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))

    saved = player._loop
    scheduled = []
    player._loop = type('L', (), {})()
    saved_sched = player.asyncio.run_coroutine_threadsafe
    player.asyncio.run_coroutine_threadsafe = (
        lambda coro, loop: scheduled.append(coro) or coro.close())
    try:
        player.play_next(ctx)
    finally:
        player.asyncio.run_coroutine_threadsafe = saved_sched
        player._loop = saved

    assert scheduled == [], 'a pornit o a doua rezolvare peste una in curs'
    assert st.load_token == in_flight, (
        'a furat token-ul incarcarii in curs: finally-ul ei nu va mai stinge '
        'steagul niciodata')
    assert st.is_loading is True, 'a stins steagul incarcarii in curs'
    assert len(st.queue) == 1, f'a consumat capul cozii: {st.queue}'


def test_play_next_never_leaves_the_flag_on_without_scheduling():
    """Ramura fara bucla ieșea prin `return` cu steagul APRINS si nimic programat.

    Din acel moment fiecare `!play` raspundea "in coada" pentru o incarcare care nu
    exista, si nimic nu mai scurgea coada — sesiune blocata pana la restart.
    """
    from music import player

    for defect in ('fara bucla', 'bucla inchisa'):
        st = _fresh_state()
        ctx = _FakeCtx(_FakeVoiceClient(playing=False))
        saved_loop = player._loop
        saved_get = player.asyncio.get_event_loop
        saved_sched = player.asyncio.run_coroutine_threadsafe
        try:
            if defect == 'fara bucla':
                player._loop = None

                def boom():
                    raise RuntimeError('there is no current event loop')

                player.asyncio.get_event_loop = boom
            else:
                player._loop = type('L', (), {})()

                def boom_sched(coro, loop):
                    coro.close()
                    raise RuntimeError('Event loop is closed')

                player.asyncio.run_coroutine_threadsafe = boom_sched
            player.play_next(ctx)
        finally:
            player._loop = saved_loop
            player.asyncio.get_event_loop = saved_get
            player.asyncio.run_coroutine_threadsafe = saved_sched

        assert st.is_loading is False, (
            f'{defect}: steagul a rămas aprins fara nicio incarcare in curs')


def test_resume_if_idle_never_doubles_a_load_in_flight():
    from music import player

    st = _fresh_state()
    st.queue = [{'query': 'a', 'title': 'A'}]
    begin_loading(st)
    ctx = _FakeCtx(_FakeVoiceClient(playing=False))
    with _Wiring():
        assert player.resume_if_idle(ctx) == 'se incarca altceva'


def test_resume_if_idle_arms_the_timer_when_there_is_nothing_to_play():
    """Altfel botul rămâne conectat, tacut si fara nicio cale de a decide ceva."""
    from music import player

    _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=False))
    with _Wiring() as w:
        assert player.resume_if_idle(ctx) == 'timer armat'
    assert w.starts, 'nu a armat niciun timer'


# --- fiecare comanda trebuie sa raspunda ceva ---------------------------------

def test_stop_answers_even_when_there_is_nothing_to_stop():
    """Gasit in rularea live: `!stop` era singura comanda care NU raspundea nimic.

    Cand chiar era ceva de oprit, plecarea din canal si dispariția panoului tineau
    loc de confirmare. Cand nu era — nimic: niciun mesaj, nicio schimbare vizibila,
    adica exact ce vede cineva cand botul e mort. Toate celelalte comenzi raspund
    si pe cazul gol ("Nu se reda nimic.", "Coada e prea scurta.", "Index invalid").
    """
    _fresh_state()
    ctx = _FakeCtx(None)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['stop'](ctx))
    assert ctx.sent, '!stop nu a raspuns nimic'
    assert 'nimic' in str(ctx.sent[0]).lower(), ctx.sent


def test_stop_confirms_when_it_really_stopped_something():
    st = _fresh_state()
    st.queue.append({'query': 'ceva', 'title': 'ceva'})
    vc = _FakeVoiceClient(playing=True)
    ctx = _FakeCtx(vc)
    with _Wiring() as w:
        asyncio.run(w.bot.registry['stop'](ctx))
    assert ctx.sent and 'oprit' in str(ctx.sent[0]).lower(), ctx.sent
    assert vc.disconnects, 'nu a ieșit din canal'
    assert not st.queue


def test_clear_does_not_claim_to_have_emptied_an_empty_queue():
    """"Coada golita (0 piese)" raporta o acțiune care nu s-a intamplat."""
    _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient())
    with _Wiring() as w:
        asyncio.run(w.bot.registry['clear'](ctx))
    assert ctx.sent, '!clear nu a raspuns nimic'
    said = str(ctx.sent[0]).lower()
    assert 'deja goala' in said, said
    assert '0 piese' not in said, said


def test_clear_reports_the_real_count():
    st = _fresh_state()
    st.queue.extend({'query': f'q{i}'} for i in range(3))
    ctx = _FakeCtx(_FakeVoiceClient())
    with _Wiring() as w:
        asyncio.run(w.bot.registry['clear'](ctx))
    assert '3 piese' in str(ctx.sent[0]), ctx.sent
    assert not st.queue


# --- un playlist adaugat trebuie sa se VADA -----------------------------------

def _fake_playlist(count):
    """Inlocuieste poarta yt-dlp cu un playlist de `count` intrari."""
    from music import ytdlp

    saved = ytdlp.extract

    async def extract(opts, query, download=False, loop=None, stage=''):
        return {'entries': [{'id': f'vid{n}', 'title': f'Piesa {n}'}
                            for n in range(count)]}

    ytdlp.extract = extract
    return ytdlp, saved


def test_a_playlist_added_over_a_playing_track_says_how_many():
    """30 de piese adaugate si absolut nimic vizibil.

    Comanda isi sterge propriul mesaj, iar panoul schimba doar un contor in footer,
    pe un mesaj care poate fi mult mai sus in canal. Pentru O piesa exista deja
    confirmare explicita, exact din motivul asta; ramura de playlist nu o avea.
    """
    st = _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=True), channel=None)
    ytdlp, saved = _fake_playlist(4)
    try:
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](
                ctx, search='https://www.youtube.com/playlist?list=PLxyz'))
    finally:
        ytdlp.extract = saved

    assert len(st.queue) == 4, f'nu a adaugat tot playlist-ul: {len(st.queue)}'
    said = ' '.join(str(m) for m in ctx.sent)
    assert said.strip(), 'nicio confirmare: utilizatorul nu afla ca s-a intamplat ceva'
    assert '4' in said, f'confirmarea nu spune cate piese: {said!r}'


def test_a_playlist_on_an_idle_bot_starts_and_still_reports_the_rest():
    st = _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=False), channel=None)
    ytdlp, saved = _fake_playlist(3)
    try:
        with _Wiring() as w:
            asyncio.run(w.bot.registry['play'](
                ctx, search='https://youtu.be/vid0?list=PLxyz'))
            plays = list(w.plays)
    finally:
        ytdlp.extract = saved

    assert plays and 'vid0' in plays[0], f'nu a pornit prima piesa: {plays}'
    assert len(st.queue) == 2, f'restul playlist-ului nu a intrat in coada: {st.queue}'
    said = ' '.join(str(m) for m in ctx.sent)
    assert '2' in said, f'nu a spus cate au rămas in coada: {said!r}'


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
