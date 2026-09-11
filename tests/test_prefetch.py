"""Descarcarea in avans a piesei urmatoare, si pragul de completare a cozii.

Fara prefetch, fiecare skip plateste extractia plus descarcarea in fața
utilizatorului: masurat in producție, 26.8s pe o piesa rece contra 2.1s pe un hit
de cache. Pe autoplay e regula, nu excepția, fiindca intrarile din coada sunt doar
URL-uri pe care nimeni nu le-a atins inca.

Invariantul care conteaza cel mai mult nu e viteza, ci ce NU are voie sa atinga
prefetch-ul: `is_loading`. Steagul acela e o promisiune ca o incarcare in curs va
SCURGE coada, iar `!play` il citește ca "pune in coada, se va rezolva". Un prefetch
nu scurge nimic, deci daca l-ar aprinde, o piesa cerută in fereastra aceea ar
rămâne in coada pentru totdeauna.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_prefetch.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import config, player, state as state_mod
from music.state import GuildState

GUILD_ID = 31


class _Resolved:
    def __init__(self, filename=None, raw_error=None):
        self.filename = filename
        self.raw_error = raw_error


def _fresh_state():
    st = GuildState()
    state_mod.guild_states[GUILD_ID] = st
    return st


def _run_worker(state, *, cached=(), fails=()):
    """Ruleaza worker-ul de prefetch cu rezolvarea si cache-ul inlocuite."""
    asked = []

    def fake_cached_for(url):
        vid = url.rsplit('=', 1)[-1] if url else None
        return vid, ('/cache/hit.opus' if vid in cached else None)

    async def fake_resolve(url, *, loop=None, should_continue=None):
        asked.append(url)
        if url in fails:
            return _Resolved(raw_error='fara formate')
        return _Resolved(filename='/cache/nou.opus')

    saved = (player.cached_for, player.resolve_from_url, player.trim_cache)
    player.cached_for = fake_cached_for
    player.resolve_from_url = fake_resolve
    player.trim_cache = lambda: None
    try:
        asyncio.run(player._prefetch_worker(state))
    finally:
        player.cached_for, player.resolve_from_url, player.trim_cache = saved
    return asked


def test_the_next_track_is_downloaded_before_it_is_needed():
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=aaa', 'title': 'A'},
                {'query': 'https://www.youtube.com/watch?v=bbb', 'title': 'B'}]
    asked = _run_worker(st)
    assert asked == ['https://www.youtube.com/watch?v=aaa'], (
        f'PREFETCH_AHEAD={config.PREFETCH_AHEAD}, cerute: {asked}')


def test_a_prefetch_never_touches_the_loading_flag():
    """Cel mai important invariant. Vezi docstring-ul fisierului."""
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=aaa', 'title': 'A'}]
    _run_worker(st)
    assert st.is_loading is False, (
        'prefetch-ul a aprins is_loading: o piesa cerută in fereastra aceea '
        'rămâne in coada pentru totdeauna')
    assert st.current_file is None, 'prefetch-ul a revendicat fisierul sesiunii'
    assert not st.last_url, 'prefetch-ul a suprascris piesa curenta'
    assert st.queue, 'prefetch-ul a consumat coada'


def test_an_already_cached_track_costs_no_request():
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=aaa', 'title': 'A'}]
    assert _run_worker(st, cached={'aaa'}) == [], 'a re-descarcat un fisier din cache'


def test_a_text_query_is_left_alone():
    """Ar cere o extractie in plus doar ca sa afle ce sa caute in cache — exact
    cererea pe care prefetch-ul incearca sa o economiseasca."""
    st = _fresh_state()
    st.queue = [{'query': 'macarena los del rio', 'title': 'M'}]
    asked = []

    async def fake_resolve(url, *, loop=None, should_continue=None):
        asked.append(url)
        return _Resolved(filename='/cache/nou.opus')

    saved = (player.cached_for, player.resolve_from_url)
    player.cached_for = lambda url: (None, None)
    player.resolve_from_url = fake_resolve
    try:
        asyncio.run(player._prefetch_worker(st))
    finally:
        player.cached_for, player.resolve_from_url = saved
    assert asked == [], f'a cerut o rezolvare pentru un text de cautare: {asked}'


def test_a_failed_prefetch_is_not_an_error_for_the_track_that_plays():
    st = _fresh_state()
    url = 'https://www.youtube.com/watch?v=aaa'
    st.queue = [{'query': url, 'title': 'A'}]
    _run_worker(st, fails={url})
    assert st._consecutive_errors == 0, (
        'un prefetch eșuat a bătut contorul de erori al redarii')
    assert st.breaker_until == 0.0, 'un prefetch eșuat a armat intrerupatorul'


def test_only_one_prefetch_runs_at_a_time():
    """Doua ar dubla cererile pe un IP care deja ne limiteaza, si ambele ar scrie
    in ACELASI fisier: `outtmpl` e `%(id)s.%(ext)s`, deci calea E cheia de cache."""
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=aaa', 'title': 'A'}]

    class _Busy:
        def done(self):
            return False

    st.prefetch_task = _Busy()
    saved = player._loop
    player._loop = 'orice bucla'
    try:
        assert player.schedule_prefetch(st) == 'deja in curs'
    finally:
        player._loop = saved


def test_prefetch_can_be_switched_off_without_a_deploy():
    """Plasa de siguranța daca YouTube incepe sa numere cererile mai strict."""
    st = _fresh_state()
    st.queue = [{'query': 'https://www.youtube.com/watch?v=aaa', 'title': 'A'}]
    saved = player.PREFETCH_AHEAD
    player.PREFETCH_AHEAD = 0
    try:
        assert player.schedule_prefetch(st) == 'dezactivat'
    finally:
        player.PREFETCH_AHEAD = saved


def test_an_empty_queue_schedules_nothing():
    st = _fresh_state()
    st.queue = []
    saved = player._loop
    player._loop = 'orice bucla'
    try:
        assert player.schedule_prefetch(st) == 'coada goala'
    finally:
        player._loop = saved


def test_playback_starts_a_prefetch():
    """Un worker corect pe care nu il cheama nimeni nu grabește nimic."""
    import ast
    import inspect

    src = inspect.getsource(player.process_play)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    assert 'schedule_prefetch' in called, (
        'redarea nu mai porneste nicio descarcare in avans')


def test_a_queue_filled_after_playback_still_gets_prefetched():
    """Fluxul normal: pornesti o piesa (coada goala), apoi aprinzi Autoplay.

    Prefetch-ul pornea DOAR din `process_play`, unde coada e aproape mereu goala,
    deci in fluxul asta nu se intampla nimic si primul skip platea integral
    extractia plus descarcarea. Masurat in emulator: 30 de secunde cu coada plina
    si cache-ul gol. `resume_if_idle` e locul, fiindca pe acolo trece fiecare
    operatie care schimba coada fara sa porneasca nimic.
    """
    import ast
    import inspect

    src = inspect.getsource(player.resume_if_idle)
    tree = ast.parse(src.strip())
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert 'schedule_prefetch' in called, (
        'coada schimbata fara redare nu mai declanseaza nicio descarcare in avans')

    # Si INAINTE de ieșirea "canta deja": acela e chiar cazul obișnuit.
    body = tree.body[0].body
    order = []
    for node in body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and ast.unparse(sub.func) == 'schedule_prefetch':
                order.append('prefetch')
        if isinstance(node, ast.If) and 'is_playing' in ast.unparse(node.test):
            order.append('canta deja')
    assert order[:1] == ['prefetch'], (
        f'prefetch-ul e programat dupa ieșirea "canta deja", adica niciodata in '
        f'fluxul redare-apoi-autoplay: {order}')


def test_a_refill_after_a_drained_queue_gets_prefetched():
    """Coada golita complet: prefetch-ul de la pornire n-a avut ce sa ia."""
    import ast
    import inspect

    src = inspect.getsource(player._play_next_async)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    assert 'schedule_prefetch' in called, (
        'dupa refill nu se incalzește nimic: urmatorul skip plateste tot')


def test_the_refill_threshold_keeps_a_real_palette():
    """Lista de sub panou ESTE coada, deci pragul decide din cate piese poți
    alege. La 3, o singura alegere din dropdown te lasa fara opțiuni."""
    assert config.AUTOPLAY_REFILL_BELOW >= 6, config.AUTOPLAY_REFILL_BELOW
    assert config.AUTOPLAY_QUEUE_TARGET > config.AUTOPLAY_REFILL_BELOW, (
        'ținta nu e peste prag: refill-ul nu ar adauga nimic')


def test_the_refill_threshold_is_read_from_one_place():
    import ast
    import inspect

    src = inspect.getsource(player._play_next_async)
    numbers = {n.value for n in ast.walk(ast.parse(src.strip()))
               if isinstance(n, ast.Constant) and isinstance(n.value, int)}
    assert 3 not in numbers, (
        'pragul de refill e iar scris de mana in play_next, nu citit din config')
    assert 'AUTOPLAY_REFILL_BELOW' in src, src[:200]


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
