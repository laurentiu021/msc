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


# --- jar-ul ca tranzactie ----------------------------------------------------

TRUNCATED = '# Netscape HTTP Cookie File\n'
NO_SESSION = ('# Netscape HTTP Cookie File\n'
              '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tVISITOR_INFO1_LIVE\tx\n')


def test_a_healthy_jar_reports_its_session_cookies():
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        health = config.cookie_health(path)
        assert health['entries'] == 1, health
        assert 'SID' in health['present'], health
        assert health['earliest_expiry'] == 1790000000, health
        assert config.cookies_valid(path) is True
    _in_temp_dir(check)


def test_a_truncated_jar_is_not_valid():
    """Exact rezultatul vechii trunchieri la timeout: 142 de octeti -> 28."""
    def check(d):
        path = os.path.join(d, 'cookies.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(TRUNCATED)
        assert config.cookies_valid(path) is False
        assert config.cookie_health(path)['entries'] == 0
    _in_temp_dir(check)


def test_a_jar_without_session_cookies_is_not_valid():
    """Are linii, dar niciuna care sa autentifice: nu e "cookies rotite"."""
    def check(d):
        path = os.path.join(d, 'cookies.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(NO_SESSION)
        assert config.cookies_valid(path) is False
        assert config.cookie_health(path)['present'] == []
        assert 'SID' in config.cookie_health(path)['missing']
    _in_temp_dir(check)


def test_a_broken_jar_is_reseeded_instead_of_kept_forever():
    """Ramura de pastrare il pastra ORICUM, raportandu-l drept rotit de yt-dlp."""
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(TRUNCATED)          # yt-dlp intrerupt in mijlocul scrierii
        path2, entries = config.seed_cookies_from_env(NETSCAPE)
        assert path2 == path
        assert entries >= 1, 'a pastrat un jar fara nicio intrare'
        assert config.cookies_valid(path), 'jar-ul a rămas invalid dupa pornire'
    _in_temp_dir(check)


def test_promote_then_rollback_round_trips():
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        config.apply_cookies(path)
        assert config.promote_cookies() is True
        assert os.path.exists(path + config._GOOD_SUFFIX), 'nu s-a scris copia buna'

        # YouTube invalideaza sesiunea: yt-dlp scrie ceva ce nu autentifica
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(NO_SESSION)
        assert config.cookies_valid(path) is False

        config._rolled_back = False
        assert config.rollback_cookies() is True
        assert config.cookies_valid(path) is True
        assert 'valoare-din-env' in open(path, encoding='utf-8').read()
    _in_temp_dir(check)


def test_rollback_happens_at_most_once_per_process():
    """Altfel o sesiune moarta ar produce o bucla de reveniri si reincercari."""
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        config.apply_cookies(path)
        config.promote_cookies()
        config._rolled_back = False
        assert config.rollback_cookies() is True
        assert config.rollback_cookies() is False, 'a revenit a doua oara'
    _in_temp_dir(check)


def test_rollback_without_a_good_copy_does_nothing():
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        config.apply_cookies(path)
        config._rolled_back = False
        assert config.rollback_cookies() is False
    _in_temp_dir(check)


def test_a_broken_jar_is_never_promoted():
    def check(d):
        path = os.path.join(d, 'cookies.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(NO_SESSION)
        config.apply_cookies(path)
        assert config.promote_cookies() is False
        assert not os.path.exists(path + config._GOOD_SUFFIX)
    _in_temp_dir(check)


def test_the_jar_is_written_atomically_and_privately():
    """O scriere directa are o fereastra trunchiata; un redeploy atunci o fixeaza."""
    import ast
    import inspect

    src = inspect.getsource(config._write_private)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
             if isinstance(n, ast.Call)}
    assert 'os.replace' in calls, 'scriere neatomica'
    assert 'os.open' in calls, 'drepturile nu sunt setate la creare'
    assert '0o600' in src, 'fisierul nu e privat'

    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        assert not os.path.exists(path + '.tmp'), 'a rămas un fisier temporar'
        if os.name != 'nt':      # drepturile POSIX nu exista pe Windows
            assert oct(os.stat(path).st_mode)[-3:] == '600'
    _in_temp_dir(check)


def test_there_is_one_cookie_line_parser():
    """Doua copii ale aceluiasi parser ar da doua raspunsuri diferite."""
    import glob
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for path in glob.glob(str(root / 'music' / '*.py')) + [str(root / 'bot.py')]:
        src = pathlib.Path(path).read_text(encoding='utf-8')
        hits += [(os.path.basename(path), line.strip())
                 for line in src.splitlines()
                 if "startswith('#')" in line and 'splitlines' not in line
                 and 'l.strip()' in line]
    assert len(hits) <= 1, f'mai multe parsere de linii de cookie: {hits}'


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
