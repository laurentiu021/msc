"""YouTube Data API v3 — search, related videos, video details.

Folosit pentru search si autoplay (mai stabil decat yt-dlp scraping).
yt-dlp ramane doar pentru download audio.
"""
import datetime
import json
import os
import re
import urllib.parse
import urllib.request

from music.config import log, env_num, BLACKLIST
from music.utils import clean_search_title

API_KEY = os.getenv('YOUTUBE_API_KEY')
_BASE = 'https://www.googleapis.com/youtube/v3'

# Costul in unitati de cota, per endpoint. Nu e o estimare: e tariful publicat de
# YouTube. Il tinem aici ca sa fie imposibil sa faci o cerere fara sa o plateasti.
UNIT_COST = {'search': 100, 'videos': 1}

# Plafonul zilnic pe care ni-l permitem. Cota gratuita e 10.000, dar autoplay e
# doar o strategie de rezerva (Mix-ul yt-dlp e primul si e gratis), deci nu are
# ce sa consume toata ziua. Fara plafon, `_api_get` primea 403 dupa epuizare, il
# loga ca warning si autoplay se oprea in tacere.
DAILY_UNIT_CAP = env_num('YOUTUBE_API_DAILY_UNITS', 4000, low=0, high=10_000)

_units_spent = 0
_quota_day = None
_cap_logged = False


