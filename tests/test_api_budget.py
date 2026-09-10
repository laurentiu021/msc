"""Cota Data API: ce costa, cine plateste, si cand ne oprim.

Trei scurgeri, toate confirmate in sursa inainte de fix:

1. `_try_api_related` isi documenta costul ca 100 de unitati, dar rula
   `videos.list` (1) + doua `search.list` (100 + 100) = 201. Primul `videos.list`
   exista doar ca sa afle titlul si canalul videoclipului curent — exact ce botul
   avea deja in `state.last_title` si `state.last_channel`.

2. A doua cautare era gardata de `len(results) < max_results` cu max_results=20.
   O pagina nu da aproape niciodata 20 de supravietuitori ai filtrelor, deci
   cererea de 100 de unitati pornea aproape mereu, chiar cand prima adusese
   destul.

3. `search()` platea pentru durate, dar ambele strategii de API chemau
   `_add_to_queue` fara `duration=`/`live_status=`, deci filtrele de durata si de
   live NU rulau pe rezultatele API: live-uri si colaje de 40 de minute intrau in
   coada, iar abia match_filter le refuza la descarcare, in tacere.

Si epuizarea: dupa 10.000 de unitati `_api_get` primea 403, il loga ca warning,
si autoplay se oprea fara sa spuna nimic.

    python tests/test_api_budget.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import youtube_api as api


class _Recorder:
    """Inlocuieste _api_get si retine fiecare endpoint cerut, cu costul lui."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __enter__(self):
        self.saved = api._api_get
        self.saved_key = api.API_KEY
        api.API_KEY = 'test-key'

        def fake(endpoint, params):
            self.calls.append(endpoint)
            api._units_spent += api.UNIT_COST[endpoint]
            return self.responses.get(endpoint)

        api._api_get = fake
        api._units_spent = 0
        api._quota_day = api._utc_day()
        api._cap_logged = False
        return self

    def __exit__(self, *exc):
        api._api_get = self.saved
        api.API_KEY = self.saved_key
        api._units_spent = 0
        api._quota_day = None
        return False

    @property
    def cost(self):
        return sum(api.UNIT_COST[e] for e in self.calls)


def _search_page(ids, live=None):
    return {'items': [
        {'id': {'videoId': v},
         'snippet': {'title': f'Artist{v} - Piesa {v}', 'channelTitle': f'Canal{v}',
                     'thumbnails': {'high': {'url': 'http://t'}}}}
        for v in ids]}


def _videos_page(ids, duration='PT3M30S', live='none'):
    return {'items': [
        {'id': v,
         'contentDetails': {'duration': duration},
         'statistics': {'viewCount': '1000', 'likeCount': '10'},
         'snippet': {'title': f'Artist{v} - Piesa {v}', 'channelTitle': f'Canal{v}',
                     'liveBroadcastContent': live,
                     'thumbnails': {'high': {'url': 'http://t'}}}}
        for v in ids]}


def test_related_with_known_title_skips_the_metadata_purchase():
    with _Recorder({'search': _search_page(['a', 'b', 'c']),
                    'videos': _videos_page(['a', 'b', 'c'])}) as rec:
        # Titlul seed trebuie sa fie DIFERIT de rezultate: _titles_too_similar
        # respinge acelasi cantec in alta versiune, deci un seed identic ar
        # filtra tot si testul ar masura filtrul, nu costul.
        results = api.get_related_videos('origin', max_results=20,
                                         title='Alt Artist - Cu totul altceva',
                                         channel='Canalul', needed=2)
    assert results, 'nu a intors nimic'
    # o cautare (100) + un batch de detalii (1). Fara `videos.list` initial, si
    # fara a doua cautare, pentru ca `needed=2` era deja satisfacut.
    assert rec.calls == ['search', 'videos'], rec.calls
    assert rec.cost == 101, rec.cost


def test_related_without_title_still_looks_it_up():
    """Compatibilitate: un apelant care nu are titlul trebuie sa functioneze."""
    with _Recorder({'search': _search_page(['a']),
                    'videos': _videos_page(['a'])}) as rec:
        api.get_related_videos('origin', max_results=5)
    assert rec.calls[0] == 'videos', rec.calls


def test_the_second_search_only_fires_when_the_first_is_short():
    # needed=5, prima cautare da 2 rezultate utile -> a doua cautare e justificata
    with _Recorder({'search': _search_page(['a', 'b']),
                    'videos': _videos_page(['a', 'b'])}) as rec:
        api.get_related_videos('origin', max_results=20, title='T - X',
                               channel='C', needed=5)
    assert rec.calls.count('search') == 2, rec.calls

    # needed=2, prima cautare da 2 -> a doua nu mai are rost
    with _Recorder({'search': _search_page(['a', 'b']),
                    'videos': _videos_page(['a', 'b'])}) as rec:
        api.get_related_videos('origin', max_results=20, title='T - X',
                               channel='C', needed=2)
    assert rec.calls.count('search') == 1, rec.calls


