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
    # Si campul de stare, plus cele patru blocuri de curatare care il pazeau.
    # Functia fusese stearsa, dar `state.preloaded` primea doar None de la patru
    # locuri diferite, unul pe calea fierbinte a fiecarei piese — cod mort cu
    # curatare vie, care il facea pe cititor sa creada ca preload-ul exista.
    assert not hasattr(GuildState(), 'preloaded'), 'state.preloaded a revenit'
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import glob
    leftovers = [os.path.basename(p)
                 for p in glob.glob(os.path.join(root, 'music', '*.py'))
                 + [os.path.join(root, 'bot.py')]
                 if 'preloaded' in open(p, encoding='utf-8').read()]
    assert not leftovers, f'referinte la preloaded ramase in: {leftovers}'


# Cate apeluri directe `yt_dlp.YoutubeDL(` are voie fiecare fisier. Orice
# altceva ocoleste poarta din music/ytdlp.py: fara throttle, fara plafon de timp
# si cu propriul obiect care poate rescrie fisierul de cookies.
_DIRECT_YTDLP_ALLOWED = {
    # Proba de pornire e sincrona si ruleaza INAINTE ca bucla de evenimente sa
    # existe, deci nu poate folosi poarta async. E opt-in (YTDLP_STARTUP_PROBE),
    # o singura cerere, la boot.
    'bot.py': 1,
    # ytdlp.py ESTE poarta.
    'music/ytdlp.py': 1,
}


def test_every_ytdlp_call_goes_through_the_shared_throttle():
    """Scanare pe TOT pachetul, cu allowlist explicita.

    Verificarea se uita la trei fisiere alese manual si rata exact apelul care
    nu avea nici throttle, nici socket_timeout, nici plafon
    (commands._resolve_platform_url), plus proba din bot.py.
    """
    import ast
    import glob

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for path in sorted(glob.glob(os.path.join(root, 'music', '*.py'))
                       + [os.path.join(root, 'bot.py')]):
        rel = os.path.relpath(path, root).replace('\\', '/')
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        # Pe AST, nu pe text: docstring-ul lui ytdlp.py citeaza tocmai forma
        # interzisa ca sa explice de ce e interzisa.
        count = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == 'yt_dlp.YoutubeDL')
        allowed = _DIRECT_YTDLP_ALLOWED.get(rel, 0)
        if count != allowed:
            offenders.append(f'{rel}: {count} apeluri directe, permise {allowed}')
    assert not offenders, 'ocolesc poarta:\n  ' + '\n  '.join(offenders)


def test_the_pinned_versions_are_the_ones_actually_installed():
    """Testele care ruleaza pe alt extractor decat producția nu dovedesc nimic.

    yt-dlp e singura dependenta care se sparge de la sine, iar local rula
    2026.07.04 in timp ce requirements.txt pinuia 2026.8.19 — deci fiecare
    verificare de comportament yt-dlp era facuta pe alt cod decat cel livrat.
    """
    import re

    import yt_dlp

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reqs = open(os.path.join(root, 'requirements.txt'), encoding='utf-8').read()

    floating = [l.strip() for l in reqs.splitlines()
                if l.strip() and not l.startswith('#') and '==' not in l]
    assert not floating, f'dependente nepinuite: {floating}'

    pin = re.search(r'^yt-dlp\[default\]==(.+)$', reqs, re.M)
    assert pin, 'pinul yt-dlp a dispărut din requirements.txt'
    want = pin.group(1).strip()
    have = yt_dlp.version.__version__
    # 2026.8.19 si 2026.08.19 sunt aceeasi versiune pentru pip.
    norm = lambda v: tuple(int(p) for p in v.split('.'))
    assert norm(have) == norm(want), (
        f'yt-dlp instalat {have}, pinuit {want}: testele nu verifica ce se livreaza')


