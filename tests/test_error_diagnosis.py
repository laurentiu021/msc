"""diagnose_error trebuie sa nimereasca tipul corect pe mesajele REALE ale yt-dlp.

Doua clase de bug pazite aici:

1. Ordinea ramurilor. "Sign in to confirm" apare si la age gate, iar
   "is not available" apare si in "Requested format is not available", deci
   ramurile largi umbreau pe cele specifice si sfatul afisat era greșit.
2. Potriviri prea largi. Cuvantul "cookies" apare in propriile noastre mesaje
   si in textul de ajutor al yt-dlp; "opus" apare in selectorul de format
   `bestaudio[acodec=opus]`; "403" apare si in erori Discord. Toate produceau
   diagnostice false.

Mesajele de mai jos sunt copiate din logurile de producție ale botului.

Ruleaza fara pytest si fara retea:  python tests/test_error_diagnosis.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music.errors import diagnose_error

REAL_MESSAGES = {
    # din loguri, 2026-09-04 si 2026-09-09
    "ERROR: [youtube] wQwRBWI-X9Y: Sign in to confirm you're not a bot. Use "
    "--cookies-from-browser or --cookies for the authentication.": 'cookies',
    "WARNING: [youtube] The provided YouTube account cookies are no longer valid. "
    "They have likely been rotated in the browser as a security measure.": 'cookies',
    "WARNING: [youtube] jyd81XVz1ZE: Unable to download webpage: HTTP Error 429: "
    "Too Many Requests": 'ratelimit',
    "ERROR: unable to download video data: HTTP Error 403: Forbidden": 'po_token',
    "WARNING: [youtube] Unable to fetch GVS PO Token for web client: Missing "
    "required Visitor Data": 'po_token',
    "ERROR: [youtube] kk4uddaHdDE: The uploader has not made this video available "
    "in your country": 'unavailable',
    "ERROR: [youtube] jyd81XVz1ZE: Requested format is not available. Use "
    "--list-formats to see them": 'format',
    "ERROR: [youtube] x: Sign in to confirm your age. This video may be "
    "inappropriate for some users.": 'age_gate',
    "<urlopen error [Errno -3] Temporary failure in name resolution>": 'network',
    "ffprobe/ffmpeg not found. Please install or provide the path": 'ffmpeg',
    "ERROR: [youtube] x: Failed to extract any player response": 'unknown',
}

# Mesaje care NU trebuie sa fie diagnosticate greșit.
MUST_NOT_MATCH = [
    # selectorul de format contine "opus": nu e o problema de ffmpeg
    ("Download esuat (cookies=False, fmt='bestaudio[acodec=opus]/bestaudio')", 'ffmpeg'),
    # propriul nostru sfat contine cuvantul "cookies": nu e o eroare de cookies
    ("Reinnoieste cookies-urile pe Railway", 'cookies'),
    # un 403 de la Discord nu e o problema de PO Token
    ("discord.errors.Forbidden: 403 Forbidden (error code: 50013): "
     "Missing Permissions", 'po_token'),
]


def test_real_messages_map_to_the_right_type():
    wrong = []
    for message, expected in REAL_MESSAGES.items():
        got, advice = diagnose_error(message)
        if got != expected:
            wrong.append(f'{got} != {expected} pentru {message[:70]!r}')
        assert advice, 'mesajul pentru Discord nu poate fi gol'
    assert not wrong, 'diagnoza greșita:\n  ' + '\n  '.join(wrong)


def test_broad_substrings_do_not_produce_false_positives():
    wrong = []
    for message, forbidden in MUST_NOT_MATCH:
        got, _ = diagnose_error(message)
        if got == forbidden:
            wrong.append(f'{message[:60]!r} a fost diagnosticat greșit ca {forbidden}')
    assert not wrong, 'potriviri prea largi:\n  ' + '\n  '.join(wrong)


def test_age_gate_is_not_shadowed_by_the_cookie_branch():
    got, _ = diagnose_error("Sign in to confirm your age")
    assert got == 'age_gate', f'age gate raportat ca {got}'


def test_format_error_is_not_shadowed_by_unavailable():
    got, _ = diagnose_error("Requested format is not available")
    assert got == 'format', f'eroare de format raportata ca {got}'


def test_unknown_keeps_the_original_text_for_the_user():
    got, advice = diagnose_error("ceva ce nu am mai vazut niciodata")
    assert got == 'unknown'
    assert 'ceva ce nu am mai vazut' in advice


def test_type_keys_do_not_round_trip_into_themselves():
    """Cheia de tip nu e un mesaj de eroare.

    Codul trimitea cheia ('cookies', 'ratelimit', ...) inapoi in diagnose_error
    pentru raportul intrerupatorului, si asta e exact de ce raporta mereu
    'unknown'. Testul documenteaza de ce trebuie pasat textul brut.
    """
    # Cheile care conteaza operational: daca acestea nu se re-mapeaza, raportul
    # intrerupatorului nu poate spune niciodata "reinnoieste cookies-urile".
    # ('ffmpeg' se auto-mapeaza accidental, fiind si substring al mesajului real.)
    for key in ('cookies', 'age_gate', 'ratelimit', 'unavailable'):
        got, _ = diagnose_error(key)
        assert got == 'unknown', (
            f'cheia {key!r} se auto-mapeaza pe {got!r}; codul trebuie sa '
            f'paseze textul brut al erorii, nu cheia')


def test_the_timeout_type_is_diagnosed_as_a_timeout():
    """Plafoanele de 90s/240s exista ca sa faca vizibil un blocaj.

    Mesajul lor e in romana ("a depasit 90s"), iar ramura de rețea cauta
    "timed out"/"timeout" — deci exact eșecul pe care aceste plafoane il
    raporteaza era singurul despre care utilizatorul nu putea fi informat.
    """
    from music.errors import YtdlpTimeout

    exc = YtdlpTimeout('search_mweb+web_safari', 90)
    assert diagnose_error(exc)[0] == 'timeout'
    # Si prin textul salvat in state.last_raw_error, unde tipul se pierde.
    assert diagnose_error(str(exc))[0] == 'timeout'
    assert diagnose_error("descarcarea a depasit 240s")[0] == 'timeout'
    assert exc.stage == 'search_mweb+web_safari' and exc.budget == 90


def test_ytdlp_raises_the_typed_timeout_not_a_bare_one():
    """Producatorul si diagnoza trebuie sa rămâna legate prin TIP, nu prin text."""
    import ast
    import inspect

    from music import ytdlp
    from music.errors import YtdlpTimeout

    assert ytdlp.YtdlpTimeout is YtdlpTimeout, 'ytdlp foloseste alt tip'
    raised = [
        getattr(n.exc.func, 'id', '')
        for n in ast.walk(ast.parse(inspect.getsource(ytdlp.extract)))
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
    ]
    assert raised == ['YtdlpTimeout'], raised


# Excepțiile de mai jos NU trec prin diagnose_error: au fiecare propriul
# handler in process_play, care nu diagnosticheaza nimic.
_SELF_HANDLED_TYPES = {'PlaybackInterrupted', 'TrackRejected'}
# ...si trigger_radio isi prinde propria eroare, cu propriul mesaj.
_SELF_HANDLED_FUNCS = {'trigger_radio'}


def _own_error_messages():
    """Mesajele literale pe care player.py le ridica si care AJUNG la diagnoza."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((root / 'music' / 'player.py').read_text(encoding='utf-8'))
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _SELF_HANDLED_FUNCS:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            name = getattr(node.exc.func, 'id', '')
            if name in _SELF_HANDLED_TYPES or not node.exc.args:
                continue
            arg = node.exc.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((fn.name, node.lineno, arg.value))
    return found