def test_related_results_carry_duration_and_live_status():
    with _Recorder({'search': _search_page(['a']),
                    'videos': _videos_page(['a'], duration='PT45M', live='live')}) as rec:
        results = api.get_related_videos('origin', title='T - X', channel='C',
                                         needed=1)
    assert results[0]['duration'] == 45 * 60, results[0]
    assert results[0]['live_status'] == 'is_live', results[0]


def test_search_results_carry_live_status():
    with _Recorder({'search': _search_page(['a']),
                    'videos': _videos_page(['a'], live='upcoming')}) as rec:
        results = api.search('ceva')
    assert results[0]['live_status'] == 'is_upcoming', results[0]


def test_live_broadcast_none_becomes_none_not_a_string():
    with _Recorder({'videos': _videos_page(['a'], live='none')}):
        details = api.get_video_details(['a'])
    assert details['a']['live_status'] is None, details


def test_every_endpoint_has_a_published_price():
    """Un endpoint nou fara tarif trebuie sa dea KeyError, nu sa treaca gratis."""
    assert api.UNIT_COST == {'search': 100, 'videos': 1}, api.UNIT_COST
    saved_key, api.API_KEY = api.API_KEY, 'k'
    try:
        raised = False
        try:
            api._api_get('playlistItems', {})
        except KeyError:
            raised = True
        assert raised, 'un endpoint netaxat a trecut'
    finally:
        api.API_KEY = saved_key


def test_the_daily_cap_closes_the_tap():
    saved = (api.API_KEY, api._units_spent, api._quota_day, api.DAILY_UNIT_CAP)
    try:
        api.API_KEY = 'k'
        api.DAILY_UNIT_CAP = 250
        api._quota_day = api._utc_day()
        api._units_spent = 0
        assert api.is_available() is True
        api._units_spent = 200
        assert api.is_available() is True, 'sub plafon trebuie sa mearga'
        api._units_spent = 250
        assert api.is_available() is False, 'la plafon trebuie sa se opreasca'
        assert api.units_spent() == 250
    finally:
        (api.API_KEY, api._units_spent, api._quota_day,
         api.DAILY_UNIT_CAP) = saved


def test_a_new_day_resets_the_budget():
    saved = (api.API_KEY, api._units_spent, api._quota_day)
    try:
        api.API_KEY = 'k'
        api._units_spent = 9999
        api._quota_day = '1999-01-01'          # alta zi decat azi
        assert api.units_spent() == 0, 'cota nu s-a resetat la zi noua'
        assert api.is_available() is True
    finally:
        api.API_KEY, api._units_spent, api._quota_day = saved


def test_no_api_key_means_unavailable_regardless_of_budget():
    saved = (api.API_KEY, api._units_spent)
    try:
        api.API_KEY = None
        api._units_spent = 0
        assert api.is_available() is False
    finally:
        api.API_KEY, api._units_spent = saved


def test_the_filters_actually_run_on_api_results():
    """Un live de 45 de minute din API nu are voie sa intre in coada."""
    from music.autoplay import _add_to_queue
    from music.state import GuildState

    st = GuildState()
    assert _add_to_queue(st, 'x', 'Artist - Live acum', set(), {}, 'Canal',
                         duration=45 * 60, live_status='is_live') is False
    assert _add_to_queue(st, 'y', 'Artist - Colaj lung', set(), {}, 'Canal',
                         duration=45 * 60) is False
    assert _add_to_queue(st, 'z', 'Artist - Piesa', set(), {}, 'Canal',
                         duration=210) is True
    assert len(st.queue) == 1, st.queue


def test_autoplay_forwards_the_filter_fields():
    """Filtrele exista, dar degeaba daca apelantul nu le da VALORILE.

    Verificarea pe nume de kwarg trecea si daca se transmitea `duration=None`:
    filtrul de durata sare peste None, deci un live de 45 de minute din API ar
    intra in coada oricum. Astea conduc functiile reale.
    """
    import asyncio

    from music import autoplay
    from music.state import GuildState

    live = {'id': 'L', 'title': 'Artist - Live acum', 'channel': 'Canal',
            'duration': 45 * 60, 'live_status': 'is_live'}
    ok = {'id': 'B', 'title': 'Alt Artist - Piesa', 'channel': 'Alt Canal',
          'duration': 210, 'live_status': None}

    class _Loop:
        async def run_in_executor(self, _executor, fn):
            return fn()

    cases = (
        ('related', 'get_related_videos',
         lambda st: autoplay._try_api_related(st, _Loop(), 'x', set(), 5, {})),
        ('search', 'search_music',
         lambda st: autoplay._try_api_search(st, _Loop(), 'Artist - Piesa',
                                             set(), 5, {})),
    )
    for name, seam, call in cases:
        st = GuildState()
        st.last_title = 'Artist - Piesa'
        st.last_channel = 'Canal'
        saved = getattr(autoplay.yt_api, seam)
        setattr(autoplay.yt_api, seam, lambda *a, **k: [live, ok])
        try:
            added = asyncio.run(call(st))
        finally:
            setattr(autoplay.yt_api, seam, saved)

        assert added == 1, f'{name}: a adaugat {added} in loc de 1'
        assert [item['title'] for item in st.queue] == [ok['title']], (
            f"{name}: un live de 45 de minute a intrat in coada: {st.queue}")


