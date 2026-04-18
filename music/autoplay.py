"""Logica autoplay: YouTube Mix (preferat) + fallback-uri."""
import re
import yt_dlp
from music.config import YDL_OPTS_SEARCH, BLACKLIST, log
from music.state import GuildState
from music import youtube_api as yt_api


# Cat de multe piese de la acelasi artist sunt permise in coada/history
MAX_SAME_ARTIST = 2


def _extract_video_id(url: str) -> str | None:
    if 'v=' in url:
        return url.split('v=')[-1].split('&')[0]
    if 'youtu.be/' in url:
        return url.split('youtu.be/')[-1].split('?')[0]
    return None


def _artist_from_title(title: str) -> str:
    """Extrage posibil nume de artist din titlu (partea dinainte de '-')."""
    if ' - ' in title:
        return title.split(' - ', 1)[0].strip().lower()
    return ''


async def prefill_autoplay_queue(state: GuildState, bot_loop, target: int = 6):
    """Populeaza coada pana la target piese.
    
    Strategii in ordine:
    1. YouTube API related videos (1 request = 100 units, dar stabil)
    2. yt-dlp YouTube Mix (RD playlist, gratis dar instabil)
    3. YouTube API search fallback (bazat pe titlu)
    4. yt-dlp search fallback (ultima sansa)
    """
    needed = target - len(state.queue)
    if needed <= 0:
        return

    origin_url = state.history[0]['url'] if state.history else state.last_url
    if not origin_url:
        log.warning("Autoplay: no origin URL")
        return

    origin_id = _extract_video_id(origin_url)
    if not origin_id:
        log.warning(f"Autoplay: can't extract ID from {origin_url}")
        return

    skip_ids = set()
    artist_counts: dict[str, int] = {}
    for h in state.history:
        vid = _extract_video_id(h.get('url') or '')
        if vid:
            skip_ids.add(vid)
        artist_key = _artist_from_title(h.get('title') or '').lower()
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
    for item in state.queue:
        vid = _extract_video_id(item.get('query', ''))
        if vid:
            skip_ids.add(vid)
        artist_key = _artist_from_title(item.get('title') or '').lower()
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1

    added = 0

    # Strategy 1: YouTube Mix (RD playlist) — radio curat, stil pastrat, artisti diversi
    if added < needed:
        added += await _try_ytdlp_mix(state, bot_loop, origin_id, skip_ids, needed - added, artist_counts)

    # Strategy 2: YouTube API related (search dupa artist — mai putin divers)
    if yt_api.is_available() and added < needed:
        added += await _try_api_related(state, bot_loop, origin_id, skip_ids, needed - added, artist_counts)

    # Strategy 3: YouTube API search (dupa titlu)
    if yt_api.is_available() and added < needed:
        added += await _try_api_search(state, bot_loop, state.last_title, skip_ids, needed - added, artist_counts)

    # Strategy 4: yt-dlp search (ultima sansa)
    if added < needed:
        added += await _try_ytdlp_search(state, bot_loop, state.last_title, skip_ids, needed - added, artist_counts)

    if added == 0:
        log.warning("Autoplay: 0 piese gasite din toate strategiile")
    else:
        log.info(f"Autoplay: total +{added} piese (coada: {len(state.queue)})")


def _add_to_queue(state, vid_id, title, skip_ids, artist_counts=None, channel='') -> bool:
    """Adauga un video in coada daca trece filtrele."""
    if not vid_id or vid_id in skip_ids:
        return False
    if any(w in title.lower() for w in BLACKLIST):
        return False
    # Limit same-artist pieces (use channel name or title prefix)
    if artist_counts is not None:
        artist_key = (channel or _artist_from_title(title) or '').strip().lower()
        if artist_key:
            if artist_counts.get(artist_key, 0) >= MAX_SAME_ARTIST:
                return False
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
    state.queue.append({
        'query': f"https://www.youtube.com/watch?v={vid_id}",
        'title': title or 'Autoplay'
    })
    skip_ids.add(vid_id)
    return True


