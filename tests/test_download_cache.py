"""Cache audio pe volum: un hit costa zero cereri catre YouTube.

`outtmpl` era deja `%(id)s.%(ext)s`, adica cheia de cache exista pe disc de la
inceput. Doar ca `cleanup_file` stergea fisierul ~2 secunde dupa final, boot-ul
golea directorul, iar singura reutilizare era potrivirea exacta
`query == state.last_url`. Intr-un grup de cațiva oameni aceleasi piese revin
constant: acelasi link dat din nou, butonul Back, loop pe coada, saltul din
select, sau autoplay care re-propune o piesa ieșita din cele 20 de intrari de
history.

Un hit inseamna zero cereri, zero octeti de media, zero rulari de Deno, niciun
slot de throttle si nicio expunere la 429 sau la cookie-uri expirate. Cand
YouTube ne refuza, tot ce s-a ascultat inca merge.

Riscul e un fisier pe jumatate scris redat ca piesa corupta, deci `.part` si
fisierele de zero octeti nu sunt niciodata servite.

    python tests/test_download_cache.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import config, player, resolve
from music.state import GuildState
from music.utils import (cached_download, read_track_meta, sweep_partials,
                         trim_download_cache, write_track_meta)


def _write(directory, name, size=1024, age_sec=0.0):
    path = os.path.join(directory, name)
    with open(path, 'wb') as fh:
        fh.write(b'x' * size)
    if age_sec:
        when = time.time() - age_sec
        os.utime(path, (when, when))
    return path


def test_a_finished_download_is_found_again():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, 'vid123.opus')
        assert cached_download('vid123', tmp) == os.path.join(tmp, 'vid123.opus')


def test_a_partial_download_is_never_served():
    """Un SIGKILL in mijlocul descarcarii lasa un .part; redat, ar fi corupt."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, 'vid123.opus.part')
        _write(tmp, 'vid123.ytdl')
        assert cached_download('vid123', tmp) is None


def test_an_empty_file_is_never_served():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, 'vid123.opus', size=0)
        assert cached_download('vid123', tmp) is None


def test_a_different_id_is_not_a_hit():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, 'altceva.opus')
        assert cached_download('vid123', tmp) is None
        assert cached_download('', tmp) is None
        assert cached_download(None, tmp) is None


def test_a_hit_refreshes_the_recency_so_lru_means_something():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, 'vid123.opus', age_sec=10_000)
        old_mtime = os.path.getmtime(path)
        assert cached_download('vid123', tmp) == path
        assert os.path.getmtime(path) > old_mtime, (
            'un hit nu a improspatat mtime: evacuarea ar sterge exact fisierele folosite')


def test_a_missing_directory_is_not_an_error():
    assert cached_download('vid123', os.path.join('nu', 'exista')) is None
    assert trim_download_cache(set(), directory=os.path.join('nu', 'exista')) == 0
    assert sweep_partials(os.path.join('nu', 'exista')) == 0


# --- evacuare -----------------------------------------------------------------

def test_the_cache_stays_under_the_cap_oldest_first():
    with tempfile.TemporaryDirectory() as tmp:
        # 5 fisiere de 1000 de octeti, cel mai vechi primul
        paths = [_write(tmp, f'v{i}.opus', size=1000, age_sec=10_000 - i * 100)
                 for i in range(5)]
        removed = trim_download_cache(set(), max_bytes=2500, directory=tmp)
        assert removed == 3, removed
        assert not os.path.exists(paths[0]), 'cel mai vechi a supravietuit'
        assert not os.path.exists(paths[1])
        assert not os.path.exists(paths[2])
        assert os.path.exists(paths[3]), 'a sters si fisiere recente'
        assert os.path.exists(paths[4])


def test_nothing_is_removed_below_the_cap():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, 'v0.opus', size=100, age_sec=99_999)
        assert trim_download_cache(set(), max_bytes=10_000, directory=tmp) == 0
        assert os.path.exists(path)


