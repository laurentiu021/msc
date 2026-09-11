"""Cele Șapte defecte "high" din audit care nu aveau plasa nicaieri.

Fiecare are un simptom pe care un utilizator il vede, si niciunul nu se vedea in
loguri. Ruleaza fara pytest, fara retea, fara Discord:

    python tests/test_audit_high.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DISCORD_TOKEN', 'test-token-nefolosit')

from music import config, utils
from music.utils import UndecodableAudio, format_time, trim_download_cache


# --- format_time: o durata float ridica ValueError din stratul de UI ----------

def test_a_float_duration_does_not_raise():
    """yt-dlp intoarce durata ca float pentru multe clipuri.

    `divmod` pe float da float, deci `f"{m:02d}"` ridica ValueError. Excepția
    ieșea din stratul de UI si era prinsa de `except Exception` din process_play,
    care o raporta ca "Eroare necunoscuta" pe o piesa perfect redabila.
    """
    assert format_time(90.7) == '1:30'
    assert format_time(3725.4) == '1:02:05'


def test_format_time_survives_anything_the_panel_can_hold():
    for value in (None, '', 'abc', -5, 0, float('nan'), float('inf'), True):
        out = format_time(value)
        assert isinstance(out, str) and out, (value, out)


# --- cache: un .part inghetat facea evacuarea imposibila ---------------------

def _cache(tmp, files):
    for name, size, age in files:
        path = os.path.join(tmp, name)
        with open(path, 'wb') as fh:
            fh.write(b'x' * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return tmp


def test_a_frozen_partial_no_longer_makes_eviction_impossible():
    """Marimea unui `.part` se numara in total, dar el nu era candidat.

    Bucla se opreste doar cand totalul scade sub plafon, deci un `.part` inghetat
    mai mare decat plafonul facea condiția de ieșire imposibila: fiecare trecere
    ștergea TOT ce nu era protejat — inclusiv piesa abia adusa de prefetch — si tot
    rămânea peste limita.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _cache(tmp, [('uriaș.opus.part', 3000, 3600),      # abandonat de o ora
                     ('a.opus', 100, 300),
                     ('b.opus', 100, 200)])
        removed = trim_download_cache(set(), max_bytes=500, directory=tmp)
        rest = sorted(os.listdir(tmp))
        assert 'uriaș.opus.part' not in rest, (
            f'transferul abandonat a rămas si a forțat evacuarea altora: {rest}')
        assert removed >= 1
        assert 'b.opus' in rest, (
            f'a Șters piese bune desi gunoiul era chiar acolo: {rest}')


def test_a_live_partial_is_still_protected():
    """Un transfer CHIAR in curs nu are voie sa fie tras de sub yt-dlp."""
    with tempfile.TemporaryDirectory() as tmp:
        _cache(tmp, [('viu.opus.part', 3000, 5),           # atins acum 5s
                     ('a.opus', 100, 300)])
        trim_download_cache(set(), max_bytes=500, directory=tmp)
        assert 'viu.opus.part' in os.listdir(tmp), (
            'a Șters un transfer in curs: yt-dlp ar raporta o defectiune tehnica')


# --- audio: un fisier care nu se decodeaza raportat ca succes -----------------

def test_a_file_without_audio_is_refused_not_played():
    """`probe` inghite eșecul si intoarce `(None, None)`.

    Un `.part` redenumit, un HTML de eroare salvat ca audio sau un transfer
    trunchiat ajungea la FFmpeg, care ieșea imediat cu 0 cadre — iar redarea era
    raportata ca REUSITA: niciun mesaj, contorul de erori neatins, si fisierul
    otravit rămânea in cache, servit la fiecare reluare.
    """
    class _FakeOpus:
        @classmethod
        async def probe(cls, source, **kw):
            return (None, None)

        def __init__(self, *a, **k):
            raise AssertionError('nu trebuie sa ajunga la FFmpeg')

    saved = utils.discord
    utils.discord = type('D', (), {'FFmpegOpusAudio': _FakeOpus})
    try:
        try:
            asyncio.run(utils.make_opus_source(
                'stricat.webm', type('Ch', (), {'bitrate': 64000})()))
        except UndecodableAudio:
            pass
        else:
            raise AssertionError('un fisier fara audio a fost acceptat ca redabil')
    finally:
        utils.discord = saved