def test_our_own_error_messages_are_all_diagnosable():
    """Scanare pe AST, nu o lista scrisa de mana.

    Toate mesajele proprii ieseau 'unknown', deci utilizatorul primea propriul
    nostru text citat inapoi la el si niciun sfat. Testul se uita in sursa, deci
    un mesaj NOU adaugat maine trebuie sa aiba si el o ramura.
    """
    messages = _own_error_messages()
    assert messages, 'scanarea nu a gasit nimic — s-a schimbat structura?'
    undiagnosed = [(f, ln, m) for f, ln, m in messages
                   if diagnose_error(m)[0] == 'unknown']
    assert not undiagnosed, 'mesaje proprii fara ramura de diagnoza:\n  ' + \
        '\n  '.join(f'player.py:{ln} in {f}: {m!r}' for f, ln, m in undiagnosed)


def test_log_tokens_quoted_to_the_user_actually_exist():
    """Un sfat care trimite la un text inexistent e mai rau decat niciun sfat.

    Mesajul de PO Token spunea "In loguri cauta `[STARTUP] PO Token server
    running`" — un text care nu exista nicaieri in proiect. Verificarea e
    mecanica, deci nu se mai poate intampla.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    source = root / 'music' / 'errors.py'
    advice = source.read_text(encoding='utf-8')
    # errors.py NU face parte din caut: altfel orice token s-ar gasi in fisierul
    # care il citeaza si verificarea ar trece mereu.
    haystack = '\n'.join(
        p.read_text(encoding='utf-8')
        for p in [*sorted(root.glob('*.py')), *sorted((root / 'music').glob('*.py')),
                  root / 'start.sh', root / 'Dockerfile']
        if p.exists() and p != source)

    missing = []
    for token in re.findall(r'`([^`\n]+)`', advice):
        # Comenzile de Discord (`!play`) si flag-urile yt-dlp (`--cookies`) sunt
        # ale altcuiva; `{short}` e interpolare, nu un token de căutat.
        # Verificam doar ce pretindem ca producem noi.
        if token.startswith(('!', '-')) or '{' in token:
            continue
        if token not in haystack:
            missing.append(token)
    assert not missing, f'citate in errors.py dar inexistente in cod: {missing}'


def _capture_ydl_log(messages):
    """Ruleaza logger-ul yt-dlp cu un handler care retine (nivel, text)."""
    import logging

    from music.config import YDL_LOGGER, log as music_log

    records = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))

    sink = _Sink()
    saved_level = music_log.level
    music_log.addHandler(sink)
    music_log.setLevel(logging.DEBUG)
    try:
        for msg in messages:
            YDL_LOGGER.debug(msg)
    finally:
        music_log.removeHandler(sink)
        music_log.setLevel(saved_level)
    return records


def test_filter_rejections_are_promoted_to_visible_lines():
    """Motivul unei piese sarite trebuie sa ajunga in log, nu in vid.

    yt-dlp raporteaza respingerea prin `to_screen`, care la quiet=True nu scrie
    nimic si nu ridica excepție: piesa dispărea complet, iar utilizatorului i se
    arata eroarea unei piese anterioare. Cu logger atasat, `to_screen` intra pe
    `debug`, deci exact liniile care explica o decizie se ridica la INFO.
    """
    import logging

    # Textele sunt cele REALE ale yt-dlp-ului instalat, verificate cu
    # match_filter_func(...) si cu downloader/http.py, nu scrise din memorie.
    promoted = [
        '[download] Radio non-stop does not pass filter '
        '(!is_live & !live_from_start & duration < 660), skipping ..',
        '\r[download] File is larger than max-filesize '
        '(150000000 bytes > 104857600 bytes). Aborting.',
        "[youtube] vid123: Sign in to confirm you're not a bot",
    ]
    records = _capture_ydl_log(promoted)
    assert len(records) == len(promoted)
    for level, text in records:
        assert level == logging.INFO, f'rămâne invizibila in producție: {text}'
    # Textul trebuie sa rămâna citibil. lstrip('[debug] ') primeste un SET de
    # caractere, deci tăia si din "[download] File is larger" pana la primul
    # caracter din afara setului: "ownload] File is larger".
    for (_, text), original in zip(records, promoted):
        payload = original.strip('\r\n').removeprefix('[debug] ')
        assert text.endswith(payload), f'mesaj mutilat: {text!r}'
    # \r ar tăia prefixul inregistrarii in vizualizatorul de loguri.
    assert not any('\r' in text for _, text in records), records


def test_the_debug_prefix_is_removed_without_eating_the_message():
    records = _capture_ydl_log(['[debug] [youtube] downloading player'])
    assert records[0][1] == 'yt-dlp: [youtube] downloading player', records


def test_routine_chatter_stays_at_debug():
    """Altfel logul de producție devine ilizibil si nu-l mai citeste nimeni."""
    import logging

    records = _capture_ydl_log([
        '[debug] Loading youtube-nsig player from cache',
        '[debug] [youtube] Extracting URL: https://www.youtube.com/watch?v=x',
    ])
    assert records and all(level == logging.DEBUG for level, _ in records), records


def test_both_opts_sets_carry_the_logger():
    """Fara logger, yt-dlp tace la quiet=True — nu e o optiune de estetica."""
    from music.config import make_download_opts, make_search_opts

    for name, opts in (('search', make_search_opts()),
                       ('download', make_download_opts())):
        logger = opts.get('logger')
        assert logger is not None, f'{name} nu are logger'
        for level in ('debug', 'info', 'warning', 'error'):
            assert callable(getattr(logger, level, None)), \
                f'{name}: logger fara {level}(), yt-dlp va crapa'


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