def test_a_file_being_played_is_never_evicted():
    """Mai bine depasim plafonul cu o piesa decat sa tragem fisierul de sub FFmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        playing = _write(tmp, 'acum.opus', size=5000, age_sec=99_999)
        other = _write(tmp, 'vechi.opus', size=5000, age_sec=50_000)
        removed = trim_download_cache({playing}, max_bytes=1000, directory=tmp)
        assert os.path.exists(playing), 'a sters fisierul care se reda ACUM'
        assert not os.path.exists(other)
        assert removed == 1, removed


def test_partials_are_swept_at_boot():
    with tempfile.TemporaryDirectory() as tmp:
        part = _write(tmp, 'v0.opus.part')
        good = _write(tmp, 'v1.opus')
        assert sweep_partials(tmp) == 1
        assert not os.path.exists(part)
        assert os.path.exists(good), 'curatarea de pornire a atins cache-ul bun'


# --- integrare cu calea de redare ---------------------------------------------

def test_the_video_id_is_the_cache_key():
    assert resolve.video_id('https://www.youtube.com/watch?v=abc123') == 'abc123'
    assert resolve.video_id('https://www.youtube.com/watch?v=abc123&list=RD') == 'abc123'
    assert resolve.video_id('https://youtu.be/abc123?t=5') == 'abc123'
    assert resolve.video_id('https://example.com/nimic') is None
    assert resolve.video_id('') is None
    assert resolve.video_id(None) is None
    # Numele fisierului scris de yt-dlp e chiar ID-ul (outtmpl %(id)s.%(ext)s).
    assert '%(id)s' in config.YDL_OPTS_DOWNLOAD['outtmpl']


def test_history_supplies_the_metadata_for_a_cache_hit():
    st = GuildState()
    st.history = [
        {'url': 'https://www.youtube.com/watch?v=aaa', 'title': 'Prima', 'channel': 'C1'},
        {'url': 'https://www.youtube.com/watch?v=bbb', 'title': 'A doua', 'channel': 'C2'},
    ]
    entry = player._history_entry(st, 'bbb')
    assert entry and entry['title'] == 'A doua', entry
    assert player._history_entry(st, 'ccc') is None
    assert player._history_entry(st, '') is None


def test_history_without_a_title_is_not_usable_metadata():
    st = GuildState()
    st.history = [{'url': 'https://www.youtube.com/watch?v=aaa', 'title': None}]
    assert player._history_entry(st, 'aaa') is None


def test_the_most_recent_history_entry_wins():
    st = GuildState()
    st.history = [
        {'url': 'https://www.youtube.com/watch?v=aaa', 'title': 'Vechi'},
        {'url': 'https://www.youtube.com/watch?v=aaa', 'title': 'Nou'},
    ]
    assert player._history_entry(st, 'aaa')['title'] == 'Nou'


def test_the_cache_lives_on_the_volume_when_there_is_one():
    """Pe disc efemer fiecare deploy pierdea tot ce se ascultase."""
    import importlib

    saved = os.environ.get('COOKIE_DIR')
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['COOKIE_DIR'] = tmp
        try:
            fresh = importlib.reload(config)
            assert fresh.DOWNLOAD_DIR == os.path.join(tmp, 'audio'), fresh.DOWNLOAD_DIR
            assert fresh.YTDLP_CACHE_DIR == os.path.join(tmp, 'ytdlp-cache')
            # cachedir explicit: fara el, semnaturile yt-dlp stau in ~/.cache,
            # efemer in container, deci prima piesa de dupa deploy re-plateste
            # rezolvarea semnaturii.
            assert fresh.make_download_opts()['cachedir'] == fresh.YTDLP_CACHE_DIR
        finally:
            if saved is None:
                os.environ.pop('COOKIE_DIR', None)
            else:
                os.environ['COOKIE_DIR'] = saved
            importlib.reload(config)


# --- metadatele de langa fisierul audio -------------------------------------
# Cache-ul trebuie sa se descrie singur. Metadatele existau doar in
# `state.history`: 20 de intrari, golita de `!stop`, de plecarea din voce si de
# butonul Inapoi, si fara durata sau thumbnail in intrari. Deci un hit pornea cu
# durata 0 — panou fara lungime si fara timp rămas, `!seek` fara plafon (garda e
# scrisa `if state.last_duration and ...`) — si cumpara o unitate de Data API
# pentru statistici, exact costul pe care cache-ul exista sa il elimine.

def test_a_cached_file_carries_its_own_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, 'vid1.opus')
        meta = {'url': 'https://www.youtube.com/watch?v=vid1',
                'title': 'Artist - Piesa', 'channel': 'Canalul',
                'duration': 213, 'thumbnail': 'http://t/max.jpg',
                'views': 1234, 'likes': 56}
        assert write_track_meta(path, meta) is True
        got = read_track_meta(path)
        assert got == meta, got


def test_a_file_without_metadata_reads_as_unknown():
    """None inseamna "nu stim ce e", si apelantul trateaza asta ca lipsa de cache:
    mai bine o descarcare in plus decat un panou care minte despre ce cânta."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, 'vid2.opus')
        assert read_track_meta(path) is None
        # Si un insoțitor corupt sau gol nu are voie sa crape nimic.
        with open(os.path.splitext(path)[0] + '.meta.json', 'w',
                  encoding='utf-8') as fh:
            fh.write('{nu e json')
        assert read_track_meta(path) is None
        with open(os.path.splitext(path)[0] + '.meta.json', 'w',
                  encoding='utf-8') as fh:
            fh.write('{"title": null}')
        assert read_track_meta(path) is None