def test_the_pcm_fallback_does_not_catch_an_undecodable_file():
    """Fallback-ul ar reda acelasi fisier stricat si ar raporta iar succes."""
    import ast
    import inspect

    from music import player

    src = inspect.getsource(player.process_play)
    handlers = [ast.unparse(h.type) for node in ast.walk(ast.parse(src.strip()))
                if isinstance(node, ast.Try) for h in node.handlers if h.type]
    assert any('UndecodableAudio' in h for h in handlers), (
        'fisierul stricat cade in fallback-ul PCM, care il reda din nou')


# --- cookies: un jar fara sesiune YouTube era adoptat si promovat -------------

def _jar(tmp, lines):
    path = os.path.join(tmp, 'cookies.txt')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('# Netscape HTTP Cookie File\n')
        for domain, name, value in lines:
            fh.write(f'{domain}\tTRUE\t/\tTRUE\t9999999999\t{name}\t{value}\n')
    return path


def test_a_jar_with_only_sapisid_is_not_valid():
    """SAPISID nu e un credential: e intrarea pentru `Authorization: SAPISIDHASH`,
    si in export-urile reale e cookie de `.google.com`, deci nu pleaca niciodata
    spre YouTube. Un jar rămas doar cu el trecea un OR pe cele patru nume, era
    adoptat pe volum, si apoi promovat peste singura copie care mai autentifica."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _jar(tmp, [('.google.com', 'SAPISID', 'abc')])
        assert config.cookies_valid(path) is False


def test_a_session_cookie_for_google_only_is_not_enough():
    with tempfile.TemporaryDirectory() as tmp:
        path = _jar(tmp, [('.google.com', '__Secure-1PSID', 'abc')])
        assert config.cookies_valid(path) is False, (
            'un SID de .google.com nu pleaca spre youtube.com')


def test_a_real_youtube_session_is_valid():
    with tempfile.TemporaryDirectory() as tmp:
        path = _jar(tmp, [('.youtube.com', '__Secure-1PSID', 'abc'),
                          ('.google.com', 'SAPISID', 'def')])
        assert config.cookies_valid(path) is True


def test_httponly_entries_still_count():
    """Export-urile reale scriu cookie-urile de sesiune ca `#HttpOnly_`."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'cookies.txt')
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('# Netscape HTTP Cookie File\n')
            fh.write('#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t9999999999\t'
                     '__Secure-3PSID\tabc\n')
        assert config.cookies_valid(path) is True


def test_adopting_a_rotation_over_a_changed_jar_is_refused():
    """Compare-and-swap.

    O cerere aflata in zbor peste un `rollback_cookies()` scria instantaneul ei de
    DINAINTE peste jar-ul restaurat — iar revenirea e o singura lovitura pe proces,
    deci deja consumata. De atunci fiecare cerere folosea cookie-uri moarte pana la
    repornirea containerului.
    """
    with tempfile.TemporaryDirectory() as tmp:
        shared = _jar(tmp, [('.youtube.com', '__Secure-1PSID', 'vechi')])
        borrowed = config.borrow_cookies(shared)
        assert borrowed, 'nu s-a putut face copia privata'
        try:
            # Intre timp, revenirea repara jar-ul comun.
            reparat = _jar(tmp, [('.youtube.com', '__Secure-1PSID', 'reparat')])
            assert reparat == shared
            adopted = config.adopt_cookies(borrowed, shared)
            assert adopted is False, (
                'a suprascris jar-ul reparat cu instantaneul de dinainte')
            with open(shared, encoding='utf-8') as fh:
                assert 'reparat' in fh.read(), 'reparatia a fost pierduta'
        finally:
            config.discard_cookies(borrowed)


