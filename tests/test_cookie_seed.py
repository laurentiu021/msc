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
# Structural valid (numele critice sunt acolo), dar valorile sunt cele pe care
# YouTube le-a refuzat deja. Exact forma de putrezire pe care `cookies_valid` NU o
# poate detecta: verifica doar numele.
DEAD_ROTATION = ('# Netscape HTTP Cookie File\n'
                 '.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tvaloare-moarta\n')


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


# --- fisierul de cookies nu mai e partajat cu yt-dlp ------------------------

def test_a_request_works_on_its_own_copy_of_the_jar():
    """`YoutubeDL.close()` cheama `save_cookies()`, care e necondiționat.

    Adica RESCRIE fisierul din jar-ul lui din memorie, fara sa se uite ce s-a
    schimbat pe disc intre timp — verificat in yt-dlp 2026.8.19:
    `if self.params.get('cookiefile') is not None: self.cookiejar.save()`.
    """
    def check(d):
        shared = os.path.join(d, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(NETSCAPE)
        temp = config.borrow_cookies(shared)
        assert temp and temp != shared, temp
        assert 'valoare-din-env' in open(temp, encoding='utf-8').read()
        config.discard_cookies(temp)
        assert not os.path.exists(temp), 'copia nu a fost aruncata'
    _in_temp_dir(check)


def test_an_abandoned_thread_cannot_undo_a_rollback():
    """Defectul, capat la capat, cu instanta REALA de yt-dlp.

    Cronologie reprodusa inainte de fix: o descarcare depaseste bugetul de 240s cu
    cookies, thread-ul continua cu jar-ul MORT in memorie; piesa urmatoare eșua,
    `rollback_cookies()` restaura copia buna si consuma singura lovitura pe
    proces; thread-ul abandonat se inchidea si rescria jar-ul MORT peste ea. De
    atunci fiecare cerere folosea cookie-uri moarte, iar revenirea intorcea False.
    """
    import yt_dlp

    def check(d):
        shared = os.path.join(d, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(DEAD_ROTATION)

        # Cererea porneste si isi ia copia — exact ce face acum ytdlp.extract.
        private = config.borrow_cookies(shared)
        ydl = yt_dlp.YoutubeDL({'cookiefile': private, 'quiet': True})
        _ = ydl.cookiejar                     # incarca valorile MOARTE

        # Intre timp, revenirea restaureaza jar-ul bun in fisierul COMUN.
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(NETSCAPE)

        # Thread-ul abandonat se inchide in cele din urma.
        ydl.close()

        body = open(shared, encoding='utf-8').read()
        assert 'valoare-din-env' in body, (
            'un thread abandonat a rescris jar-ul comun si a anulat revenirea')
        assert 'valoare-moarta' not in body, body
        # Copia lui exista inca si conține ce a scris el; se arunca, nu se adopta.
        config.discard_cookies(private)
    _in_temp_dir(check)


def test_only_a_successful_request_hands_its_rotation_to_the_shared_jar():
    def check(d):
        shared = os.path.join(d, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(NETSCAPE)
        private = config.borrow_cookies(shared)
        with open(private, 'w', encoding='utf-8') as fh:
            fh.write(ROTATED)
        assert config.adopt_cookies(private, shared) is True
        assert 'valoare-rotita' in open(shared, encoding='utf-8').read(), (
            'rotatia scrisa de yt-dlp nu ajunge pe volum')
        config.discard_cookies(private)
    _in_temp_dir(check)


def test_a_copy_without_session_cookies_is_never_adopted():
    """Cand YouTube invalideaza sesiunea, scrie peste jar valori care nu mai
    autentifica; adoptarea lor orbeste ar face exact ce facea partajarea."""
    def check(d):
        shared = os.path.join(d, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(NETSCAPE)
        private = config.borrow_cookies(shared)
        with open(private, 'w', encoding='utf-8') as fh:
            fh.write(NO_SESSION)
        assert config.adopt_cookies(private, shared) is False
        assert 'valoare-din-env' in open(shared, encoding='utf-8').read()
        config.discard_cookies(private)
    _in_temp_dir(check)


def test_copies_left_by_a_killed_process_are_swept_at_boot():
    """os._exit din watchdog nu ruleaza niciun finally: copia rămâne pe volum,
    si fiecare conține o sesiune Google."""
    def check(d):
        shared = os.path.join(d, 'cookies.txt')
        with open(shared, 'w', encoding='utf-8') as fh:
            fh.write(NETSCAPE)
        orphans = [config.borrow_cookies(shared) for _ in range(3)]
        assert all(orphans), orphans
        assert config.sweep_borrowed_cookies(d) == 3
        assert all(not os.path.exists(p) for p in orphans)
        assert os.path.exists(shared), 'curatarea a sters jar-ul real'
        assert config.sweep_borrowed_cookies(d) == 0, 'a doua trecere a sters ceva'
    _in_temp_dir(check)


def test_a_successful_promotion_re_arms_the_rollback():
    """Steagul opreste o BUCLA de reveniri, nu a doua salvare din viata procesului.

    Promovarea se cheama doar dupa o descarcare care a folosit cookie-urile, deci
    e dovada ca episodul precedent s-a inchis. Fara re-armare, o singura zi cu
    doua invalidari de sesiune lasa botul fara nicio plasa la a doua.
    """
    def check(d):
        path, _ = config.seed_cookies_from_env(NETSCAPE)
        config.apply_cookies(path)
        config._rolled_back = False
        config.promote_cookies()
        assert config.rollback_cookies() is True
        assert config.rollback_cookies() is False, 'a revenit doua ori la rand'
        assert config.promote_cookies() is True
        assert config.rollback_cookies() is True, (
            'o promovare reusita nu re-armeaza revenirea')
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