def _utc_day() -> str:
    """Cota se reseteaza la miezul nopții Pacific, dar UTC e o aproximare buna."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')


def units_spent() -> int:
    """Cate unitati de cota am consumat azi (0 dupa schimbarea zilei)."""
    _roll_day()
    return _units_spent


def _roll_day() -> None:
    global _units_spent, _quota_day, _cap_logged
    today = _utc_day()
    if _quota_day != today:
        _quota_day = today
        _units_spent = 0
        _cap_logged = False


def is_available() -> bool:
    """Cheia exista SI mai avem cota pe ziua de azi."""
    global _cap_logged
    if not API_KEY:
        return False
    _roll_day()
    if _units_spent >= DAILY_UNIT_CAP:
        if not _cap_logged:
            _cap_logged = True
            log.warning(f"YouTube API: plafon zilnic atins ({_units_spent}/"
                        f"{DAILY_UNIT_CAP} unitati). Autoplay merge doar pe yt-dlp.")
        return False
    return True


def _api_get(endpoint: str, params: dict) -> dict | None:
    """GET la YouTube Data API, contorizand cota consumata.

    Cererea e taxata chiar daca raspunsul e o eroare: YouTube scade unitatile
    la primire, nu la succes.
    """
    global _units_spent
    cost = UNIT_COST[endpoint]        # KeyError deliberat: un endpoint nou trebuie taxat
    _roll_day()
    _units_spent += cost
    params['key'] = API_KEY
    url = f"{_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    # Enumerate, nu `Exception`: urllib ridica URLError/HTTPError (ambele OSError)
    # si json.loads ridica ValueError. Un TypeError din construirea parametrilor de
    # mai sus e un defect AL NOSTRU si trebuie sa se vada, nu sa arate ca o pana
    # de rețea care se rezolva singura.
    except (OSError, ValueError) as e:
        log.warning(f"YouTube API error ({endpoint}, {cost}u): {e}")
        return None

def search(query: str, max_results: int = 5) -> list[dict]:
    """Cauta pe YouTube. Returneaza lista de {id, title, channel, duration, thumbnail}.
    Costa 100 unitati per request (100 search-uri/zi cu free tier).
    """
    data = _api_get('search', {
        'part': 'snippet',
        'q': query,
        'type': 'video',
        'maxResults': max_results,
        'videoCategoryId': '10',  # Music category
    })
    if not data:
        return []

    video_ids = []
    results = []
    for item in data.get('items', []):
        vid_id = item['id'].get('videoId')
        if not vid_id:
            continue
        snippet = item.get('snippet', {})
        results.append({
            'id': vid_id,
            'title': snippet.get('title', ''),
            'channel': snippet.get('channelTitle', ''),
            'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
        })
        video_ids.append(vid_id)

    # Fetch durations in batch (1 unit — basically free)
    if video_ids:
        details = get_video_details(video_ids)
        for r in results:
            d = details.get(r['id'], {})
            r['duration'] = d.get('duration', 0)
            r['views'] = d.get('views', 0)
            r['likes'] = d.get('likes', 0)
            r['live_status'] = d.get('live_status')
            if d.get('thumbnail'):
                r['thumbnail'] = d['thumbnail']

    return results


def search_music(query: str, max_results: int = 5) -> list[dict]:
    """Search fara category filter (fallback daca Music category da 0 results)."""
    # Try music category first
    results = search(query, max_results)
    if results:
        return results

    # Fallback: search fara category filter
    data = _api_get('search', {
        'part': 'snippet',
        'q': query,
        'type': 'video',
        'maxResults': max_results,
    })
    if not data:
        return []

    video_ids = []
    results = []
    for item in data.get('items', []):
        vid_id = item['id'].get('videoId')
        if not vid_id:
            continue
        snippet = item.get('snippet', {})
        results.append({
            'id': vid_id,
            'title': snippet.get('title', ''),
            'channel': snippet.get('channelTitle', ''),
            'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
        })
        video_ids.append(vid_id)

    if video_ids:
        details = get_video_details(video_ids)
        for r in results:
            d = details.get(r['id'], {})
            r['duration'] = d.get('duration', 0)
            r['views'] = d.get('views', 0)
            r['likes'] = d.get('likes', 0)
            r['live_status'] = d.get('live_status')
            if d.get('thumbnail'):
                r['thumbnail'] = d['thumbnail']

    return results

def get_video_details(video_ids: list[str]) -> dict:
    """Detalii video: durata, views, likes, thumbnail HD.
    Costa 1 unitate per request (max 50 IDs per batch).
    """
    if not video_ids:
        return {}
    data = _api_get('videos', {
        'part': 'contentDetails,statistics,snippet',
        'id': ','.join(video_ids[:50]),
    })
    if not data:
        return {}

    result = {}
    for item in data.get('items', []):
        vid_id = item['id']
        cd = item.get('contentDetails', {})
        stats = item.get('statistics', {})
        snippet = item.get('snippet', {})

        # Parse ISO 8601 duration (PT4M33S -> 273)
        duration = _parse_duration(cd.get('duration', ''))

        # Best thumbnail
        thumbs = snippet.get('thumbnails', {})
        thumb = (thumbs.get('maxres') or thumbs.get('high') or
                 thumbs.get('medium') or thumbs.get('default') or {}).get('url', '')

        # liveBroadcastContent: 'none' | 'live' | 'upcoming'. Il traducem in
        # vocabularul yt-dlp, ca filtrele din _add_to_queue sa aiba o singura
        # forma de verificat, indiferent de unde vine piesa.
        live_map = {'live': 'is_live', 'upcoming': 'is_upcoming'}
        live_status = live_map.get(snippet.get('liveBroadcastContent'))

        result[vid_id] = {
            'duration': duration,
            'views': int(stats.get('viewCount', 0)),
            'likes': int(stats.get('likeCount', 0)),
            'channel': snippet.get('channelTitle', ''),
            'title': snippet.get('title', ''),
            'thumbnail': thumb,
            'live_status': live_status,
        }
    return result


def get_related_videos(video_id: str, max_results: int = 15, *,
                       title: str = '', channel: str = '',
                       needed: int | None = None) -> list[dict]:
    """Video-uri similare, prin search pe canal (diversitate) apoi pe titlu.

    `title` si `channel` se primesc de la apelant cand le are deja. Botul le are
    mereu: sunt `state.last_title` si `state.last_channel` pentru exact acest
    videoclip. Fara ele, functia cumpara cu o cerere ceva ce era deja in memorie.

    `needed` e cate piese lipsesc de fapt. A doua cautare se facea cand
    `len(results) < max_results`, iar apelantul cerea 20: o pagina nu da aproape
    niciodata 20 de supravietuitori ai filtrelor, deci a doua cerere de 100 de
    unitati pornea aproape mereu, chiar cand prima adusese destul.
    """
    video_title = title
    if not video_title and not channel:
        details = get_video_details([video_id])          # 1 unitate
        info = details.get(video_id, {})
        channel = info.get('channel', '')
        video_title = info.get('title', '')

    if not video_title and not channel:
        return []

    enough = max(1, needed if needed else max_results)
    clean_title = clean_search_title(video_title) if video_title else ''
    results = []
    seen_ids = {video_id}

    # Strategy 1: search by channel/artist name (diverse songs by same artist)
    if channel:
        data = _api_get('search', {
            'part': 'snippet',
            'q': channel,
            'type': 'video',
            'maxResults': max_results,
            'videoCategoryId': '10',
        })
        if data:
            for item in data.get('items', []):
                vid_id = item['id'].get('videoId')
                snippet = item.get('snippet', {})
                if not vid_id or vid_id in seen_ids:
                    continue
                t = snippet.get('title', '')
                if any(w in t.lower() for w in BLACKLIST):
                    continue
                # Skip results that are too similar to current song
                if clean_title and _titles_too_similar(clean_title, t):
                    continue
                results.append({
                    'id': vid_id,
                    'title': t,
                    'channel': snippet.get('channelTitle', ''),
                    'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                })
                seen_ids.add(vid_id)

    # Strategy 2: search by cleaned title if not enough results
    if len(results) < enough and clean_title:
        data = _api_get('search', {
            'part': 'snippet',
            'q': clean_title,
            'type': 'video',
            'maxResults': max_results,
            'videoCategoryId': '10',
        })
        if data:
            for item in data.get('items', []):
                vid_id = item['id'].get('videoId')
                snippet = item.get('snippet', {})
                if not vid_id or vid_id in seen_ids:
                    continue
                t = snippet.get('title', '')
                if any(w in t.lower() for w in BLACKLIST):
                    continue
                if _titles_too_similar(clean_title, t):
                    continue
                results.append({
                    'id': vid_id,
                    'title': t,
                    'channel': snippet.get('channelTitle', ''),
                    'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                })
                seen_ids.add(vid_id)

    results = results[:max_results]

    # O singura cerere batch (1 unitate) pentru durata si starea de live. Fara
    # ea, `search` nu intoarce nimic despre durata, deci filtrele de durata si de
    # live din _add_to_queue nu rulau niciodata pe rezultatele API: live-urile si
    # colajele de 40 de minute ajungeau in coada, iar abia match_filter le
    # refuza la descarcare, in tacere.
    if results:
        details = get_video_details([r['id'] for r in results])
        for r in results:
            d = details.get(r['id'], {})
            r['duration'] = d.get('duration', 0)
            r['live_status'] = d.get('live_status')
    return results


def _song_part(title: str) -> str:
    """Partea de titlu de dupa numele artistului, cand exista un separator."""
    cleaned = clean_search_title(title)
    if ' - ' in cleaned:
        return cleaned.split(' - ', 1)[1]
    return cleaned


def _titles_too_similar(title_a: str, title_b: str) -> bool:
    """Aceeasi piesa in alta versiune? Comparam PIESA, nu artistul.

    Inainte se comparau titlurile intregi, deci doua piese diferite ale
    aceluiasi artist puteau depasi pragul doar din numele lui: pentru un artist
    din trei cuvinte, 3 tokeni comuni din 4 dau 0.75 si a doua piesa era
    respinsa. Radio-ul rămânea astfel fara candidati exact la artistii pe care
    ii ascultai.
    """
    a = _song_part(title_a).lower().split()
    b = _song_part(title_b).lower().split()
    if not a or not b:
        return False
    common = set(a) & set(b)
    shorter = min(len(a), len(b))
    return shorter > 0 and len(common) / shorter >= 0.7


def _parse_duration(iso: str) -> int:
    """PT4M33S -> 273 seconds."""
    if not iso:
        return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + s
