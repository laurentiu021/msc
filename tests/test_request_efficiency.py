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

from music import autoplay, config, player, resolve, state as state_mod, ytdlp
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
    """Cu ytsearch (un singur rezultat) filtrul is_clean nu are din ce alege.

    Numarul sta acum in prefixul EXPLICIT, nu in `default_search`: acela, combinat cu
    `extract_flat=True`, dezactiva cautarea complet si in tacere. Vezi
    config.SEARCH_PREFIX si tests/test_resolve.py.
    """
    prefix = config.SEARCH_PREFIX
    assert prefix.startswith('ytsearch') and prefix.endswith(':'), prefix
    assert config.SEARCH_RESULTS >= 3, config.SEARCH_RESULTS
    assert prefix == f'ytsearch{config.SEARCH_RESULTS}:', prefix


def test_text_search_is_flat_then_one_full_extraction():
    """ytsearchN fara extract_flat extrage COMPLET toate rezultatele.

    Masurat: cu opts-urile reale ale repo-ului, ytsearch5 producea cinci
    extractii complete, adica ~20 de cereri catre YouTube pentru un !play, toate
    in acelasi slot de throttle si acelasi buget de 90s.
    """
    src = inspect.getsource(resolve.search_to_url)
    assert 'extract_flat=True' in src, 'cautarea de text nu mai e flat'
    # si nu mai luam orbeste primul rezultat cand niciunul nu trece filtrul
    assert 'is_clean' in src
    code = ''.join(line.split('#')[0] for line in src.splitlines())
    assert 'entries[0]' not in code, 'inca ia orbeste primul rezultat'


def test_format_list_is_short_and_without_duplicates():
    """Pe valoarea reala, nu pe textul sursei.

    Varianta veche despica `inspect.getsource` pe 'formats_to_try = [' si pe
    ']' — deci in clipa in care lista a devenit una de tupluri, testul a
    continuat sa treaca masurand fragmente de text fara sens.
    """
    attempts = resolve.DOWNLOAD_ATTEMPTS
    formats = [fmt for fmt, _ in attempts]
    assert 1 <= len(attempts) <= 3, f'prea multe incercari: {len(attempts)}'
    assert len(set(formats)) == len(formats), 'formate duplicate'
    for fmt, cap in attempts:
        assert isinstance(cap, int) and cap > 0, f'{fmt}: plafon invalid {cap}'


def test_the_hls_fallback_asks_for_audio_not_video():
    """`best[protocol=m3u8]` e o redare muxata video+audio.

    `-vn` arunca imaginea abia DUPA ce a ajuns pe disc, deci un bot audio
    descarca zeci de MB de video pe un IP care ne limiteaza deja.
    """
    fallbacks = [(fmt, cap) for fmt, cap in resolve.DOWNLOAD_ATTEMPTS
                 if 'm3u8' in fmt]
    assert fallbacks, 'nu mai exista nicio incercare HLS'
    for fmt, cap in fallbacks:
        assert fmt.startswith('bestaudio'), f'HLS cere video: {fmt}'
        assert cap < config.MAX_DOWNLOAD_BYTES, (
            f'plafonul HLS ({cap}) nu e mai strans decat cel normal')


def test_the_hls_attempt_can_never_ask_for_muxed_video():
    """Plafonul de octeti NU se aplica pe HLS, deci selectorul e singura aparare.

    Verificat pe yt-dlp 2026.8.19: `max_filesize` e citit doar de HttpFD si CurlFD;
    toate formatele HLS de YouTube sunt `m3u8_native` (HlsFD), iar nici
    downloader/hls.py nici downloader/fragment.py nu il pomenesc. Un
    `best[protocol^=m3u8]` e o redare muxata video+audio, iar `-vn` arunca imaginea
    abia DUPA ce a ajuns pe disc: transfer de video nemarginit pe un bot audio.
    """
    import inspect

    from yt_dlp.downloader import get_suitable_downloader

    fd = get_suitable_downloader({'protocol': 'm3u8_native', 'url': 'http://x'},
                                 params={})
    module_src = inspect.getsource(sys.modules[fd.__module__])
    assert 'max_filesize' not in module_src, (
        f'{fd.__name__} pare sa respecte acum max_filesize — daca da, plafonul HLS '
        f'poate redeveni aparare si comentariul din resolve.py trebuie actualizat')

    for fmt, _cap in resolve.DOWNLOAD_ATTEMPTS:
        if 'm3u8' not in fmt:
            continue
        for alternative in fmt.split('/'):
            assert alternative.startswith('bestaudio'), (
                f'alternativa HLS cere video, fara niciun plafon: {alternative}')


