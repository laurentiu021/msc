"""Pornirea procesului si evenimentele de voce — partea care decide daca botul
mai exista dupa cinci minute.

Trei defecte de aici nu se vad niciodata la testare manuala, fiindca toate cer o
eroare de rețea sau o cursa de 20 de secunde:

1. Bataia pornea la FINALUL lui on_ready. Orice excepție mai sus (un
   ClientConnectorError de la `tree.sync`, care nu e discord.HTTPException) o
   omitea complet, iar discord.py nu redifuzeaza niciodata on_ready. Watchdog-ul
   nu poate deosebi "n-a batut niciodata" de "bucla e blocata", deci omora un bot
   perfect functional la ~5 minute dupa pornire, la infinit.
2. Fiecare plecare din voce era inregistrata drept decizie a utilizatorului, deci
   o deconectare provocata de Discord otravea 24/7 pe viata procesului.
3. Rabdarea de 20 de secunde pe un canal gol re-verifica totul in afara de
   `always_on`, adica deconecta exact sesiunea 24/7 pornita in acele secunde.

    python tests/test_lifecycle_events.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DISCORD_TOKEN', 'test-token-nefolosit')

import bot as bot_mod
from music import config, state as state_mod
from music.state import GuildState

GUILD_ID = 42


class _FakeVoiceClient:
    def __init__(self, channel=None, playing=False, paused=False):
        self.channel = channel
        self.playing = playing
        self.paused = paused
        self.disconnected = False

    def is_connected(self):
        return not self.disconnected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    async def disconnect(self, **kwargs):
        self.disconnected = True


class _FakeChannel:
    def __init__(self, members):
        self.members = list(members)


class _FakeGuild:
    def __init__(self, vc):
        self.id = GUILD_ID
        self.voice_client = vc


class _FakeMember:
    """Un membru fals care poate pretinde ca E botul.

    `bot.user` e o proprietate read-only in discord.py, deci nu se poate injecta;
    handler-ul intreaba `member == bot.user`, si raspundem noi la intrebare.
    """

    def __init__(self, guild, *, is_bot=False, is_me=False):
        self.guild = guild
        self.bot = is_bot or is_me
        self.is_me = is_me

    def __eq__(self, other):
        if isinstance(other, _FakeMember):
            return self is other
        return self.is_me and other is bot_mod.bot.user

    def __hash__(self):
        return id(self)


class _VoiceState:
    def __init__(self, channel=None, mute=False, self_mute=False):
        self.channel = channel
        self.mute = mute
        self.self_mute = self_mute


def _fresh_state(**fields):
    st = GuildState()
    for key, value in fields.items():
        setattr(st, key, value)
    state_mod.guild_states[GUILD_ID] = st
    return st


class _Seams:
    """Inlocuieste ce ar atinge Discord sau discul."""

    def __enter__(self):
        self.cancels = []
        self._saved = (bot_mod.cancel_timeout, bot_mod.player.trim_cache)
        bot_mod.cancel_timeout = lambda *a, **k: self.cancels.append(a)
        bot_mod.player.trim_cache = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        bot_mod.cancel_timeout, bot_mod.player.trim_cache = self._saved
        return False


# --- 1. bataia porneste inainte de orice cerere de rețea ---------------------

def test_the_heartbeat_starts_before_ready_not_after_it():
    """setup_hook e aȘteptat din login(), deci inainte de connect() si de READY.

    Asa bataia exista si cat timp gateway-ul reincearca sa se conecteze — exact
    scenariul in care varianta veche ieșea cu os._exit(1) la fiecare ~5 minute
    pentru o pana care nu era a noastra si pe care o repornire nu o repara.
    """
    started = []
    saved_beat = bot_mod._heartbeat
    saved_task = bot_mod._heartbeat_task

    async def fake_beat():
        started.append(True)
        await asyncio.sleep(3600)

    bot_mod._heartbeat = fake_beat
    bot_mod._heartbeat_task = None
    try:
        async def main():
            await bot_mod.bot.setup_hook()
            await asyncio.sleep(0)          # lasa task-ul sa intre in functie
            task = bot_mod._heartbeat_task
            assert task is not None and not task.done(), 'bataia nu a pornit'
            task.cancel()

        asyncio.run(main())
    finally:
        bot_mod._heartbeat = saved_beat
        bot_mod._heartbeat_task = saved_task

    assert started, 'setup_hook nu a pornit heartbeat-ul'


def test_a_failing_network_call_in_on_ready_cannot_stop_the_heartbeat():
    """`tree.sync` da ClientConnectorError, care NU e discord.HTTPException.

    Excepția scapa din handler, discord.py o inghite, si tot ce urma in on_ready
    nu mai rula niciodata in sesiunea aceea.
    """
    started = []
    saved = (bot_mod._heartbeat, bot_mod._heartbeat_task,
             bot_mod.bot._BotBase__tree, bot_mod.bot._connection.user,
             bot_mod._tree_synced)

    async def fake_beat():
        started.append(True)
        await asyncio.sleep(3600)

    class _BrokenTree:
        def clear_commands(self, guild=None):
            return None

        async def sync(self, guild=None):
            raise OSError(111, 'Connection refused')

    class _FakeUser:
        id = 1
        name = 'gogu'

        def __str__(self):
            return 'gogu'

    bot_mod._heartbeat = fake_beat
    bot_mod._heartbeat_task = None
    bot_mod._tree_synced = False
    bot_mod.bot._BotBase__tree = _BrokenTree()
    bot_mod.bot._connection.user = _FakeUser()
    presence_calls = []

    async def fake_presence(**kwargs):
        presence_calls.append(kwargs)

    saved_presence = bot_mod.bot.change_presence
    bot_mod.bot.change_presence = fake_presence
    try:
        async def main():
            await bot_mod.bot.on_ready()
            await asyncio.sleep(0)
            task = bot_mod._heartbeat_task
            assert task is not None and not task.done(), (
                'o cadere de rețea in on_ready a omorat bataia')
            task.cancel()

        asyncio.run(main())
    finally:
        bot_mod.bot.change_presence = saved_presence
        (bot_mod._heartbeat, bot_mod._heartbeat_task,
         bot_mod.bot._BotBase__tree, bot_mod.bot._connection.user,
         bot_mod._tree_synced) = saved

    assert started, 'bataia nu a pornit deloc'
    assert presence_calls, (
        'on_ready s-a oprit la eroarea de sync: restul nu a mai rulat')


def test_the_watchdog_would_have_killed_a_bot_without_a_heartbeat():
    """De ce conteaza: dovada consecinței, nu doar a cauzei."""
    boot = 10_000.0
    # `_LAST_BEAT` semanat la import = momentul pornirii. Fara nicio bataie,
    # diferenta creste la infinit si arata identic cu o bucla blocata.
    reason = bot_mod._should_restart(boot + bot_mod.WATCHDOG_STALL_SEC + 16,
                                     boot, 0, 2, boot)
    assert reason and 'heartbeat' in reason, reason


# --- 2. o deconectare nu e o preferinta a utilizatorului ---------------------

def _leave_voice(state):
    channel = _FakeChannel([])
    vc = _FakeVoiceClient(channel=channel)
    guild = _FakeGuild(vc)
    me = _FakeMember(guild, is_me=True)
    with _Seams():
        asyncio.run(bot_mod.bot.on_voice_state_update(
            me, _VoiceState(channel=channel), _VoiceState(channel=None)))
    return state


def test_being_disconnected_is_not_recorded_as_a_user_decision():
    """state.py:autoplay_user_off e singurul steag de acest fel din proiect.

    Cu by_user=True aici, orice deconectare provocata de Discord ("We were
    externally disconnected from voice", close 4014/4022/4021, canal sters) il
    aprindea, si de atunci fiecare tick raspundea "radio oprit de utilizator" —
    pe viata procesului, invinuind un utilizator care nu facuse nimic.
    """
    st = _fresh_state(always_on=True, autoplay=True, autoplay_user_off=False)
    _leave_voice(st)
    assert st.autoplay is False, 'autoplay trebuie oprit la plecarea din voce'
    assert st.autoplay_user_off is False, (
        'o deconectare automata a fost inregistrata ca decizie a utilizatorului: '
        '24/7 nu va mai reporni niciodata radioul')


def test_leaving_voice_also_clears_always_on():
    """Altfel rămâne o sesiune "stai conectat" pe o stare deconectata."""
    st = _fresh_state(always_on=True, loop_mode=2, is_loading=True,
                      queue=[{'query': 'x', 'title': 'X'}])
    _leave_voice(st)
    assert st.always_on is False, 'always_on a supravietuit deconectarii'
    assert st.queue == [] and st.loop_mode == 0 and st.is_loading is False


def test_the_radio_can_still_resume_after_an_automatic_disconnect():
    """Consecinta care conteaza, verificata prin politica reala de inactivitate."""
    from music.idle import RADIO, decide_idle_action

    st = _fresh_state(always_on=True, autoplay=True)
    _leave_voice(st)
    # Utilizatorul revine cu !play si porneste iar 24/7.
    st.always_on = True
    st.autoplay = True
    st.last_url = 'https://www.youtube.com/watch?v=x'
    decision = decide_idle_action(st, connected=True, playing=False,
                                 paused=False, now=1_000_000.0)
    assert decision.action == RADIO, (
        f'radioul rămâne blocat dupa o deconectare automata: {decision.reason}')


# --- 3. rabdarea pe canal gol re-verifica 24/7 -------------------------------

def _empty_channel_tick(state, *, turn_on_247_during_grace):
    channel = _FakeChannel([])
    vc = _FakeVoiceClient(channel=channel)
    guild = _FakeGuild(vc)
    bot_user = _FakeMember(guild, is_me=True)
    channel.members = [bot_user]
    human = _FakeMember(guild, is_bot=False)

    saved_grace = bot_mod.EMPTY_CHANNEL_GRACE_SEC
    bot_mod.EMPTY_CHANNEL_GRACE_SEC = 0.02
    with _Seams():
        async def main():
            task = asyncio.ensure_future(bot_mod.bot.on_voice_state_update(
                human, _VoiceState(channel=channel), _VoiceState(channel=None)))
            if turn_on_247_during_grace:
                await asyncio.sleep(0.005)
                state.always_on = True      # exact ce face !247 in acele secunde
            await task

        try:
            asyncio.run(main())
        finally:
            bot_mod.EMPTY_CHANNEL_GRACE_SEC = saved_grace
    return vc


def test_an_empty_channel_still_disconnects_after_the_grace_period():
    st = _fresh_state(always_on=False)
    vc = _empty_channel_tick(st, turn_on_247_during_grace=False)
    assert vc.disconnected, 'a rămas in canalul gol'


def test_247_switched_on_during_the_grace_period_is_respected():
    """20 de secunde sunt exact cat ii trebuie cuiva sa dea !247 vazand canalul
    golindu-se. Varianta care verifica `always_on` doar la intrare deconecta apoi
    sesiunea pe care el tocmai o pornise."""
    st = _fresh_state(always_on=False)
    vc = _empty_channel_tick(st, turn_on_247_during_grace=True)
    assert not vc.disconnected, (
        'a deconectat o sesiune 24/7 pornita in timpul rabdarii de 20s')


# --- 4. valorile numerice din env nu au voie sa omoare procesul --------------

def test_a_typo_in_a_numeric_env_var_does_not_kill_the_boot():
    """Se scriu de mana in Railway, fara nicio validare si fara mesaj de eroare.

    Cu `int(os.getenv(...))` gol, "30O" devenea un ValueError la IMPORT: container
    care nu porneste si bot disparut, fara nicio linie care sa spuna de ce.
    """
    saved = os.environ.get('PROBA_NUMERICA')
    try:
        for bad in ('30O', '', '   ', '3,5', 'None', '12 34'):
            os.environ['PROBA_NUMERICA'] = bad
            assert config.env_num('PROBA_NUMERICA', 300) == 300, bad
    finally:
        if saved is None:
            os.environ.pop('PROBA_NUMERICA', None)
        else:
            os.environ['PROBA_NUMERICA'] = saved


def test_a_zero_is_clamped_to_the_floor_not_accepted():
    """Un 0 in WATCHDOG_STALL_SEC facea din watchdog o bucla de repornire
    instantanee; un 0 in DOWNLOAD_CACHE_MB stergea fiecare piesa imediat."""
    saved = os.environ.get('PROBA_NUMERICA')
    os.environ['PROBA_NUMERICA'] = '0'
    try:
        assert config.env_num('PROBA_NUMERICA', 300, low=60) == 60
        assert config.env_num('PROBA_NUMERICA', 300, low=50) == 50
    finally:
        if saved is None:
            os.environ.pop('PROBA_NUMERICA', None)
        else:
            os.environ['PROBA_NUMERICA'] = saved


def test_a_valid_value_is_still_honoured():
    saved = os.environ.get('PROBA_NUMERICA')
    os.environ['PROBA_NUMERICA'] = ' 900 '
    try:
        assert config.env_num('PROBA_NUMERICA', 300, low=60) == 900
        assert config.env_num('PROBA_NUMERICA', 300, low=60, high=600) == 600
        os.environ['PROBA_NUMERICA'] = '2.5'
        assert config.env_num('PROBA_NUMERICA', 1.2, cast=float) == 2.5
    finally:
        if saved is None:
            os.environ.pop('PROBA_NUMERICA', None)
        else:
            os.environ['PROBA_NUMERICA'] = saved


def test_every_numeric_env_read_goes_through_the_helper():
    """Regula mecanica: `int(os.getenv(...))` gol nu are voie sa reapara.

    Instanta reparata fara clasa se intoarce la primul env var nou.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    # Pe AST, nu pe text: comentariile si docstring-urile din proiect CITEAZA
    # forma interzisa ca sa explice de ce e interzisa, deci un regex pe linii
    # s-ar plange de propria documentatie.
    for path in [root / 'bot.py'] + sorted((root / 'music').glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, 'id', None) not in ('int', 'float'):
                continue
            inner = node.args[0] if node.args else None
            if not isinstance(inner, ast.Call):
                continue
            target = getattr(inner.func, 'value', None)
            if (getattr(inner.func, 'attr', None) in ('getenv', 'environ')
                    and getattr(target, 'id', None) == 'os'):
                offenders.append(f'{path.name}:{node.lineno}: '
                                 f'{ast.unparse(node)}')
    assert not offenders, (
        'citire numerica din env fara validare (foloseste config.env_num):\n'
        + '\n'.join(offenders))


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
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