def test_the_pot_provider_versions_are_in_lockstep():
    """Serverul si pluginul trebuie sa fie aceeasi versiune.

    Pluginul (pip) vorbeste cu serverul (tag de git din Dockerfile) printr-un
    protocol care s-a schimbat intre versiuni majore. Doua pinuri in doua fisiere
    diferite driftează in silence si abia apoi apar 403-uri fara explicatie.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reqs = open(os.path.join(root, 'requirements.txt'), encoding='utf-8').read()
    docker = open(os.path.join(root, 'Dockerfile'), encoding='utf-8').read()

    plugin = re.search(r'^bgutil-ytdlp-pot-provider==(.+)$', reqs, re.M)
    server = re.search(r'^ARG BGUTIL_VERSION=(.+)$', docker, re.M)
    assert plugin and server, 'pinul bgutil a dispărut dintr-unul din fisiere'
    assert plugin.group(1).strip() == server.group(1).strip(), (
        f'plugin {plugin.group(1)} != server {server.group(1)}')


def test_the_healthcheck_path_is_one_the_server_answers():
    """railway.toml si bot.py trebuie sa cada de acord, altfel deploy-ul eșueaza."""
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    toml = open(os.path.join(root, 'railway.toml'), encoding='utf-8').read()
    bot_src = open(os.path.join(root, 'bot.py'), encoding='utf-8').read()

    m = re.search(r'healthcheckPath\s*=\s*"([^"]+)"', toml)
    assert m, 'healthcheckPath lipseste din railway.toml'
    path = m.group(1)
    assert f"'{path}'" in bot_src or f'"{path}"' in bot_src, (
        f'{path} nu apare in bot.py, deci handler-ul nu il accepta')


def test_the_slot_is_always_released_even_when_the_request_raises():
    """Un slot nereturnat inseamna un bot care tace la orice !play de acum inainte.

    Poarta e un semafor de 1: daca o excepție ar putea ocoli eliberarea,
    prima eroare ar bloca definitiv toate cererile catre YouTube.
    """
    async def main():
        for _ in range(3):
            try:
                async with ytdlp._slot():
                    raise RuntimeError('cererea a eșuat')
            except RuntimeError:
                pass
        # Daca semaforul nu s-a eliberat, aici s-ar aștepta la infinit.
        _, gate = ytdlp._primitives()
        await asyncio.wait_for(gate.acquire(), timeout=1)
        gate.release()

    saved_min = ytdlp.YT_REQUEST_MIN_INTERVAL_SEC
    saved_max = ytdlp.YT_REQUEST_MAX_INTERVAL_SEC
    ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = 0.0
    ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = 0.0
    ytdlp._NEXT_ALLOWED_AT = 0.0
    try:
        asyncio.run(main())
    except asyncio.TimeoutError:
        raise AssertionError('poarta a rămas inchisa dupa o cerere eșuata')
    finally:
        ytdlp.YT_REQUEST_MIN_INTERVAL_SEC = saved_min
        ytdlp.YT_REQUEST_MAX_INTERVAL_SEC = saved_max
        ytdlp._NEXT_ALLOWED_AT = 0.0


def test_no_ytdlp_call_uses_the_default_executor():
    """Pe pool-ul implicit ruleaza si FFmpegOpusAudio.probe al lui discord.py.

    asyncio.wait_for anuleaza aȘteptarea, nu thread-ul, deci cateva cereri
    expirate ar infometa fiecare alt run_in_executor din proces, inclusiv
    pornirea audio. Verificarea e pe TOT modulul, nu pe functiile de azi: un apel
    nou adaugat cu None ar trece altfel nedetectat.
    Comportamentul (executor propriu, serializare, distanta) e in
    tests/test_ytdlp_gate.py.
    """
    import ast

    # Pe AST, nu pe text: docstring-ul modulului citeaza chiar forma greșita
    # (`run_in_executor(None, ...)`) ca sa explice de ce e interzisa.
    tree = ast.parse(inspect.getsource(ytdlp))
    targets = [
        ast.unparse(node.args[0]) if node.args else 'LIPSA'
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, 'attr', None) == 'run_in_executor'
    ]
    assert targets, 'nu mai exista niciun apel in executor'
    assert all(t == '_EXECUTOR' for t in targets), targets
    assert 0 < ytdlp.MAX_WORKERS <= 8, ytdlp.MAX_WORKERS


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
        except Exception as e:
            # Nu doar AssertionError: un test care CRAPA (RuntimeError,
            # TypeError) opreste altfel fisierul si testele de dupa el nu mai
            # ruleaza deloc, fara sa apara nicaieri ca lipsesc.
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
