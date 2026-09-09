"""Logica autoplay: YouTube Mix (preferat) + fallback-uri."""
import re
from music.config import (BLACKLIST, MAX_TRACK_SECONDS, MIN_TRACK_SECONDS,
                          cookies_available, log, make_search_opts)
from music.state import GuildState
from music.utils import clean_search_title
from music import youtube_api as yt_api
from music import ytdlp


# Cat de multe piese de la acelasi artist sunt permise in coada/history
MAX_SAME_ARTIST = 2


def _extract_video_id(url: str) -> str | None:
    if 'v=' in url:
        return url.split('v=')[-1].split('&')[0]
    if 'youtu.be/' in url:
        return url.split('youtu.be/')[-1].split('?')[0]
    return None


def _artist_from_title(title) -> str:
    """Extrage posibil nume de artist din titlu (partea dinainte de '-')."""
    title = str(title or '')
    if ' - ' in title:
        return title.split(' - ', 1)[0].strip().lower()
    return ''


def artist_key(title, channel='') -> str:
    """Cheia de diversitate, folosita IDENTIC la numarare si la verificare.

    Inainte, seed-ul folosea prefixul titlului si verificarea folosea canalul.
    Cele doua spatii de nume nu se intersectau: plafonul se scurgea (numarul
    pe canal se re-descoperea din titlu la refill-ul urmator) si totodata
    infometa coada, fiindca un canal de label colapsa 50 de artisti pe o cheie.
    """
    from_title = _artist_from_title(title)
    if from_title:
        return from_title
    name = str(channel or '').strip().lower()
    for suffix in (' - topic', 'vevo', ' official', ' music'):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


async def prefill_autoplay_queue(state: GuildState, bot_loop, target: int = 12):
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

    # state.history[0] e cea mai VECHE intrare pastrata (append la coada,
    # trim din fata), deci radio-ul rămânea pinuit pe prima piesa a sesiunii si
    # apoi trage cu 20 in urma. Seed-ul trebuie sa fie piesa curenta.
    origin_url = state.last_url or (state.history[-1]['url'] if state.history else None)
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
        key = artist_key(h.get('title'), h.get('channel', ''))
        if key:
            artist_counts[key] = artist_counts.get(key, 0) + 1
    for item in state.queue:
        vid = _extract_video_id(item.get('query', ''))
        if vid:
            skip_ids.add(vid)
        key = artist_key(item.get('title'), item.get('channel', ''))
        if key:
            artist_counts[key] = artist_counts.get(key, 0) + 1

    added = 0

    # Strategy 1: YouTube Mix (RD playlist) — radio curat, stil pastrat, artisti diversi
    if added < needed:
        added += await _try_ytdlp_mix(state, bot_loop, origin_id, skip_ids, needed - added, artist_counts)

    # Strategiile de API costa cota (100 unitati fiecare cerere de search, plus
    # detaliile videoclipului). Le oprim dupa prima care nu aduce nimic: un
    # prefill eșuat consuma sute de unitati din cele 10.000 pe zi, degeaba.
    api_spent = 0
    if yt_api.is_available() and added < needed:
        got = await _try_api_related(state, bot_loop, origin_id, skip_ids,
                                     needed - added, artist_counts)
        added += got
        api_spent += 1
        if got == 0:
            log.info("Autoplay: API related n-a adus nimic, nu mai cheltui cota")

    # Strategy 3: YouTube API search (dupa titlu)
    if yt_api.is_available() and added < needed and api_spent < 2 and added > 0:
        added += await _try_api_search(state, bot_loop, state.last_title, skip_ids,
                                       needed - added, artist_counts)

    # Strategy 4: yt-dlp search (ultima sansa)
    if added < needed:
        added += await _try_ytdlp_search(state, bot_loop, state.last_title, skip_ids, needed - added, artist_counts)

    if added == 0:
        log.warning("Autoplay: 0 piese gasite din toate strategiile")
    else:
        log.info(f"Autoplay: total +{added} piese (coada: {len(state.queue)})")


def _add_to_queue(state, vid_id, title, skip_ids, artist_counts=None, channel='',
                  duration=None, live_status=None) -> bool:
    """Adauga un video in coada daca trece filtrele."""
    if not vid_id or vid_id in skip_ids:
        return False
    title_text = str(title or '')
    if not title_text:
        # Fara titlu nu putem nici filtra, nici afisa elementul.
        return False
    if any(w in title_text.lower() for w in BLACKLIST):
        return False
    # Live si durate absurde, respinse aici si nu doar la descarcare: altfel
    # ajungeau in coada si abia process_play le refuza, dupa ce pierdea cereri.
    if live_status in ('is_live', 'is_upcoming', 'post_live'):
        return False
    if duration and (duration > MAX_TRACK_SECONDS or duration < MIN_TRACK_SECONDS):
        return False
    if artist_counts is not None:
        key = artist_key(title_text, channel)
        if key:
            if artist_counts.get(key, 0) >= MAX_SAME_ARTIST:
                return False
            artist_counts[key] = artist_counts.get(key, 0) + 1
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
    # Cookies + throttle: calea asta ocolea complet limitatorul de rata si mergea
    # doar ca guest, adica exact combinatia care nu functioneaza de pe Railway.
    opts = make_search_opts(with_cookies=cookies_available(), noplaylist=False,
                            extract_flat=True, playlistend=50)
    try:
        info = await ytdlp.extract(opts, mix_url, loop=bot_loop, stage='autoplay_mix')
        entries = info.get('entries') or []
        log.info(f"Autoplay Mix: {len(entries)} entries")
        added = 0
        for e in entries:
            if added >= needed:
                break
            # Mix entries may have 'channel' or 'uploader'
            channel = e.get('channel') or e.get('uploader') or ''
            if _add_to_queue(state, e.get('id', ''), e.get('title'),
                             skip_ids, artist_counts, channel,
                             duration=e.get('duration'),
                             live_status=e.get('live_status')):
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
    opts = make_search_opts(with_cookies=cookies_available(), extract_flat=True)
    try:
        info = await ytdlp.extract(opts, f"ytsearch10:{clean} music",
                                   loop=bot_loop, stage='autoplay_search')
        entries = info.get('entries') or []
        log.info(f"Autoplay yt-dlp search: {len(entries)} for '{clean[:30]}'")
        added = 0
        for e in entries:
            if added >= needed:
                break
            channel = e.get('channel') or e.get('uploader') or ''
            if _add_to_queue(state, e.get('id', ''), e.get('title'),
                             skip_ids, artist_counts, channel,
                             duration=e.get('duration'),
                             live_status=e.get('live_status')):
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
