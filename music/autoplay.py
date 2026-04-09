"""Logica autoplay: YouTube API (preferat) + yt-dlp Mix (fallback)."""
import re
import yt_dlp
from music.config import YDL_OPTS_SEARCH, BLACKLIST, log
from music.state import GuildState
from music import youtube_api as yt_api


def _extract_video_id(url: str) -> str | None:
    if 'v=' in url:
        return url.split('v=')[-1].split('&')[0]
    if 'youtu.be/' in url:
        return url.split('youtu.be/')[-1].split('?')[0]
    return None


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
    for h in state.history:
        vid = _extract_video_id(h.get('url') or '')
        if vid:
            skip_ids.add(vid)
    for item in state.queue:
        vid = _extract_video_id(item.get('query', ''))
        if vid:
            skip_ids.add(vid)

    added = 0

    # Strategy 1: YouTube API related videos
    if yt_api.is_available() and added < needed:
        added += await _try_api_related(state, bot_loop, origin_id, skip_ids, needed - added)

    # Strategy 2: yt-dlp Mix
    if added < needed:
        added += await _try_ytdlp_mix(state, bot_loop, origin_id, skip_ids, needed - added)

    # Strategy 3: YouTube API search
    if yt_api.is_available() and added < needed:
        added += await _try_api_search(state, bot_loop, state.last_title, skip_ids, needed - added)

    # Strategy 4: yt-dlp search
    if added < needed:
        added += await _try_ytdlp_search(state, bot_loop, state.last_title, skip_ids, needed - added)

    if added == 0:
        log.warning("Autoplay: 0 piese gasite din toate strategiile")
    else:
        log.info(f"Autoplay: total +{added} piese (coada: {len(state.queue)})")


def _add_to_queue(state, vid_id, title, skip_ids) -> bool:
    """Adauga un video in coada daca trece filtrele."""
    if not vid_id or vid_id in skip_ids:
        return False
    if any(w in title.lower() for w in BLACKLIST):
        return False
    state.queue.append({
        'query': f"https://www.youtube.com/watch?v={vid_id}",
        'title': title or 'Autoplay'
    })
    skip_ids.add(vid_id)
    return True


async def _try_api_related(state, bot_loop, origin_id, skip_ids, needed):
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
            if _add_to_queue(state, r['id'], r['title'], skip_ids):
                added += 1
        if added:
            log.info(f"Autoplay API related: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay API related failed: {e}")
        return 0


async def _try_ytdlp_mix(state, bot_loop, origin_id, skip_ids, needed):
    """yt-dlp: YouTube Mix (RD playlist). Gratis dar instabil."""
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
            if _add_to_queue(state, e.get('id', ''), e.get('title', ''), skip_ids):
                added += 1
        if added:
            log.info(f"Autoplay Mix: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay Mix failed: {e}")
        return 0


async def _try_api_search(state, bot_loop, title, skip_ids, needed):
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
            if _add_to_queue(state, r['id'], r['title'], skip_ids):
                added += 1
        if added:
            log.info(f"Autoplay API search: +{added}")
        return added
    except Exception as e:
        log.warning(f"Autoplay API search failed: {e}")
        return 0


async def _try_ytdlp_search(state, bot_loop, title, skip_ids, needed):
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
            if _add_to_queue(state, e.get('id', ''), e.get('title', ''), skip_ids):
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
