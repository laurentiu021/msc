"""Panoul trebuie sa se repare singur, sa nu se dubleze, si sa nu minta despre timp.

Trei defecte, toate vizibile doar din Discord:

1. `!play` putea produce ZERO output. Comanda isi sterge propriul mesaj, iar
   ramura de coada chema doar `update_player_ui(send_new=False)`, care era
   `elif state.current_msg:` — deci cand panoul nu mai exista (dupa `!stop`, dupa
   mesajul de plecare, dupa un 403), comanda nu facea absolut nimic vizibil.

2. Ramura de edit suprascria `state.current_view` fara sa opreasca view-ul
   vechi. `Message.delete()` nu il scoate din ViewStore-ul lui discord.py, deci
   fiecare piesa lasa un view viu, pe viata procesului.

3. Panoul calcula finalul ca `last_start_time + last_duration`, si nimic nu
   ajusta `last_start_time` la pauza: dupa o pauza de 10 minute anunta ca piesa
   s-a terminat acum 6 minute.

    python tests/test_panel_and_timing.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import state as state_mod, ui
from music.state import (GuildState, mark_paused, mark_resumed)
from music.utils import playback_remaining

GUILD_ID = 21


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


class _FakeMessage:
    def __init__(self, sink):
        self.sink = sink
        self.edits = 0
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits += 1
        self.sink.append('edit')

    async def delete(self):
        self.deleted = True


class _FakeCtx:
    def __init__(self, vc):
        self.guild = type('G', (), {'id': GUILD_ID})()
        self.voice_client = vc
        self.bot = type('B', (), {'loop': None})()
        self.events = []

    async def send(self, *args, **kwargs):
        self.events.append('send')
        return _FakeMessage(self.events)


def _fresh_state():
    st = GuildState()
    st.last_title = 'Artist - Piesa'
    st.last_url = 'https://www.youtube.com/watch?v=x'
    state_mod.guild_states[GUILD_ID] = st
    return st


def test_the_panel_heals_itself_when_it_is_gone():
    st = _fresh_state()
    st.current_msg = None
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    asyncio.run(ui.update_player_ui(ctx, send_new=False))
    assert ctx.events == ['send'], (
        'panoul lipsea si actualizarea nu a facut nimic: !play e complet invizibil')
    assert st.current_msg is not None


def test_an_existing_panel_is_edited_not_re_sent():
    st = _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    st.current_msg = _FakeMessage(ctx.events)
    asyncio.run(ui.update_player_ui(ctx, send_new=False))
    assert ctx.events == ['edit'], ctx.events


def test_the_edit_branch_stops_the_old_view():
    st = _fresh_state()
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    st.current_msg = _FakeMessage(ctx.events)

    stopped = []

    class _OldView:
        def stop(self):
            stopped.append(True)

    st.current_view = _OldView()
    asyncio.run(ui.update_player_ui(ctx, send_new=False))
    assert stopped, 'view-ul vechi a rămas viu in ViewStore la fiecare piesa'
    assert st.current_view is not None and not isinstance(st.current_view, _OldView)


def test_a_send_in_flight_blocks_a_second_panel():
    st = _fresh_state()
    st.current_msg = None
    st._ui_sending = True                  # o trimitere e deja in zbor
    ctx = _FakeCtx(_FakeVoiceClient(playing=True))
    asyncio.run(ui.update_player_ui(ctx, send_new=True))
    assert ctx.events == [], 'a trimis un al doilea panou cu butoane vii'


def test_the_send_guard_is_released_even_on_a_403():
    st = _fresh_state()
    st.current_msg = None

    class _Forbidden(_FakeCtx):
        async def send(self, *a, **k):
            import discord
            raise discord.HTTPException(
                type('R', (), {'status': 403, 'reason': 'Forbidden'})(), 'nope')

    asyncio.run(ui.update_player_ui(_Forbidden(_FakeVoiceClient(playing=True)),
                                    send_new=True))
    assert st._ui_sending is False, 'garda a rămas pusa: panoul nu mai revine niciodata'
    assert st.current_msg is None


def test_remaining_time_ignores_the_pause():
    # piesa de 300s, pornita la t=1000, pauzata la t=1100 (100s consumate)
    elapsed, remaining = playback_remaining(now=1700, start_time=1000,
                                            duration=300, paused_at=1100)
    assert (elapsed, remaining) == (100, 200), (elapsed, remaining)


def test_remaining_time_while_playing():
    elapsed, remaining = playback_remaining(now=1120, start_time=1000, duration=300)
    assert (elapsed, remaining) == (120, 180), (elapsed, remaining)


def test_remaining_time_never_goes_negative_or_past_the_duration():
    assert playback_remaining(now=9999, start_time=1000, duration=300) == (300, 0)
    assert playback_remaining(now=1000, start_time=1000, duration=300) == (0, 300)


def test_remaining_time_without_data_is_zero():
    assert playback_remaining(now=1000, start_time=0, duration=300) == (0, 0)
    assert playback_remaining(now=1000, start_time=1000, duration=0) == (0, 0)


def test_pause_then_resume_shifts_the_start():
    st = _fresh_state()
    st.last_start_time = 1000.0
    st.last_duration = 300

    mark_paused(st, 1100.0)
    assert st.paused_at == 1100.0
    # o a doua pauza nu are voie sa mute reperul
    mark_paused(st, 1200.0)
    assert st.paused_at == 1100.0

    mark_resumed(st, 1700.0)               # pauza de 600s
    assert st.paused_at == 0.0
    assert st.last_start_time == 1600.0, st.last_start_time
    # exact cele 100s consumate inainte de pauza
    elapsed, remaining = playback_remaining(1700.0, st.last_start_time,
                                            st.last_duration, st.paused_at)
    assert (elapsed, remaining) == (100, 200), (elapsed, remaining)


def test_resume_without_a_pause_changes_nothing():
    st = _fresh_state()
    st.last_start_time = 1000.0
    mark_resumed(st, 5000.0)
    assert st.last_start_time == 1000.0


def test_both_pause_sites_go_through_the_shared_transitions():
    """Doua locuri pauzeaza redarea; niciunul nu are voie sa isi scrie propria regula."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ('bot.py', 'music/views.py'):
        src = (root / rel).read_text(encoding='utf-8')
        for call, marker in (('.pause()', 'mark_paused'), ('.resume()', 'mark_resumed')):
            if call in src:
                assert marker in src, f'{rel}: {call} fara {marker}'
        assert not re.search(r'paused_at\s*=\s*time\.time\(\)', src), (
            f'{rel}: tranzitia de pauza a fost re-scrisa pe loc')


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
