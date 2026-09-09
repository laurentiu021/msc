"""Cookie-urile rotite de yt-dlp nu trebuie suprascrise la fiecare restart.

YouTube roteste __Secure-1PSIDTS si SIDCC des, iar yt-dlp scrie valorile noi in
fisierul de cookies. Daca la pornire rescriem orbeste din YT_COOKIES_CONTENT,
aruncam rotatia si ne intoarcem la valori care imbatranesc pana sunt refuzate.
Reseed-ul se face doar cand valoarea din env s-a schimbat efectiv.

Ruleaza fara pytest si fara retea:  python tests/test_cookie_seed.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import config

NETSCAPE = ('# Netscape HTTP Cookie File\n'
            '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tvaloare-din-env\n')
ROTATED = ('# Netscape HTTP Cookie File\n'
           '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tvaloare-rotita\n'
           '.youtube.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-1PSIDTS\tnou\n')


def _in_temp_dir(fn):
    """Ruleaza fn(dir) cu COOKIE_DIR pointat pe un director temporar."""
    original = config.COOKIE_DIR
    with tempfile.TemporaryDirectory() as d:
        config.COOKIE_DIR = d
        try:
            return fn(d)
        finally:
            config.COOKIE_DIR = original


def test_seeds_from_env_on_first_boot():
    def check(d):
        path, entries = config.seed_cookies_from_env(NETSCAPE)
        assert path == os.path.join(d, 'cookies.txt'), path
        assert entries == 1, entries
        assert 'valoare-din-env' in open(path, encoding='utf-8').read()
        assert os.path.exists(os.path.join(d, '.cookies_seed'))
    _in_temp_dir(check)


def test_unchanged_env_keeps_rotated_file():
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        # yt-dlp rescrie fisierul cu valorile rotite de YouTube
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(ROTATED)
        path2, entries = config.seed_cookies_from_env(NETSCAPE)
        assert path2 == path
        body = open(path, encoding='utf-8').read()
        assert 'valoare-rotita' in body, 'rotatia a fost pierduta la restart'
        assert 'valoare-din-env' not in body
        assert entries == 2, entries
    _in_temp_dir(check)


def test_changed_env_forces_reseed():
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(ROTATED)
        fresh = NETSCAPE.replace('valoare-din-env', 'cookie-nou-lipit-manual')
        path2, entries = config.seed_cookies_from_env(fresh)
        body = open(path2, encoding='utf-8').read()
        assert 'cookie-nou-lipit-manual' in body, 'reseed-ul nu s-a aplicat'
        assert 'valoare-rotita' not in body
        assert entries == 1
    _in_temp_dir(check)


def test_escaped_newlines_from_railway_are_restored():
    def check(d):
        path, entries = config.seed_cookies_from_env(NETSCAPE.replace('\n', '\\n'))
        body = open(path, encoding='utf-8').read()
        assert body.startswith('# Netscape HTTP Cookie File\n'), repr(body[:40])
        assert entries == 1
    _in_temp_dir(check)


def test_missing_env_is_not_an_error():
    def check(d):
        assert config.seed_cookies_from_env(None) == (None, 0)
        assert config.seed_cookies_from_env('') == (None, 0)
    _in_temp_dir(check)


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