# --- contabilitatea REALA a cotei -------------------------------------------
# Testele de mai sus inlocuiesc `_api_get`, deci taxarea o facea harness-ul, iar
# contorul din producție se putea sterge cu suita verde: `units_spent()` ar
# raporta 0 pe veci, `is_available()` n-ar trece niciodata plafonul, si autoplay ar
# cumpara cautari de 100 de unitati pana la 403 — pe care il logheaza ca warning si
# apoi se opreste in tacere. Astea de mai jos patrund pana la urllib.

class _FakeHTTP:
    """Inlocuieste urllib.request.urlopen, adica stratul de SUB `_api_get`."""

    # Raspunsuri per ENDPOINT: search.list si videos.list au forme diferite
    # (`id` e un dict la primul si un string la al doilea), deci un singur payload
    # pentru ambele nu poate fi realist.
    SEARCH_PAGE = {'items': [
        {'id': {'videoId': 'a'},
         'snippet': {'title': 'Artist - Piesa', 'channelTitle': 'Canal',
                     'thumbnails': {'high': {'url': 'http://t'}}}}]}
    VIDEOS_PAGE = {'items': [
        {'id': 'a',
         'contentDetails': {'duration': 'PT3M30S'},
         'statistics': {'viewCount': '10', 'likeCount': '2'},
         'snippet': {'title': 'Artist - Piesa', 'channelTitle': 'Canal',
                     'liveBroadcastContent': 'none',
                     'thumbnails': {'high': {'url': 'http://t'}}}}]}

    def __init__(self, pages=None, error=None):
        self.pages = pages or {'search': self.SEARCH_PAGE,
                               'videos': self.VIDEOS_PAGE}
        self.error = error
        self.urls = []

    def __enter__(self):
        import json
        import urllib.request

        self.saved = urllib.request.urlopen
        outer = self

        class _Resp:
            def __init__(self_inner, body):
                self_inner.body = body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return json.dumps(self_inner.body).encode()

        def fake_urlopen(req, timeout=None):
            url = getattr(req, 'full_url', str(req))
            outer.urls.append(url)
            if outer.error:
                raise outer.error
            endpoint = 'videos' if '/videos?' in url else 'search'
            return _Resp(outer.pages.get(endpoint, {'items': []}))

        urllib.request.urlopen = fake_urlopen
        self.saved_key = api.API_KEY
        api.API_KEY = 'test-key'
        api._units_spent = 0
        api._quota_day = api._utc_day()
        api._cap_logged = False
        return self

    def __exit__(self, *exc):
        import urllib.request
        urllib.request.urlopen = self.saved
        api.API_KEY = self.saved_key
        api._units_spent = 0
        api._quota_day = None
        return False


def test_a_real_search_charges_the_real_counter():
    """101 = 100 pentru search.list + 1 pentru batch-ul videos.list."""
    with _FakeHTTP() as http:
        results = api.search('ceva', max_results=1)
        assert results and results[0]['duration'] == 210, results
        assert api.units_spent() == 101, (
            f'contorul real nu taxeaza cererile: {api.units_spent()}')
        assert len(http.urls) == 2, http.urls


def test_a_failed_request_is_charged_too():
    """YouTube scade unitatile la PRIMIRE, nu la succes.

    Daca taxarea ar depinde de raspuns, o serie de erori ar consuma cota reala si
    ar lasa contorul nostru la zero — exact cazul in care plafonul e necesar.
    """
    with _FakeHTTP(error=OSError('connection reset')):
        assert api.search('ceva') == []
        assert api.units_spent() == 100, api.units_spent()


def test_the_cap_closes_after_real_requests():
    saved_cap = api.DAILY_UNIT_CAP
    with _FakeHTTP():
        api.DAILY_UNIT_CAP = 250
        try:
            api.search('prima')                # 100 + 1
            assert api.units_spent() == 101, api.units_spent()
            assert api.is_available() is True, 'sub plafon trebuie sa mearga'
            api.search('a doua')               # 202 > 250? nu; a treia trece
            api.search('a treia')              # 303 -> peste plafon
            assert api.units_spent() == 303, api.units_spent()
            assert api.is_available() is False, (
                f'plafonul nu s-a inchis la {api.units_spent()} unitati')
        finally:
            api.DAILY_UNIT_CAP = saved_cap


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