def test_the_metadata_file_is_never_served_as_audio():
    """Depinde de faptul ca extensia insoțitorului e DUBLA.

    `splitext('vid3.meta.json')` da stem-ul 'vid3.meta', care nu se potriveste
    niciodata cu un ID. Daca `_META_EXT` ar deveni un singur sufix (`.json`),
    cache-ul ar servi fisierul de metadate ca piesa.
    """
    from music.utils import _META_EXT

    assert _META_EXT.count('.') >= 2, (
        f'{_META_EXT!r}: cu un singur punct, insoțitorul devine un hit de cache')
    with tempfile.TemporaryDirectory() as tmp:
        write_track_meta(os.path.join(tmp, 'vid3.opus'), {'title': 'X'})
        assert cached_download('vid3', tmp) is None, (
            'insoțitorul a fost servit ca fisier audio')
        audio = _write(tmp, 'vid3.opus')
        assert cached_download('vid3', tmp) == audio


def test_evicting_audio_takes_its_metadata_with_it():
    """Altfel rămâne un insoțitor orfan pe volum, la fiecare evacuare."""
    with tempfile.TemporaryDirectory() as tmp:
        old = _write(tmp, 'vechi.opus', size=900, age_sec=100)
        write_track_meta(old, {'title': 'Vechi', 'duration': 100})
        new = _write(tmp, 'nou.opus', size=900, age_sec=1)
        write_track_meta(new, {'title': 'Nou', 'duration': 100})

        removed = trim_download_cache({new}, max_bytes=1200, directory=tmp)
        assert removed == 1, removed
        assert not os.path.exists(old), 'nu a evacuat fisierul vechi'
        assert not os.path.exists(os.path.splitext(old)[0] + '.meta.json'), (
            'insoțitorul a rămas orfan pe volum')
        assert os.path.exists(new) and read_track_meta(new), (
            'a atins fisierul protejat sau metadatele lui')


def test_orphan_metadata_is_swept_at_boot():
    """Un SIGKILL intre scrierea metadatelor si terminarea descarcarii, sau o
    evacuare facuta de o versiune care nu stia de insoțitori."""
    with tempfile.TemporaryDirectory() as tmp:
        orphan = os.path.join(tmp, 'fantoma.opus')
        write_track_meta(orphan, {'title': 'Fantoma'})
        keeper = _write(tmp, 'real.opus')
        write_track_meta(keeper, {'title': 'Real'})

        assert sweep_partials(tmp) == 1
        assert not os.path.exists(os.path.splitext(orphan)[0] + '.meta.json')
        assert read_track_meta(keeper), 'a sters metadatele unui fisier existent'


def test_playback_no_longer_deletes_what_it_just_played():
    """Regula centrala a cache-ului, verificata pe FISIER, nu pe nume de functii.

    Varianta veche cerea doar ca identificatorul `cleanup_file` sa nu apara in
    sursa lui `make_after_play`. O verificare de nume nu poate exprima "fisierul
    supravietuieste": un `os.remove(filename)` pus in loc ar fi trecut vesel, iar
    fiecare piesa ar fi fost stearsa la ~0s dupa final — deci cache-ul nu s-ar mai
    umple niciodata si a doua redare a oricarei piese (acelasi link, butonul Back,
    loop pe coada, autoplay care o re-propune) s-ar re-descarca integral.
    """
    class _Ctx:
        guild = type('G', (), {'id': 909})()
        voice_client = None

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'piesa.opus')
        with open(path, 'wb') as fh:
            fh.write(b'audio')

        state = GuildState()
        state.current_file = path
        trims = []
        saved = (player.play_next, player.trim_cache, player._loop)
        player.play_next = lambda *a, **k: None
        player.trim_cache = lambda *a, **k: trims.append(True)
        player._loop = None
        try:
            player.make_after_play(_Ctx(), state, path)(None)
        finally:
            player.play_next, player.trim_cache, player._loop = saved

        assert os.path.exists(path), (
            'sfarsitul de piesa a sters fisierul: cache-ul nu se umple niciodata')
        assert trims, 'nu s-a chemat evacuarea pe marime'


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
