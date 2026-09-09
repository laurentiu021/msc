"""Regresii pentru clasa de bug-uri "botul tace pe veci, conectat".

Doua mecanisme, ambele mute cand se strica:

1. VoiceClient.stop() declanseaza ALWAYS callback-ul after_play. O oprire
   deliberata (seek, nplay, inlocuirea piesei) facea deci coada sa avanseze si
   stergea fisierul care tocmai pornea. Token-ul de generatie il invalideaza.

2. CancelledError e BaseException, nu Exception. process_play prindea doar
   `except Exception`, deci cand o comanda noua anula task-ul in care rula
   redarea, is_loading rămânea True pentru totdeauna. De atunci !play doar
   adauga in coada si nimic nu mai pornea.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_playback_lifecycle.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import player
from music.state import GuildState


class _FakeGuild:
    id = 1


class _FakeVoiceClient:
    def __init__(self, playing=False):
        self._playing = playing

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def stop(self):
        self._playing = False


class _FakeCtx:
    guild = _FakeGuild()

    def __init__(self, vc=None):
        self.voice_client = vc if vc is not None else _FakeVoiceClient()

    async def send(self, *a, **k):
        return None


def _patch(monkey):
    """Inlocuieste temporar atribute de modul; intoarce functia de restaurare."""
    saved = {(m, n): getattr(m, n, None) for m, n in monkey}
    for (m, n), v in monkey.items() if isinstance(monkey, dict) else []:
        pass
    return saved


def test_stale_after_play_does_nothing():
    state = GuildState()
    calls = {'cleanup': 0, 'next': 0}
    orig_cleanup, orig_next, orig_loop = player.cleanup_file, player.play_next, player._loop
    player.cleanup_file = lambda *a, **k: calls.__setitem__('cleanup', calls['cleanup'] + 1)
    player.play_next = lambda *a, **k: calls.__setitem__('next', calls['next'] + 1)
    player._loop = None
    try:
        stale = player.make_after_play(_FakeCtx(), state, 'vechi.opus')
        # Cineva opreste deliberat si preia redarea.
        player.bump_play_generation(state)
        stale(None)
        assert calls == {'cleanup': 0, 'next': 0}, (
            f'callback-ul invechit a actionat: {calls}')
    finally:
        player.cleanup_file, player.play_next, player._loop = orig_cleanup, orig_next, orig_loop


def test_current_after_play_advances():
    state = GuildState()
    calls = {'cleanup': 0, 'next': 0}
    orig_cleanup, orig_next, orig_loop = player.cleanup_file, player.play_next, player._loop
    player.cleanup_file = lambda *a, **k: calls.__setitem__('cleanup', calls['cleanup'] + 1)
    player.play_next = lambda *a, **k: calls.__setitem__('next', calls['next'] + 1)
    player._loop = None
    try:
        current = player.make_after_play(_FakeCtx(), state, 'curent.opus')
        current(None)
        assert calls == {'cleanup': 1, 'next': 1}, (
            f'sfarsitul normal de piesa nu a avansat coada: {calls}')
    finally:
        player.cleanup_file, player.play_next, player._loop = orig_cleanup, orig_next, orig_loop


def test_generation_is_monotonic():
    state = GuildState()
    first = player.bump_play_generation(state)
    second = player.bump_play_generation(state)
    assert second == first + 1
    assert state.play_generation == second


def _run_process_play_raising(exc):
    """Ruleaza process_play cu extractia inlocuita de o eroare data."""
    state = GuildState()
    state.last_url = 'https://youtu.be/x'

    async def boom(*a, **k):
        raise exc

    async def noop(*a, **k):
        return None

    orig = {
        'extract': player._yt_extract_info,
        'cleanup': player.cleanup_file,
        'next': player.play_next,
        'timeout': player.start_timeout,
        'ui': player.update_player_ui,
        'loop': player._loop,
        'sleep': asyncio.sleep,
    }
    player._yt_extract_info = boom
    player.cleanup_file = lambda *a, **k: None
    player.play_next = lambda *a, **k: None
    player.start_timeout = lambda *a, **k: None
    player.update_player_ui = noop
    player._loop = None

    async def drive():
        from music import state as state_mod
        state_mod.guild_states[1] = state
        # backoff-ul de la finalul erorii nu trebuie sa incetineasca testul
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda *_a, **_k: real_sleep(0)
        try:
            await player.process_play(_FakeCtx(), 'ceva')
        finally:
            asyncio.sleep = real_sleep

    try:
        return state, drive()
    finally:
        player._yt_extract_info = orig['extract']
        player.cleanup_file = orig['cleanup']
        player.play_next = orig['next']
        player.start_timeout = orig['timeout']
        player.update_player_ui = orig['ui']
        player._loop = orig['loop']


def test_is_loading_cleared_on_cancellation():
    """Bug-ul central: anularea lasa is_loading=True si botul tace la orice !play."""
    state = GuildState()
    state.last_url = 'https://youtu.be/x'

    async def boom(*a, **k):
        raise asyncio.CancelledError()

    async def noop(*a, **k):
        return None

    saved = (player._yt_extract_info, player.cleanup_file, player.play_next,
             player.start_timeout, player.update_player_ui, player._loop)
    player._yt_extract_info = boom
    player.cleanup_file = lambda *a, **k: None
    player.play_next = lambda *a, **k: None
    player.start_timeout = lambda *a, **k: None
    player.update_player_ui = noop
    player._loop = None
    try:
        from music import state as state_mod
        state_mod.guild_states[1] = state

        async def drive():
            try:
                await player.process_play(_FakeCtx(), 'ceva')
            except asyncio.CancelledError:
                pass  # se propaga corect, cum trebuie

        asyncio.run(drive())
        assert state.is_loading is False, (
            'is_loading a rămas True dupa anulare: botul ar tacea la orice !play')
    finally:
        (player._yt_extract_info, player.cleanup_file, player.play_next,
         player.start_timeout, player.update_player_ui, player._loop) = saved


def test_is_loading_cleared_on_ordinary_error():
    state = GuildState()
    state.last_url = 'https://youtu.be/x'

    async def boom(*a, **k):
        raise ValueError('yt-dlp a picat')

    async def noop(*a, **k):
        return None

    saved = (player._yt_extract_info, player.cleanup_file, player.play_next,
             player.start_timeout, player.update_player_ui, player._loop)
    player._yt_extract_info = boom
    player.cleanup_file = lambda *a, **k: None
    player.play_next = lambda *a, **k: None
    player.start_timeout = lambda *a, **k: None
    player.update_player_ui = noop
    player._loop = None
    try:
        from music import state as state_mod
        state_mod.guild_states[1] = state
        asyncio.run(player.process_play(_FakeCtx(), 'ceva'))
        assert state.is_loading is False, 'is_loading a rămas True dupa o eroare obisnuita'
        assert state._consecutive_errors == 1
    finally:
        (player._yt_extract_info, player.cleanup_file, player.play_next,
         player.start_timeout, player.update_player_ui, player._loop) = saved


def test_breaker_sets_a_real_cooldown():
    """Fara pauza, timer-ul de 24/7 reactiva autoplay la fiecare 60s."""
    state = GuildState()
    state._consecutive_errors = 5
    state.autoplay = True

    async def noop(*a, **k):
        return None

    saved = (player.start_timeout, player.update_player_ui)
    player.start_timeout = lambda *a, **k: None
    player.update_player_ui = noop
    try:
        from music import state as state_mod
        state_mod.guild_states[1] = state
        asyncio.run(player.process_play(_FakeCtx(), 'ceva'))
        assert state.autoplay is False
        assert state.breaker_until > 0, 'intrerupatorul nu a pus nicio pauza'
        assert state._consecutive_errors == 0
    finally:
        player.start_timeout, player.update_player_ui = saved


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