def test_a_rate_limit_does_not_trigger_a_second_format_attempt():
    """Acelasi 429 nu devine alt raspuns cu alt selector de format."""
    for message, expected in (
            ('HTTP Error 429: Too Many Requests', False),
            ("Sign in to confirm you're not a bot. Use --cookies", False),
            ('This video is not available', False),
            ('Requested format is not available', True),
            ('ceva ce nu am mai vazut', True),
    ):
        got = resolve.worth_another_format(message)
        assert got is expected, f'{message[:40]!r}: {got} != {expected}'

    # Fara nicio eroare raportata = respins de filtru, nu problema de format.
    assert resolve.worth_another_format(None) is False


def test_ffmpeg_options_do_not_override_the_probed_bitrate():
    """discord.py emite deja -ar/-ac/-b:a, iar sirul nostru se adauga DUPA.

    Verificat in discord.py 2.7.1 instalat: FFmpegOpusAudio.__init__ pune
    `-ar 48000 -ac 2 -b:a {bitrate}k` inainte de `options`, iar `from_probe`
    trece bitrate-ul masurat din fisier — pe care un `-b:a 128k` al nostru il
    suprascria.
    """
    options = config.FFMPEG_OPTS['options'].split()
    assert options == ['-vn'], options


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


def test_the_js_runtimes_are_pinned_like_everything_else():
    """Deno si Node, pinuite ca yt-dlp si bgutil — nu "ce e mai nou in ziua build-ului".

    Deno rezolva provocarile JS ale YouTube-ului, iar daca se strica formatele opus
    dispar tacit; Node ruleaza serverul de PO Token, fara de care fiecare descarcare
    primeste 403. Se instalau totusi prin scripturi descarcate si rulate direct in
    shell (`deno.land/install.sh | sh`, `setup_24.x | bash`), adica ultima versiune
    de la momentul build-ului: un rebuild fara nicio schimbare in repo le putea
    schimba pe amandoua, si nimic nu spunea ca s-a intamplat. In plus, Node-ul care
    construia serverul (node:24-slim) si cel care il rula (NodeSource) erau doua
    versiuni care plutesc independent.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docker = open(os.path.join(root, 'Dockerfile'), encoding='utf-8').read()
    code = '\n'.join(line for line in docker.splitlines()
                     if not line.lstrip().startswith('#'))
    piped = re.findall(r'^.*\|[^|&;\n]*\b(?:ba)?sh\b.*$', code, re.M)
    assert not piped, f'script descarcat si rulat direct in shell: {piped}'

    for arg in ('NODE_VERSION', 'DENO_VERSION'):
        assert re.search(rf'^ARG {arg}=\d+\.\d+\.\d+$', docker, re.M), (
            f'{arg} nu e pinuit la o versiune exacta')
    assert re.search(r'^FROM node:\$\{NODE_VERSION\}-slim AS pot-builder$',
                     docker, re.M), 'serverul de PO Token nu se construieste cu Node-ul pinuit'
    assert re.search(r'^FROM denoland/deno:bin-\$\{DENO_VERSION\} AS deno$',
                     docker, re.M), 'Deno nu vine din imaginea pinuita'
    # Acelasi Node construieste si ruleaza serverul.
    assert 'COPY --from=pot-builder /usr/local/bin/node /usr/local/bin/node' in docker
    assert 'COPY --from=deno /deno /usr/local/bin/deno' in docker

    # Iar CI-ul verifica in imaginea construita ca asta s-a si livrat.
    ci = open(os.path.join(root, '.github', 'workflows', 'ci.yml'),
              encoding='utf-8').read()
    for arg in ('NODE_VERSION', 'DENO_VERSION'):
        assert f'ARG {arg}=' in ci, f'CI nu compara {arg} cu imaginea construita'


def test_the_image_can_never_carry_a_google_session():
    """`.dockerignore` exclude tot ce `.gitignore` numeste drept secret.

    Doua liste pentru acelasi lucru driftează: `.gitignore` prindea `cookies*` — deci
    si `cookies.txt.good` (ultima copie buna) si `cookies.txt.tmp` (scrierea
    atomica) — iar `.dockerignore` doar `cookies.txt`. Un `docker build` dintr-un
    checkout local punea deci o sesiune Google in straturile imaginii. La fel
    `.livecheck/`: e COOKIE_DIR-ul harness-ului live, deci tine un jar oricand
    acesta ruleaza cu cookies.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def patterns(name):
        with open(os.path.join(root, name), encoding='utf-8') as fh:
            return [line.strip() for line in fh]

    gitignore = patterns('.gitignore')
    start = next(i for i, line in enumerate(gitignore)
                 if line.startswith('# --- secrete'))
    secrets = []
    for line in gitignore[start + 1:]:
        if line.startswith('# ---'):
            break
        if line and not line.startswith('#'):
            secrets.append(line)
    assert secrets, 'sectiunea de secrete a dispărut din .gitignore'

    docker = set(patterns('.dockerignore'))
    missing = [p for p in secrets + ['.livecheck/'] if p not in docker]
    assert not missing, f'.dockerignore lasa in imagine: {missing}'


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
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    # run_in_executor(None, ...) e forma interzisa; cu _EXECUTOR ar fi in regula.
    targets = [ast.unparse(node.args[0]) if node.args else 'LIPSA'
               for node in calls
               if getattr(node.func, 'attr', None) == 'run_in_executor']
    assert all(t == '_EXECUTOR' for t in targets), targets

    # asyncio.to_thread e tot pool-ul implicit, doar cu alt nume.
    assert not [n for n in calls if getattr(n.func, 'attr', None) == 'to_thread'], \
        'asyncio.to_thread foloseste tot executorul implicit'

    # Si trebuie sa existe o cale reala de trimitere a muncii, altfel testul ar
    # trece vesel pe un modul din care s-a sters tot.
    dispatch = [n for n in calls
                if getattr(n.func, 'attr', None) == 'submit'
                and ast.unparse(n.func.value) == '_EXECUTOR']
    assert dispatch or targets, 'nu mai exista niciun apel in executor'
    assert 0 < ytdlp.MAX_WORKERS <= 8, ytdlp.MAX_WORKERS


