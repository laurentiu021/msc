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
