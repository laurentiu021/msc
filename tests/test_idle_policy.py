"""Tabel de decizii pentru inactivitate: zero mock-uri, timp injectat.

Politica de 24/7 era singura din proiect fara acoperire, si singura scrisa direct
in punctul de intrare, sudata pe un task viu si pe `bot.loop`. Bug-ul pe care il
inchide randul "radio oprit de utilizator": tick-ul punea `autoplay = True`
necondiționat, deci butonul Autoplay se stingea singur dupa 60 de secunde si o
coada curatata manual era inlocuita de un Mix de YouTube. Butonul parea sa
functioneze, apoi revenea in tacere.

    python tests/test_idle_policy.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music.idle import DISCONNECT, NOTHING, RADIO, decide_idle_action
from music.state import GuildState, set_autoplay

NOW = 1_000_000.0


def _state(**fields):
    st = GuildState()
    for key, value in fields.items():
        setattr(st, key, value)
    return st


def _decide(st, connected=True, playing=False, paused=False, now=NOW):
    return decide_idle_action(st, connected=connected, playing=playing,
                              paused=paused, now=now)


# (nume, stare, kwargs de apel, actiune aȘteptata, reluare aȘteptata)
TABLE = [
    ('fara 24/7, inactiv -> pleaca',
     dict(always_on=False), dict(), DISCONNECT, False),
    ('fara 24/7, se reda -> nimic',
     dict(always_on=False), dict(playing=True), NOTHING, False),
    ('fara 24/7, pauzat -> nimic (pauza nu e inactivitate)',
     dict(always_on=False), dict(paused=True), NOTHING, False),
    ('fara 24/7, deconectat -> nimic',
     dict(always_on=False), dict(connected=False), NOTHING, False),

    ('24/7 cu radio pornit -> radio',
     dict(always_on=True, autoplay=True, last_url='u'), dict(), RADIO, False),
    ('24/7 dar se reda deja -> nimic',
     dict(always_on=True, autoplay=True, last_url='u'), dict(playing=True),
     NOTHING, False),
    ('24/7 dar o incarcare e in curs -> nimic',
     dict(always_on=True, autoplay=True, last_url='u', is_loading=True), dict(),
     NOTHING, False),
    ('24/7 dar intrerupatorul e activ -> nimic',
     dict(always_on=True, autoplay=True, last_url='u', breaker_until=NOW + 60),
     dict(), NOTHING, False),
    ('24/7 dar suntem in pauza de liniste -> nimic',
     dict(always_on=True, autoplay=True, last_url='u', idle_quiet_until=NOW + 10),
     dict(), NOTHING, False),
    ('24/7 dar nu s-a redat nimic inca -> nimic',
     dict(always_on=True, autoplay=True, last_url=None), dict(), NOTHING, False),
    ('24/7 dupa expirarea intrerupatorului -> radio',
     dict(always_on=True, autoplay=True, last_url='u', breaker_until=NOW - 1),
     dict(), RADIO, False),

    ('24/7, radio oprit de o DEFECTIUNE -> reluare',
     dict(always_on=True, autoplay=False, autoplay_user_off=False, last_url='u'),
     dict(), RADIO, True),
    ('24/7, radio oprit de UTILIZATOR -> nimic, dar rămâne in canal',
     dict(always_on=True, autoplay=False, autoplay_user_off=True, last_url='u'),
     dict(), NOTHING, False),
]


def test_the_decision_table():
    wrong = []
    for name, fields, call, expected, resume in TABLE:
        d = _decide(_state(**fields), **call)
        if d.action != expected or d.resume_autoplay != resume:
            wrong.append(f'{name}: {d.action}/resume={d.resume_autoplay} '
                         f'!= {expected}/resume={resume}')
    assert not wrong, 'decizii greșite:\n  ' + '\n  '.join(wrong)


def test_every_decision_carries_a_reason():
    """Fiecare ramura ieșea printr-un `return` mut, deci "de ce nu cânta?" nu
    avea niciun raspuns nici in loguri, nici in `!debug`."""
    for name, fields, call, _, _ in TABLE:
        d = _decide(_state(**fields), **call)
        assert d.reason, f'{name}: decizie fara motiv'


def test_the_autoplay_button_survives_a_tick():
    """Scenariul raportat: apeși Autoplay off in 24/7 si dupa 60s revine singur."""
    st = _state(always_on=True, last_url='u')
    set_autoplay(st, True, by_user=True)          # !247 porneste radioul
    assert _decide(st).action == RADIO

    set_autoplay(st, False, by_user=True)         # butonul din panou
    d = _decide(st)
    assert d.action == NOTHING and not d.resume_autoplay, d
    # Si la tick-ul urmator, si la al zecelea.
    for _ in range(10):
        assert _decide(st, now=NOW + 600).action == NOTHING


def test_a_breaker_trip_still_recovers_by_itself():
    """Oprirea automata nu e o preferinta: 24/7 trebuie sa se ridice singur."""
    st = _state(always_on=True, last_url='u', autoplay=True)
    # exact ce face intrerupatorul din process_play
    st.autoplay = False
    st.breaker_until = NOW + 900

    assert _decide(st).action == NOTHING, 'nu are voie sa reia in timpul pauzei'
    d = _decide(st, now=NOW + 901)
    assert d.action == RADIO and d.resume_autoplay is True, d


def test_the_decision_function_has_no_side_effects():
    st = _state(always_on=True, autoplay=False, last_url='u')
    before = {k: v for k, v in vars(st).items() if not k.startswith('_')}
    _decide(st)
    after = {k: v for k, v in vars(st).items() if not k.startswith('_')}
    assert before == after, 'functia de decizie a modificat starea'


def test_the_entry_point_only_orchestrates():
    """Politica nu are voie sa se re-scrie in bot.py, unde nu se poate testa."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((root / 'bot.py').read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == 'idle_timer')
    src = ast.unparse(fn)
    assert 'decide_idle_action' in src, 'idle_timer nu mai foloseste politica'
    # Intrarile de politica nu au ce cauta in punctul de intrare. `always_on`
    # rămâne permis: el decide si RE-ARMAREA timer-ului, care e treaba
    # orchestratorului, nu a politicii.
    # `idle_quiet_until` nu e in lista: politica il CITESTE, dar orchestratorul
    # il SCRIE, pentru ca abia dupa prefill se afla ca n-a ieșit nimic.
    leaked = [name for name in ('autoplay_user_off', 'breaker_until', 'last_url')
              if name in src]
    assert not leaked, (
        f'ramuri de politica reapărute in bot.py: {leaked}; locul lor e in music/idle.py')


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