def test_autoplay_seeds_from_the_current_track():
    """state.history[0] e cea mai VECHE intrare, deci radio-ul rămânea pinuit.

    Verificarea veche se uita la textul sursei (`'state.last_url or' in code`),
    adica trecea si daca comportamentul era inversat. Asta conduce functia reala si
    verifica ce ID primesc strategiile.
    """
    import asyncio

    seen = []

    async def record(state, bot_loop, origin_id, skip_ids, needed,
                     artist_counts=None):
        seen.append(origin_id)
        return 0

    st = GuildState()
    st.history = [
        {'url': 'https://www.youtube.com/watch?v=CEA_VECHE', 'title': 'A'},
        {'url': 'https://www.youtube.com/watch?v=MIJLOC', 'title': 'B'},
        {'url': 'https://www.youtube.com/watch?v=PENULTIMA', 'title': 'C'},
    ]
    st.last_url = 'https://www.youtube.com/watch?v=CURENTA'
    st.last_title = 'Artist - Piesa'

    saved = (autoplay._try_ytdlp_mix, autoplay._try_api_related,
             autoplay._try_api_search, autoplay._try_ytdlp_search)
    autoplay._try_ytdlp_mix = record
    autoplay._try_api_related = record
    autoplay._try_api_search = lambda *a, **k: _zero()
    autoplay._try_ytdlp_search = lambda *a, **k: _zero()
    try:
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=5))
    finally:
        (autoplay._try_ytdlp_mix, autoplay._try_api_related,
         autoplay._try_api_search, autoplay._try_ytdlp_search) = saved

    assert seen, 'nicio strategie nu a fost chemata'
    assert set(seen) == {'CURENTA'}, (
        f'radio-ul se seamana din alta piesa decat cea curenta: {seen}')


async def _zero():
    return 0


