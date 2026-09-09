"""Regresie: extractor_args trebuie sa fie dict-de-dict, nu sintaxa de CLI.

Capcana pe care o pazeste testul asta: yt-dlp accepta fara nicio eroare
`{'youtube': 'player_client=mweb'}` — forma corecta pe linia de comanda — dar
in API-ul Python o ignora complet si foloseste clientii impliciti. Efectul in
producție a fost ca un lant de 5 clienti diferiti rula de 5 ori acelasi set
implicit: cereri multiplicate degeaba, HTTP 429, apoi 403 la download.

Ruleaza fara pytest si fara retea:  python tests/test_extractor_args.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp

from music.config import YDL_OPTS_DOWNLOAD, YDL_OPTS_SEARCH, yt_client_args


def _resolved_clients(extractor_args):
    """Ce vede efectiv extractorul YouTube pentru player_client."""
    ydl = yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True,
                            'extractor_args': extractor_args})
    ie = ydl.get_info_extractor('Youtube')
    return ie._configuration_arg('player_client', [], ie_key='youtube',
                                 casesense=True)


def test_helper_reaches_the_extractor():
    assert _resolved_clients(yt_client_args('android_vr')) == ['android_vr']
    assert _resolved_clients(yt_client_args('android_vr', 'web_safari')) == \
        ['android_vr', 'web_safari']


def test_cli_string_form_is_silently_ignored():
    """Documenteaza capcana: nu arunca eroare, doar nu are efect."""
    assert _resolved_clients({'youtube': 'player_client=android_vr'}) == []


def test_module_level_opts_are_honored():
    """Opts-urile reale folosite de bot, nu doar helper-ul."""
    for name, opts in (('SEARCH', YDL_OPTS_SEARCH),
                       ('DOWNLOAD', YDL_OPTS_DOWNLOAD)):
        clients = _resolved_clients(opts['extractor_args'])
        assert clients, f'{name}: player_client nu ajunge la extractor'
        assert 'android_vr' in clients, \
            f'{name}: android_vr lipseste (singurul client fara cookies/PO Token)'


def test_download_format_selector_prefers_opus():
    """android_vr ofera opus 251; selectorul nu trebuie sa cada pe altceva."""
    assert YDL_OPTS_DOWNLOAD['format'].startswith('bestaudio[acodec=opus]')


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