async def _try_api_related(state, bot_loop, origin_id, skip_ids, needed, artist_counts=None):
    """YouTube API: related videos. Stabil, costa 100 units."""
    try:
        results = await bot_loop.run_in_executor(
            None, lambda: yt_api.get_related_videos(origin_id, max_results=20)
        )
        log.info(f"Autoplay API related: {len(results)} results")
        added = 0
        for r in results:
            if added >= needed:
                break
            if _add_to_queue(state, r['id'], r['title'], skip_ids,
                             artist_counts, r.get('channel', '')):
                added += 1
        if added:
            log.info(f"Autoplay API related: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay API related failed: {e}")
        return 0


async def _try_ytdlp_mix(state, bot_loop, origin_id, skip_ids, needed, artist_counts=None):
    """yt-dlp: YouTube Mix (RD playlist). Radio curat, stil pastrat."""
    mix_url = f"https://www.youtube.com/watch?v={origin_id}&list=RD{origin_id}"
    opts = YDL_OPTS_SEARCH.copy()
    opts['noplaylist'] = False
    opts['extract_flat'] = True
    opts['playlistend'] = 50
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await bot_loop.run_in_executor(
                None, lambda: ydl.extract_info(mix_url, download=False)
            )
        entries = info.get('entries') or []
        log.info(f"Autoplay Mix: {len(entries)} entries")
        added = 0
        for e in entries:
            if added >= needed:
                break
            # Mix entries may have 'channel' or 'uploader'
            channel = e.get('channel') or e.get('uploader') or ''
            if _add_to_queue(state, e.get('id', ''), e.get('title', ''),
                             skip_ids, artist_counts, channel):
                added += 1
        if added:
            log.info(f"Autoplay Mix: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay Mix failed: {e}")
        return 0


async def _try_api_search(state, bot_loop, title, skip_ids, needed, artist_counts=None):
    """YouTube API: search bazat pe titlu. Costa 100 units."""
    if not title:
        return 0
    clean = _clean_title(title)
    try:
        results = await bot_loop.run_in_executor(
            None, lambda: yt_api.search_music(f"{clean}", max_results=10)
        )
        log.info(f"Autoplay API search: {len(results)} for '{clean[:30]}'")
        added = 0
        for r in results:
            if added >= needed:
                break
            if _add_to_queue(state, r['id'], r['title'], skip_ids,
                             artist_counts, r.get('channel', '')):
                added += 1
        if added:
            log.info(f"Autoplay API search: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay API search failed: {e}")
        return 0


async def _try_ytdlp_search(state, bot_loop, title, skip_ids, needed, artist_counts=None):
    """yt-dlp: search fallback. Ultima sansa."""
    if not title:
        return 0
    clean = _clean_title(title)
    opts = YDL_OPTS_SEARCH.copy()
    opts['extract_flat'] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await bot_loop.run_in_executor(
                None, lambda: ydl.extract_info(f"ytsearch10:{clean} music", download=False)
            )
        entries = info.get('entries') or []
        log.info(f"Autoplay yt-dlp search: {len(entries)} for '{clean[:30]}'")
        added = 0
        for e in entries:
            if added >= needed:
                break
            channel = e.get('channel') or e.get('uploader') or ''
            if _add_to_queue(state, e.get('id', ''), e.get('title', ''),
                             skip_ids, artist_counts, channel):
                added += 1
        if added:
            log.info(f"Autoplay yt-dlp search: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay yt-dlp search failed: {e}")
        return 0


def _clean_title(title: str) -> str:
    """Curata titlul de tag-uri inutile pentru search."""
    clean = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean = re.sub(r'\b(official|video|audio|lyrics|hd|hq|4k|mv|music\s*video)\b',
                   '', clean, flags=re.I).strip()
    return clean if len(clean) >= 3 else title