def test_autoplay_falls_back_to_the_newest_history_entry():
    """Fara `last_url` (dupa un restart de sesiune) seed-ul e ultima piesa, nu prima."""
    import asyncio

    seen = []

    async def record(state, bot_loop, origin_id, skip_ids, needed,
                     artist_counts=None):
        seen.append(origin_id)
        return 0

    st = GuildState()
    st.history = [
        {'url': 'https://www.youtube.com/watch?v=CEA_VECHE', 'title': 'A'},
        {'url': 'https://www.youtube.com/watch?v=CEA_NOUA', 'title': 'B'},
    ]
    st.last_url = None

    saved = (autoplay._try_ytdlp_mix, autoplay._try_api_related,
             autoplay._try_api_search, autoplay._try_ytdlp_search)
    autoplay._try_ytdlp_mix = record
    autoplay._try_api_related = record
    autoplay._try_api_search = lambda *a, **k: _zero()
    autoplay._try_ytdlp_search = lambda *a, **k: _zero()
    try:
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=5))
    finally:
        (autoplay._try_ytdlp_mix, autoplay._try_api_related,
         autoplay._try_api_search, autoplay._try_ytdlp_search) = saved

    assert set(seen) == {'CEA_NOUA'}, seen


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
    """Prag egal cu target-ul insemna un refill la FIECARE piesa.

    Vechea verificare citea un sir din sursa si valoarea IMPLICITA a parametrului
    `target` — niciuna nu spune ce se transmite la apel, nici daca refill-ul se
    intampla. Asta conduce `_play_next_async` si reține argumentele reale.
    """
    import asyncio

    calls = []

    async def record_prefill(state, bot_loop, target=12):
        calls.append((len(state.queue), target))

    class _Ctx:
        guild = type('G', (), {'id': 313})()
        voice_client = type('V', (), {'is_connected': lambda self: True})()

    st = GuildState()
    st.autoplay = True
    st.last_url = 'https://www.youtube.com/watch?v=x'
    st.last_title = 'Artist - Piesa'
    # Doua piese in coada: una se scoate ca sa fie redata, deci refill-ul vede una.
    st.queue = [{'query': 'a', 'title': 'A'}, {'query': 'b', 'title': 'B'}]
    state_mod.guild_states[313] = st

    saved = (player.prefill_autoplay_queue, player.process_play,
             player.cancel_timeout, player.start_timeout,
             player.update_player_ui, player._loop)
    player.prefill_autoplay_queue = record_prefill

    async def noop_play(ctx, query, is_radio=False):
        return None

    async def noop_ui(ctx, send_new=False):
        return None

    player.process_play = noop_play
    player.cancel_timeout = lambda *a, **k: None
    player.start_timeout = lambda *a, **k: None
    player.update_player_ui = noop_ui
    player._loop = None
    try:
        asyncio.run(player._play_next_async(_Ctx()))
    finally:
        (player.prefill_autoplay_queue, player.process_play,
         player.cancel_timeout, player.start_timeout,
         player.update_player_ui, player._loop) = saved

    assert calls, 'nu s-a facut niciun refill dupa ce coada a scazut sub prag'
    queue_len, target = calls[0]
    assert target > queue_len + 1, (
        f'target={target} nu e peste pragul care l-a declanșat ({queue_len}): '
        f'un refill la fiecare piesa')


# --- doua prefill-uri in paralel -----------------------------------------------

def test_two_prefills_at_once_do_not_double_fill_or_double_pay():
    """Tick-ul de inactivitate cheama prefill-ul fara sa ia `is_loading`.

    Decizia lui doar CITESTE steagul, deci un buton Autoplay apasat in fereastra de
    cateva secunde a unui prefill trecea de propria verificare si pornea un al doilea.
    Amandoua calculau `needed` din aceeasi coada goala: coada ajungea la dublul
    țintei, iar extractiile si cota de API se plateau de doua ori — pe un IP care
    oricum ne limiteaza.
    """
    st = GuildState()
    st.last_url = 'https://www.youtube.com/watch?v=seed00'
    st.last_title = 'Artist - Piesa'
    strategies = []

    async def fake_mix(state, loop, origin_id, skip_ids, needed, artist_counts=None):
        strategies.append(needed)
        await asyncio.sleep(0.05)          # o cerere de retea dureaza
        added = 0
        for n in range(needed):
            if autoplay._add_to_queue(state, f'vid{len(state.queue)}{n}',
                                      f'Cineva{n} - Piesa{n}', skip_ids, artist_counts):
                added += 1
        return added

    saved = autoplay._try_ytdlp_mix
    autoplay._try_ytdlp_mix = fake_mix

    async def both():
        await asyncio.gather(
            autoplay.prefill_autoplay_queue(st, None, target=6),
            autoplay.prefill_autoplay_queue(st, None, target=6))

    try:
        asyncio.run(both())
    finally:
        autoplay._try_ytdlp_mix = saved

    assert len(st.queue) == 6, f'coada a depasit ținta: {len(st.queue)}'
    assert len(strategies) == 1, (
        f'am plătit strategiile de {len(strategies)} ori pentru aceeasi coada')


