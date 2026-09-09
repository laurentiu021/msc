"""Intrarea utilizatorului si titlurile lipsa nu trebuie sa poata rupe botul.

1. sanitize_query: fara allowlist de host, orice string cu schema ajungea la
   extractorul generic al yt-dlp. Acela urmarea URL-ul, iar un raspuns
   application/x-mpegurl devenea formate HLS reale pe care botul le descarca si
   le reda. Cererea duce si cookiefile-ul, deci un Set-Cookie ostil ajungea in
   /data/cookies.txt, iar seed_cookies_from_env nu il curata (amprenta env e
   neschimbata).

2. item_title: yt-dlp intoarce title=None pentru unele intrari — cheia EXISTA,
   deci dict.get('title', 'Necunoscut') nu ajuta. Indexarea unui None arunca
   TypeError in primul statement din _play_next_async si ucide sesiunea mut.

Ruleaza fara pytest si fara retea:  python tests/test_input_hardening.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music.commands import ALLOWED_HOSTS, sanitize_query
from music.utils import is_clean, item_title


def test_plain_text_becomes_a_search():
    assert sanitize_query('manele noi 2026') == ('manele noi 2026', None)


def test_youtube_urls_pass_through():
    for url in ('https://youtu.be/jyd81XVz1ZE',
                'https://www.youtube.com/watch?v=x&list=RDx',
                'https://music.youtube.com/watch?v=x',
                'https://m.youtube.com/watch?v=x'):
        query, reason = sanitize_query(url)
        assert reason is None, f'{url} respins: {reason}'
        assert query == url


def test_foreign_hosts_are_rejected():
    for url in ('http://evil.example.com/pwn.m3u8',
                'https://127.0.0.1:8080/admin',
                'https://169.254.169.254/latest/meta-data/',
                'https://raw.githubusercontent.com/x/y/z.m3u8'):
        query, reason = sanitize_query(url)
        assert query is None, f'{url} a trecut allowlist-ul'
        assert reason


def test_non_http_schemes_are_rejected():
    for url in ('file:///etc/passwd', 'ftp://host/x', 'data:audio/mpeg;base64,AAA'):
        query, reason = sanitize_query(url)
        assert query is None, f'{url} a trecut'
        assert reason


def test_spotify_uri_becomes_a_resolvable_web_url():
    """Nu o cautare pe ID-ul opac.

    Vechea varianta intorcea ytsearch:<id>, deci botul cauta pe YouTube
    "4cOdK2wGLETKBW3PvgPWqT" si reda orice rezultat, fara eroare. Acum URI-ul
    devine forma web, care trece prin resolver-ul de platforma.
    """
    query, reason = sanitize_query('spotify:track:4cOdK2wGLETKBW3PvgPWqT')
    assert reason is None
    assert query == 'https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT', query
    assert not query.startswith('ytsearch:')


def test_malformed_spotify_uri_is_rejected_not_guessed():
    query, reason = sanitize_query('spotify:bad')
    assert query is None
    assert reason


def test_empty_input_is_rejected():
    assert sanitize_query('')[0] is None
    assert sanitize_query('   ')[0] is None


def test_allowlist_has_no_wildcards():
    """O intrare cu wildcard ar face verificarea inutila."""
    for host in ALLOWED_HOSTS:
        assert '*' not in host and '/' not in host, host
        assert host == host.lower()


def test_item_title_handles_none():
    assert item_title({'title': None, 'query': 'x'}) == 'x'
    assert item_title({'title': None}) == 'Necunoscut'
    assert item_title({}) == 'Necunoscut'
    assert item_title(None) == 'Necunoscut'
    assert item_title({'title': 'Un titlu lung foarte lung'}, 6) == 'Un tit'


def test_item_title_never_raises_on_slicing():
    """Exact crash-ul vechi: next_item['title'][:40] cu title=None."""
    for item in ({'title': None}, {}, {'title': 0}, {'title': []}):
        assert isinstance(item_title(item, 40), str)


def test_is_clean_rejects_missing_title_instead_of_crashing():
    assert is_clean(None, 200, '') is False
    assert is_clean('', 200, '') is False
    assert is_clean('melodie normala', 200, '') is True


def test_is_clean_still_filters_duration_and_blocklist():
    assert is_clean('melodie', 5, '') is False, 'prea scurt'
    assert is_clean('melodie', 4000, '') is False, 'prea lung'
    assert is_clean('lofi chill beats', 200, '') is False, 'blocklist'


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
