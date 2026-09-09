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


def test_every_client_can_get_a_po_token_from_bgutil():
    """Blocheaza clientii pentru care bgutil NU poate emite PO Token.

    bgutil e BotGuard (web). Un client android/ios are nevoie de DroidGuard,
    respectiv iOSGuard; fara token, yt-dlp ii omite formatele in silentiu, iar
    botul raporteaza "formate reale" inexistente si apoi nu descarca nimic.
    Regula asta trebuie verificata mecanic, nu tinuta minte.
    """
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    from yt_dlp.extractor.youtube.pot.utils import WEBPO_CLIENTS

    def check(name, clients):
        assert clients, f'{name}: niciun client'
        for client in clients:
            spec = INNERTUBE_CLIENTS.get(client)
            assert spec, f'{name}: {client!r} nu e un client yt-dlp valid'
            inner = spec['INNERTUBE_CONTEXT']['client']['clientName']
            assert inner in WEBPO_CLIENTS, (
                f'{name}: {client} -> {inner} nu e in WEBPO_CLIENTS, '
                f'deci bgutil nu-i poate emite GVS PO Token'
            )

    for name, opts in (('SEARCH', YDL_OPTS_SEARCH),
                       ('DOWNLOAD', YDL_OPTS_DOWNLOAD)):
        check(name, _resolved_clients(opts['extractor_args']))

    # Lanturile din player.py sunt cele folosite EFECTIV la runtime. Cand erau
    # definite local in process_play, testul verifica doar copia din config si o
    # schimbare in player trecea nedetectata — exact regresia pe care testul
    # trebuie sa o previna.
    from music import player
    for name, chain in (('COOKIE_CHAIN', player.COOKIE_CHAIN),
                        ('GUEST_CHAIN', player.GUEST_CHAIN)):
        assert chain, f'{name} e gol'
        for clients, _use_cookies in chain:
            check(name, list(clients))
            check(f'{name} via yt_client_args',
                  _resolved_clients(yt_client_args(*clients)))


def test_download_format_selector_prefers_opus():
    assert YDL_OPTS_DOWNLOAD['format'].startswith('bestaudio[acodec=opus]')


def test_unusable_formats_do_not_count_as_real():
    """Un format fara URL, cu DRM sau storyboard nu inseamna "merge".

    Regresie directa: cu verificarea permisiva, un singur format inutilizabil
    facea botul sa anunte "5 formats (1 real)", sa opreasca lantul de clienti
    pe acel client si apoi sa nu descarce nimic.
    """
    from music.config import count_real_formats, has_real_formats

    assert not has_real_formats([])
    assert not has_real_formats([{'acodec': 'opus'}])                      # fara sursa
    assert not has_real_formats([{'acodec': 'opus', 'url': 'x',
                                  'has_drm': True}])                       # DRM
    assert not has_real_formats([{'acodec': 'none', 'vcodec': 'none',
                                  'url': 'x'}])                            # fara audio
    assert not has_real_formats([{'acodec': 'opus', 'url': 'x',
                                  'format_note': 'storyboard'}])
    assert has_real_formats([{'acodec': 'opus', 'url': 'x'}])
    assert has_real_formats([{'acodec': 'none', 'vcodec': 'avc1',
                              'protocol': 'm3u8_native', 'url': 'x'}])
    assert count_real_formats([{'acodec': 'opus', 'url': 'x'},
                               {'acodec': 'opus'}]) == 1


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