def test_a_second_prefill_after_the_first_still_tops_up():
    """Serializarea nu are voie sa devina "unul singur, si restul degeaba"."""
    st = GuildState()
    st.last_url = 'https://www.youtube.com/watch?v=seed00'
    st.last_title = 'Artist - Piesa'

    async def fake_mix(state, loop, origin_id, skip_ids, needed, artist_counts=None):
        added = 0
        for n in range(needed):
            if autoplay._add_to_queue(state, f'v{len(state.queue)}x{n}',
                                      f'Altul{n} - Piesa{n}', skip_ids, artist_counts):
                added += 1
        return added

    saved = autoplay._try_ytdlp_mix
    autoplay._try_ytdlp_mix = fake_mix
    try:
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=3))
        assert len(st.queue) == 3, st.queue
        st.queue.pop(0)
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=3))
    finally:
        autoplay._try_ytdlp_mix = saved
    assert len(st.queue) == 3, f'nu a completat coada scazuta: {len(st.queue)}'


# --- plafonul de diversitate trebuie sa țina si intre prefill-uri ---------------

def test_the_same_artist_cap_survives_a_refill():
    """`artist_key` cade pe CANAL cand titlul nu are separator.

    Piesele din coada nu purtau canalul, deci prima umplere numara corect (are
    canalul la indemana), iar urmatoarea recitește coada si gaseste doar titlul:
    cheia iese goala, piesele nu se mai numara, si acelasi artist putea aduna 4+
    piese. Exact ce plafonul exista sa impiedice — un radio care da aceeasi voce la
    infinit. Titlurile de mai jos sunt un singur cuvant, ca la manele.

    Toate cele patru strategii sunt inlocuite, nu doar Mix-ul: altfel strategia 4
    chiar ar cere o cautare pe YouTube, iar rezultatul testului ar depinde de ce
    intoarce internetul in ziua aceea.
    """
    st = GuildState()
    st.last_url = 'https://www.youtube.com/watch?v=seed00'
    st.last_title = 'Cineva - Ceva'
    batch = 0

    async def fake_mix(state, loop, origin_id, skip_ids, needed, artist_counts=None):
        nonlocal batch
        batch += 1
        added = 0
        for n in range(needed):
            if autoplay._add_to_queue(state, f'b{batch}n{n}', f'Meneaito{batch}{n}',
                                      skip_ids, artist_counts,
                                      channel='Tzanca Uraganu'):
                added += 1
        return added

    async def nothing(*a, **k):
        return 0

    saved = (autoplay._try_ytdlp_mix, autoplay._try_api_related,
             autoplay._try_api_search, autoplay._try_ytdlp_search)
    autoplay._try_ytdlp_mix = fake_mix
    autoplay._try_api_related = nothing
    autoplay._try_api_search = nothing
    autoplay._try_ytdlp_search = nothing
    try:
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=4))
        first = len(st.queue)
        # Coada scade (s-a ascultat una), deci urmeaza un refill: el recitește coada.
        st.queue.pop(0)
        asyncio.run(autoplay.prefill_autoplay_queue(st, None, target=4))
    finally:
        (autoplay._try_ytdlp_mix, autoplay._try_api_related,
         autoplay._try_api_search, autoplay._try_ytdlp_search) = saved

    from music.autoplay import MAX_SAME_ARTIST
    # Numarate dupa TITLU, nu dupa cheia calculata din canal: altfel verificarea ar
    # depinde de chiar campul pe care il testeaza, iar scoaterea canalului ar face-o
    # sa treaca din oficiu (cheia iese goala pentru tot). Toate piesele produse de
    # fake sunt ale aceluiasi artist, deci titlul e suficient.
    same = [q for q in st.queue if str(q.get('title', '')).startswith('Meneaito')]
    assert first == MAX_SAME_ARTIST, (
        f'prima umplere nu a respectat plafonul: {first}')
    assert len(same) <= MAX_SAME_ARTIST, (
        f'{len(same)} piese de la acelasi artist dupa refill (plafon '
        f'{MAX_SAME_ARTIST}): {[q["title"] for q in st.queue]}')


def test_a_queued_track_remembers_its_channel():
    """Fara canal in element, verificarea de mai sus nu are de unde sa il afle."""
    st = GuildState()
    autoplay._add_to_queue(st, 'vid1', 'Meneaito', set(), {}, channel='Tzanca Uraganu')
    assert st.queue[0].get('channel') == 'Tzanca Uraganu', st.queue[0]


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