def test_adopting_a_rotation_over_an_unchanged_jar_still_works():
    """Cazul normal: rotatia scrisa de yt-dlp trebuie sa ajunga pe volum."""
    with tempfile.TemporaryDirectory() as tmp:
        shared = _jar(tmp, [('.youtube.com', '__Secure-1PSID', 'vechi')])
        borrowed = config.borrow_cookies(shared)
        try:
            with open(borrowed, 'a', encoding='utf-8', newline='\n') as fh:
                fh.write('.youtube.com\tTRUE\t/\tTRUE\t9999999999\tSIDCC\tnou\n')
            assert config.adopt_cookies(borrowed, shared) is True
            with open(shared, encoding='utf-8') as fh:
                assert 'SIDCC' in fh.read(), 'rotatia nu a ajuns pe volum'
        finally:
            config.discard_cookies(borrowed)


# --- butonul Autoplay nu anula timer-ul de inactivitate ----------------------

def test_the_autoplay_button_cancels_the_idle_timer_before_prefilling():
    """Prefill-ul e o operatie de secunde, iar tick-ul de inactivitate care cade in
    fereastra aceea deconecteaza botul exact la apasarea butonului care trebuia sa
    porneasca radioul. `!247` anuleaza deja timer-ul inainte de prefill."""
    import ast
    import inspect
    import textwrap

    from music import views

    # `dedent`, nu `strip`: sursa vine indentata la nivel de clasa, iar `strip`
    # curata doar primul rand — restul rămâne indentat si `ast.parse` cade.
    src = textwrap.dedent(inspect.getsource(views.MusicControlView.autoplay_btn))
    tree = ast.parse(src)
    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if 'cancel_timeout' in name:
                order.append(('cancel', node.lineno))
            elif 'prefill_autoplay_queue' in name:
                order.append(('prefill', node.lineno))
    kinds = [k for k, _ in order]
    assert 'cancel' in kinds, 'butonul Autoplay nu anuleaza timer-ul de inactivitate'
    assert 'prefill' in kinds, kinds
    first_cancel = min(line for kind, line in order if kind == 'cancel')
    first_prefill = min(line for kind, line in order if kind == 'prefill')
    assert first_cancel < first_prefill, (
        'anularea vine DUPA prefill: tick-ul din fereastra deconecteaza botul')


# --- Spotify/Deezer ilizibil lasa 24/7 fara niciun tick ---------------------

def test_an_unreadable_platform_link_re_arms_the_idle_timer():
    """`cancel_timeout` e chemat inainte de scrape (deliberat), iar `idle_timer` se
    re-armeaza doar din propriul `finally` — adica doar dintr-un tick care exista
    deja. yt-dlp nu are extractor de Spotify/Deezer, deci asta e rezultatul
    obișnuit: 24/7 rămânea ON, botul in canal, coada goala si niciun tick programat.
    """
    import ast
    import inspect

    from music import commands as commands_mod

    src = inspect.getsource(commands_mod.setup_music_commands)
    tree = ast.parse(src.strip())
    hits = 0
    for node in ast.walk(tree):
        # Exact garda `if resolved is None:`, nu orice bloc care o conține: un
        # `ast.walk` peste tot gaseste si `if`-urile exterioare.
        if not isinstance(node, ast.If):
            continue
        if ast.unparse(node.test) != 'resolved is None':
            continue
        body = ast.unparse(node.body)
        if 'Spotify/Deezer' not in body:
            continue
        hits += 1
        assert 'resume_if_idle' in body, (
            'ieșirea pe link ilizibil nu re-armeaza nimic: 24/7 rămâne fara tick')
    assert hits == 2, f'aȘteptam doua locuri (play si nplay), am gasit {hits}'


if __name__ == '__main__':
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
