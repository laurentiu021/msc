"""Cererile catre YouTube costa: pe IP de datacenter fiecare una in plus atrage 429.

Botul isi fabrica singur limitarile de rata. Testele de aici pazesc reducerile:

- opts izolate per apel. YoutubeDL MUTEAZA dict-ul primit, iar codul pasa
  aceleasi dict-uri globale, cu un singur obiect extractor_args partajat.
- un singur throttle pentru tot botul. autoplay.py si calea de playlist chemau
  yt_dlp direct si ocoleau limitatorul — exact componenta care cere 50 de
  intrari dintr-un YouTube Mix.
- lista de formate scurta. Fiecare format incercat e o re-extractie completa in
  spatele unui throttle de 1.2-3.2s; erau sase, dintre care doua duplicate ale
  alternativelor din prima.
- limite pe descarcare. Fara match_filter, un live stream cu download=True nu
  se termina niciodata si blocheaza un thread pe viata procesului.

Ruleaza fara pytest si fara retea:  python tests/test_request_efficiency.py
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import autoplay, config, player, ytdlp
from music.state import GuildState


def test_option_dicts_are_isolated_per_call():
    a = config.make_search_opts()
    b = config.make_search_opts()
    assert a is not b
    assert a['extractor_args'] is not b['extractor_args'], 'extractor_args partajat'
    a['extractor_args']['youtube']['player_client'].append('CONTAMINAT')
    a['outtmpl'] = 'undeva/rau'
    assert 'CONTAMINAT' not in config.YDL_OPTS_SEARCH['extractor_args']['youtube']['player_client']
    assert 'outtmpl' not in config.YDL_OPTS_SEARCH, (
        'YDL_OPTS_SEARCH nu trebuie sa capete outtmpl: fisierele ar ajunge in CWD')


def test_download_opts_have_real_limits():
    opts = config.make_download_opts()
    assert callable(opts.get('match_filter')), 'lipseste match_filter'
    assert opts.get('max_filesize'), 'lipseste max_filesize'


def test_match_filter_rejects_live_and_overlong():
    match = config.make_download_opts()['match_filter']
    assert match({'is_live': True, 'duration': 100, 'title': 'live'}) is not None, \
        'live-ul trebuie respins'
    assert match({'is_live': False, 'duration': 99999, 'title': 'lung'}) is not None, \
        'prea lung trebuie respins'
    assert match({'is_live': False, 'duration': 200, 'title': 'ok'}) is None, \
        'o piesa normala nu trebuie respinsa'


def test_tls_verification_is_on():
    for opts in (config.make_search_opts(), config.make_download_opts()):
        assert not opts.get('nocheckcertificate'), (
            'nocheckcertificate expune cookie-urile sesiunii Google')


def test_search_asks_for_several_candidates():
    """Cu ytsearch (un rezultat) filtrul is_clean nu avea din ce alege."""
    ds = config.make_search_opts()['default_search']
    assert ds.startswith('ytsearch') and ds != 'ytsearch', ds
    assert int(ds.replace('ytsearch', '')) >= 3


def test_text_search_is_flat_then_one_full_extraction():
    """ytsearchN fara extract_flat extrage COMPLET toate rezultatele.

    Masurat: cu opts-urile reale ale repo-ului, ytsearch5 producea cinci
    extractii complete, adica ~20 de cereri catre YouTube pentru un !play, toate
    in acelasi slot de throttle si acelasi buget de 90s.
    """
    src = inspect.getsource(player._resolve_query_to_url)
    assert 'extract_flat=True' in src, 'cautarea de text nu mai e flat'
    # si nu mai luam orbeste primul rezultat cand niciunul nu trece filtrul
    assert 'is_clean' in src
    code = ''.join(line.split('#')[0] for line in src.splitlines())
    assert 'entries[0]' not in code, 'inca ia orbeste primul rezultat'


def test_format_list_is_short_and_without_duplicates():
    src = inspect.getsource(player.process_play)
    block = src.split('formats_to_try = [')[1].split(']')[0]
    entries = [line.strip().strip("',") for line in block.split('\n') if line.strip()]
    assert len(entries) <= 3, f'prea multe formate incercate: {len(entries)}'
    assert len(set(entries)) == len(entries), 'formate duplicate'


def test_preload_is_gone():
    """preload_next nu folosea niciodata cookies, deci pe Railway eseua mereu."""
    assert not hasattr(player, 'preload_next'), (
        'preload_next a revenit: dubla cererile si corupea .part-urile partajate')


def test_every_ytdlp_call_goes_through_the_shared_throttle():
    """Nicio cerere YouTube nu are voie sa cheme yt_dlp.YoutubeDL direct."""
    offenders = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ('player.py', 'autoplay.py', 'commands.py'):
        path = os.path.join(root, 'music', name)
        body = open(path, encoding='utf-8').read()
        if 'yt_dlp.YoutubeDL(' in body:
            offenders.append(name)
    assert not offenders, f'ocolesc throttle-ul: {offenders}'


def test_throttle_reserves_the_slot_under_the_lock():
    """Doi apelanti concurenti nu au voie sa plece impreuna.

    Cand rezervarea se facea numai dupa terminarea cererii, amandoi citeau un
    _NEXT_ALLOWED_AT deja trecut, nu așteptau nimic si porneau simultan — exact
    rafala pe care throttle-ul exista sa o previna.
    """
    src = inspect.getsource(ytdlp.wait_for_slot)
    assert 'async with _LOCK' in src
    assert '_NEXT_ALLOWED_AT = time.time()' in src, (
        'rezervarea trebuie sa se intample sub lock, in wait_for_slot')
    extend = inspect.getsource(ytdlp._reserve_next_slot)
    assert 'max(' in extend, 'prelungirea de dupa cerere nu are voie sa scurteze pauza'
    assert 'finally' in inspect.getsource(ytdlp.extract)


def test_ytdlp_uses_a_bounded_dedicated_executor():
    """asyncio.wait_for anuleaza aȘteptarea, nu thread-ul: el continua sa ruleze."""
    assert ytdlp._EXECUTOR is not None
    assert ytdlp._EXECUTOR._max_workers <= 8
    for fn in (ytdlp.extract, ytdlp.extract_and_prepare_filename):
        src = inspect.getsource(fn)
        assert '_EXECUTOR' in src, (
            f'{fn.__name__} nu foloseste executorul dedicat, ci pe cel implicit')
        assert 'None, lambda' not in src, (
            f'{fn.__name__} inca trimite catre executorul implicit')


def test_autoplay_seeds_from_the_current_track():
    """state.history[0] e cea mai VECHE intrare, deci radio-ul rămânea pinuit."""
    src = inspect.getsource(autoplay.prefill_autoplay_queue)
    # doar codul, fara comentarii: comentariul explica de ce history[0] e greșit
    code = '\n'.join(line.split('#')[0] for line in src.split('\n'))
    assert 'state.last_url or' in code, code[:200]
    assert 'state.history[0]' not in code, 'inca se seamana din cea mai veche intrare'


def test_artist_key_is_the_same_for_counting_and_checking():
    """Numararea folosea prefixul titlului, verificarea folosea canalul."""
    key = autoplay.artist_key
    assert key('Luis Gabriel - Toate diamantele', 'Alt Canal') == 'luis gabriel'
    assert key('fara separator', 'Numele Canalului') == 'numele canalului'
    assert key(None, None) == ''
    # aceeasi piesa vazuta cu si fara canal trebuie sa dea aceeasi cheie
    assert key('X - Y', '') == key('X - Y', 'Canal Diferit')


def test_add_to_queue_rejects_live_and_bad_durations():
    state = GuildState()
    skip = set()
    assert autoplay._add_to_queue(state, 'a1', 'melodie', skip, None, '',
                                  duration=200) is True
    assert autoplay._add_to_queue(state, 'a2', 'melodie live', skip, None, '',
                                  live_status='is_live') is False
    assert autoplay._add_to_queue(state, 'a3', 'melodie', skip, None, '',
                                  duration=99999) is False
    assert autoplay._add_to_queue(state, 'a4', None, skip, None, '') is False
    assert autoplay._add_to_queue(state, 'a5', 'lofi beats', skip, None, '') is False


def test_add_to_queue_caps_the_same_artist():
    state = GuildState()
    skip, counts = set(), {}
    added = [autoplay._add_to_queue(state, f'v{i}', f'Artistul - Piesa {i}',
                                    skip, counts, '', duration=200)
             for i in range(5)]
    assert added.count(True) == autoplay.MAX_SAME_ARTIST, added


def test_refill_threshold_is_below_the_target():
    """Prag egal cu target-ul insemna un refill la FIECARE piesa."""
    src = inspect.getsource(player._play_next_async)
    assert 'len(state.queue) < 3' in src, src[src.index('state.autoplay and'):][:80]
    target = inspect.signature(autoplay.prefill_autoplay_queue).parameters['target'].default
    assert target > 3, f'target={target} trebuie sa fie peste prag'


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
