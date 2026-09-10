"""Curatarea titlului: o singura definitie, si una care chiar functioneaza.

Bug-ul care a motivat fisierul: `clean_search_title` conținea un octet 0x08
(BACKSPACE) acolo unde trebuia `\\b`, in interiorul unui literal raw. Regexul era
deci `<BACKSPACE>(official|video|...)<BACKSPACE>` si NU se potrivea NICIODATA cu
nimic — functia era complet inerta, iar fiecare interogare de autoplay pleca la
YouTube cu "Official Video" in ea. Un byte de control nu se vede nici la citirea
codului, nici in diff, nici in code review.

Si drift-ul: docstring-ul spunea "exista o singura data", dar supravietuiau doua
copii private, in youtube_api.py si autoplay.py, cu liste diferite de cuvinte —
deci aceeasi piesa era curatata diferit in functie de cine o cerea.

    python tests/test_title_cleaning.py
"""
import glob
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music.utils import clean_search_title

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_tags_are_actually_stripped():
    """Verificarea pe care regexul inert o trecea fara sa faca nimic."""
    cases = {
        'Los Del Rio - Macarena (Official Video)': 'Los Del Rio - Macarena',
        'Delia - Ipotecat [Official Audio]': 'Delia - Ipotecat',
        'Ceva Official Video Aici': 'Ceva Aici',
        'Piesa HD 4K': 'Piesa',
    }
    wrong = {t: clean_search_title(t) for t, want in cases.items()
             if clean_search_title(t) != want}
    assert not wrong, f'curatare greșita: {wrong}'


def test_word_boundaries_are_respected():
    """Fara \\b, 'clip' din 'Eclipse' si 'cover' din 'Discover' erau mancate."""
    for title in ('Total Eclipse of the Heart', 'Discover Weekly',
                  'Olive Tree', 'Believe'):
        assert clean_search_title(title) == title, (
            f'{title!r} -> {clean_search_title(title)!r}')


def test_a_title_made_only_of_tags_falls_back_to_the_original():
    """Altfel interogarea ar fi goala si search-ul ar intoarce orice."""
    assert clean_search_title('Official Video') == 'Official Video'
    assert clean_search_title('') == ''
    assert clean_search_title(None) == ''


def test_there_is_exactly_one_definition():
    offenders = []
    for path in sorted(glob.glob(str(ROOT / 'music' / '*.py'))
                       + [str(ROOT / 'bot.py')]):
        src = pathlib.Path(path).read_text(encoding='utf-8')
        name = os.path.basename(path)
        if 'def clean_search_title' in src and name != 'utils.py':
            offenders.append(f'{name}: a doua definitie a functiei partajate')
        for private in ('def _clean_search_title', 'def _clean_title'):
            if private in src:
                offenders.append(f'{name}: copie privata {private}')
    assert not offenders, 'curatare duplicata:\n  ' + '\n  '.join(offenders)


def test_no_source_file_contains_control_characters():
    """Clasa de bug, nu instanta.

    Un octet de control intr-un literal raw e invizibil in cod, in diff si in
    review, si transforma silentios un regex intr-unul care nu se potriveste
    niciodata. Singurele caractere sub 0x20 acceptate sunt tab, LF si CR.
    """
    allowed = {9, 10, 13}
    offenders = []
    for path in sorted(glob.glob(str(ROOT / 'music' / '*.py'))
                       + glob.glob(str(ROOT / 'tests' / '*.py'))
                       + [str(ROOT / 'bot.py'), str(ROOT / 'start.sh')]):
        data = pathlib.Path(path).read_bytes()
        for index, byte in enumerate(data):
            if byte < 32 and byte not in allowed:
                line = data[:index].count(b'\n') + 1
                offenders.append(f'{os.path.relpath(path, ROOT)}:{line} '
                                 f'octet {hex(byte)}')
    assert not offenders, 'caractere de control in sursa:\n  ' + '\n  '.join(offenders)


def test_every_regex_in_the_package_compiles_and_is_not_inert():
    """Un regex care incepe cu un octet de control se compileaza, dar nu prinde nimic."""
    import re

    import music.autoplay
    import music.commands
    import music.config
    import music.errors
    import music.utils
    import music.youtube_api

    inert = []
    for module in (music.utils, music.youtube_api, music.autoplay,
                   music.commands, music.config, music.errors):
        src = pathlib.Path(module.__file__).read_text(encoding='utf-8')
        for match in re.finditer(r"re\.(?:sub|search|match|compile)\(\s*r?'([^']*)'", src):
            pattern = match.group(1)
            if any(ord(ch) < 32 and ch not in '\t\n\r' for ch in pattern):
                inert.append(f'{os.path.basename(module.__file__)}: {pattern!r}')
    assert not inert, 'regexuri cu caractere de control:\n  ' + '\n  '.join(inert)


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
