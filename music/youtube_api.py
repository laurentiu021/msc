"""YouTube Data API v3 — search, related videos, video details.

Folosit pentru search si autoplay (mai stabil decat yt-dlp scraping).
yt-dlp ramane doar pentru download audio.
"""
import os
import urllib.request
import urllib.parse
import json
import re
from music.config import log, BLACKLIST

API_KEY = os.getenv('YOUTUBE_API_KEY')
_BASE = 'https://www.googleapis.com/youtube/v3'


def is_available() -> bool:
    """Verifica daca API key-ul e setat."""
    return bool(API_KEY)


def _api_get(endpoint: str, params: dict) -> dict | None:
    """GET request la YouTube Data API."""
    params['key'] = API_KEY
    url = f"{_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"YouTube API error ({endpoint}): {e}")
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

        result[vid_id] = {
            'duration': duration,
            'views': int(stats.get('viewCount', 0)),
            'likes': int(stats.get('likeCount', 0)),
            'channel': snippet.get('channelTitle', ''),
            'thumbnail': thumb,
        }
    return result


def get_related_videos(video_id: str, max_results: int = 15) -> list[dict]:
    """Gaseste video-uri similare. Costa 100 unitati.
    Folosit ca fallback pentru autoplay cand Mix-ul esueaza.
    """
    data = _api_get('search', {
        'part': 'snippet',
        'relatedToVideoId': video_id,
        'type': 'video',
        'maxResults': max_results,
    })
    if not data:
        return []

    results = []
    for item in data.get('items', []):
        vid_id = item['id'].get('videoId')
        snippet = item.get('snippet', {})
        if not vid_id:
            continue
        title = snippet.get('title', '')
        if any(w in title.lower() for w in BLACKLIST):
            continue
        results.append({
            'id': vid_id,
            'title': title,
            'channel': snippet.get('channelTitle', ''),
            'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
        })
    return results


def get_playlist_items(playlist_id: str, max_results: int = 50) -> list[dict]:
    """Extrage video-uri dintr-un playlist. Costa 1 unitate.
    Folosit pentru YouTube Mix (RD playlists) si playlists normale.
    """
    data = _api_get('playlistItems', {
        'part': 'snippet',
        'playlistId': playlist_id,
        'maxResults': min(max_results, 50),
    })
    if not data:
        return []

    results = []
    for item in data.get('items', []):
        snippet = item.get('snippet', {})
        vid_id = snippet.get('resourceId', {}).get('videoId')
        if not vid_id:
            continue
        title = snippet.get('title', '')
        if title in ('Deleted video', 'Private video'):
            continue
        results.append({
            'id': vid_id,
            'title': title,
            'channel': snippet.get('videoOwnerChannelTitle', ''),
            'thumbnail': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
        })
    return results


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
