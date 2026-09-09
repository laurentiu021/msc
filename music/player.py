"""Motor de redare: process_play, play_next, preload, trigger_radio."""
import discord
import yt_dlp
import asyncio
import os
import time
import random
from music.config import YDL_OPTS_SEARCH, YDL_OPTS_DOWNLOAD, FFMPEG_OPTS, log
from music.config import (get_opts_with_cookies, has_real_formats,
                          count_real_formats, yt_client_args)
from music.config import (
    YT_REQUEST_MIN_INTERVAL_SEC,
    YT_REQUEST_MAX_INTERVAL_SEC,
)
from music.state import get_state
from music.utils import is_clean, cleanup_file
from music.autoplay import prefill_autoplay_queue
from music.errors import diagnose_error
from music import youtube_api as yt_api

# Referinte setate din bot.py la startup
bot = None
update_player_ui = None
start_timeout = None
cancel_timeout = None
_loop = None
_YT_REQ_LOCK = asyncio.Lock()
_NEXT_YT_REQUEST_AT = 0.0


def _is_youtube_pressure_error(error: Exception) -> bool:
    e = str(error).lower()
    patterns = (
        "http error 429",
        "too many requests",
        "rate limit",
        "sign in to confirm",
        "forbidden",
        "http error 403",
    )
    return any(p in e for p in patterns)


async def _wait_for_youtube_slot():
    global _NEXT_YT_REQUEST_AT
    async with _YT_REQ_LOCK:
        now = time.time()
        wait_for = _NEXT_YT_REQUEST_AT - now
        if wait_for > 0:
            log.info(f"YouTube throttling active: waiting {wait_for:.1f}s")
            await asyncio.sleep(wait_for)
        min_delay = max(0.0, YT_REQUEST_MIN_INTERVAL_SEC)
        max_delay = max(min_delay, YT_REQUEST_MAX_INTERVAL_SEC)
        _NEXT_YT_REQUEST_AT = time.time() + random.uniform(min_delay, max_delay)


async def _yt_extract_info(ydl_opts, query_or_url, download=False, stage=""):
    await _wait_for_youtube_slot()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return await _loop.run_in_executor(
            None, lambda: ydl.extract_info(query_or_url, download=download)
        )


def init(bot_ref, ui_func, start_to, cancel_to):
    global bot, update_player_ui, start_timeout, cancel_timeout, _loop
    bot = bot_ref
    update_player_ui = ui_func
    start_timeout = start_to
    cancel_timeout = cancel_to


async def trigger_radio(ctx):
    state = get_state(ctx.guild.id)
    try:
        if not state.queue:
            await prefill_autoplay_queue(state, _loop)
        if state.queue:
            next_item = state.queue.pop(0)
            await process_play(ctx, next_item['query'], is_radio=True)
        else:
            raise ValueError("Nu s-au gasit piese pentru autoplay.")
    except Exception as e:
        log.warning(f"Autoplay error (guild {ctx.guild.id}): {e}")
        state.is_loading = False
        state.autoplay = False
        try:
            await ctx.send("Autoplay s-a oprit.", delete_after=10)
        except discord.HTTPException:
            pass
        start_timeout(ctx)


async def _play_next_async(ctx):
    state = get_state(ctx.guild.id)
    next_item = None
    try:
        async with state._lock:
            vc = ctx.voice_client
            if not vc or not vc.is_connected():
                state.is_loading = False
                return
            if not state.skip_request and state.last_url:
                if state.loop_mode == 1:
                    state.queue.insert(0, {'query': state.last_url, 'title': state.last_title})
                elif state.loop_mode == 2:
                    state.queue.append({'query': state.last_url, 'title': state.last_title})
            state.skip_request = False
            if state.queue:
                cancel_timeout(ctx)
                next_item = state.queue.pop(0)

        if next_item:
            log.info(f"play_next: {next_item['title'][:40]}")
            await process_play(ctx, next_item['query'], is_radio=False)
            if state.autoplay and len(state.queue) < 6 and state.last_url:
                try:
                    await prefill_autoplay_queue(state, _loop)
                    log.info(f"Refill dupa skip: coada={len(state.queue)}")
                    await update_player_ui(ctx)
                except Exception as e:
                    log.warning(f"Prefill dupa skip esuat: {e}")
        elif state.autoplay and state.last_url:
            cancel_timeout(ctx)
            await trigger_radio(ctx)
        else:
            state.is_loading = False
            start_timeout(ctx)
    except Exception as e:
        log.error(f"play_next EROARE: {e}", exc_info=True)
        state.is_loading = False
        start_timeout(ctx)


def play_next(ctx):
    global _loop
    state = get_state(ctx.guild.id)
    state.is_loading = True
    if _loop is None:
        try:
            _loop = asyncio.get_event_loop()
        except RuntimeError:
            return
    asyncio.run_coroutine_threadsafe(_play_next_async(ctx), _loop)


async def preload_next(ctx):
    state = get_state(ctx.guild.id)
    if not state.queue or state.preloaded:
        return
    next_query = state.queue[0]['query']
    try:
        info = await _yt_extract_info(
            YDL_OPTS_SEARCH, next_query, download=False, stage="preload_search"
        )
        entries = info.get('entries', [info])
        selected = entries[0]
        for entry in entries:
            if is_clean(entry.get('title', ''), entry.get('duration'), state.last_title):
                selected = entry
                break
        web_url = selected.get('webpage_url') or \
            f"https://www.youtube.com/watch?v={selected.get('id', '')}"
        with yt_dlp.YoutubeDL(YDL_OPTS_DOWNLOAD) as ydl_dl:
            dl_info = await _yt_extract_info(
                YDL_OPTS_DOWNLOAD, web_url, download=True, stage="preload_download"
            )
            filename = ydl_dl.prepare_filename(dl_info)
            # Postprocessor-ul poate schimba extensia
            if not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                for ext in ['.opus', '.m4a', '.webm', '.mp3', '.ogg']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
        if filename and os.path.exists(filename):
            state.preloaded = {
                'query': next_query, 'filename': filename,
                'info': selected, 'web_url': web_url,
            }
            log.info(f"Preloaded: {selected.get('title', '?')[:40]}")
    except Exception as e:
        log.debug(f"Preload esuat: {e}")


async def process_play(ctx, query, is_radio=False):
    state = get_state(ctx.guild.id)
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        state.is_loading = False
        return

    if state._consecutive_errors >= 5:
        log.warning(f"5 erori consecutive, opresc (guild {ctx.guild.id})")
        state.is_loading = False
        state._consecutive_errors = 0
        state.autoplay = False
        try:
            error_type, user_msg = diagnose_error(
                state._last_notified_error or "unknown"
            )
            await ctx.send(
                f"⛔ **M-am oprit dupa 5 erori consecutive.**\n"
                f"Ultima problema detectata: *{error_type}*\n"
                f"➡️ Rezolva problema si incearca din nou cu `!play`.",
                delete_after=60,
            )
        except discord.HTTPException:
            pass
        state._last_notified_error = None
        start_timeout(ctx)
        return

    filename = None
    formats_to_try = [
        'bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'bestaudio',
        'best[protocol=m3u8_native]/best[protocol=m3u8]',
        'worstaudio',
        'best',
        'worst',
    ]

    try:
        preloaded = state.preloaded
        if preloaded and preloaded['query'] == query:
            filename = preloaded['filename']
            selected = preloaded['info']
            web_url = preloaded['web_url']
            state.preloaded = None
            log.info(f"Folosesc preloaded: {selected.get('title', '?')[:40]}")
        else:
            if preloaded:
                cleanup_file(preloaded.get('filename'), _loop)
                state.preloaded = None

            # Search cu multiple strategii pana gasim formate reale.
            # Doar clienti web, singurii pentru care bgutil poate emite PO Token.
            _COOKIE_CHAIN = [
                ('mweb', True),                   # dovedit in producție: 39 formate reale
                ('web_safari', True),             # HLS ca rezerva
            ]
            _GUEST_CHAIN = [
                ('mweb', False),
                ('web_safari', False),
            ]
            # Cookies primele, pentru ca de pe IP-ul de datacenter al Railway
            # calea de guest ajunge la 429 pe webpage -> lipsa Visitor Data ->
            # niciun GVS PO Token -> zero formate redabile. Guest ramane in
            # coada pentru cand IP-ul nu e limitat.
            _CLIENT_CHAINS = (
                _COOKIE_CHAIN + _GUEST_CHAIN
                if get_opts_with_cookies()[0] is not None
                else _GUEST_CHAIN
            )
            selected = None
            successful_client = None
            successful_cookies = False
            for clients, use_cookies in _CLIENT_CHAINS:
                search_opts = dict(YDL_OPTS_SEARCH)
                search_opts['extractor_args'] = yt_client_args(clients)
                if use_cookies:
                    cookie_search, _ = get_opts_with_cookies()
                    if not cookie_search:
                        continue
                    search_opts = cookie_search
                    search_opts['extractor_args'] = yt_client_args(clients)

                try:
                    info = await _yt_extract_info(
                        search_opts, query, download=False, stage=f"search_{clients}"
                    )
                    entries = info.get('entries', [info])
                    selected = None
                    for entry in entries:
                        fmts = entry.get('formats', [])
                        real = count_real_formats(fmts)
                        log.info(f"[{clients}|cookies={use_cookies}] Video {entry.get('id','?')}: {len(fmts)} formats ({real} real)")
                        if is_clean(entry.get('title', ''), entry.get('duration'), state.last_title):
                            selected = entry
                            break
                    if not selected:
                        selected = entries[0]
                    if has_real_formats(selected.get('formats', [])):
                        log.info(f"Found real formats with client={clients}, cookies={use_cookies}")
                        successful_client = clients
                        successful_cookies = use_cookies
                        break
                except Exception as e:
                    log.warning(f"Search failed with client={clients}: {e}")

            if not selected:
                raise ValueError("Nu am gasit niciun rezultat")

            web_url = selected.get('webpage_url') or \
                f"https://www.youtube.com/watch?v={selected.get('id', '')}"

            # Retry once with delay if 0 real formats (429 rate-limit recovery)
            if not has_real_formats(selected.get('formats', [])):
                vid_id = selected.get('id', '?')
                log.warning(f"0 real formats for {vid_id} — waiting 5s and retrying with mweb")
                await asyncio.sleep(5)
                retry_url = web_url if web_url.startswith('http') else \
                    f"https://www.youtube.com/watch?v={vid_id}"
                retry_opts = dict(YDL_OPTS_SEARCH)
                retry_opts['extractor_args'] = yt_client_args('mweb')
                retry_opts['default_search'] = None  # use URL directly
                try:
                    retry_info = await _yt_extract_info(
                        retry_opts, retry_url, download=False, stage="retry_mweb"
                    )
                    retry_fmts = retry_info.get('formats', [])
                    retry_real = count_real_formats(retry_fmts)
                    log.info(f"[retry mweb] Video {vid_id}: {len(retry_fmts)} formats ({retry_real} real)")
                    if has_real_formats(retry_fmts):
                        selected = retry_info
                        log.info(f"Retry succeeded for {vid_id}")
                    else:
                        log.warning(f"Retry also got 0 real formats for {vid_id}")
                        raise ValueError("YouTube a blocat acest video (0 formate reale)")
                except ValueError:
                    raise
                except Exception as e:
                    log.warning(f"Retry failed for {vid_id}: {e}")
                    raise ValueError("YouTube a blocat acest video (0 formate reale)")

            # Download: incearca prima data cu combinatia care a mers la search
            cookie_order = [True, False] if successful_cookies else [False, True]
            for use_cookies_dl in cookie_order:
                if filename and os.path.exists(filename):
                    break
                for fmt in formats_to_try:
                    try:
                        if use_cookies_dl:
                            _, dl_opts = get_opts_with_cookies()
                            if not dl_opts:
                                break  # no cookies available
                            dl_opts['format'] = fmt
                            if successful_client:
                                dl_opts['extractor_args'] = yt_client_args(successful_client)
                            log.info(f"Download WITH cookies, client={successful_client or 'default'}, format={fmt}")
                        else:
                            dl_opts = YDL_OPTS_DOWNLOAD.copy()
                            dl_opts['format'] = fmt
                            if successful_client:
                                dl_opts['extractor_args'] = yt_client_args(successful_client)
                        with yt_dlp.YoutubeDL(dl_opts) as ydl_dl:
                            dl_info = await _yt_extract_info(
                                dl_opts, web_url, download=True, stage=f"download_{fmt}"
                            )
                            filename = ydl_dl.prepare_filename(dl_info)
                            if not os.path.exists(filename):
                                base = os.path.splitext(filename)[0]
                                for ext in ['.opus', '.m4a', '.webm', '.mp3', '.ogg']:
                                    if os.path.exists(base + ext):
                                        filename = base + ext
                                        break
                        if filename and os.path.exists(filename):
                            break
                    except Exception as e:
                        log.warning(f"Download esuat (cookies={use_cookies_dl}, fmt='{fmt}'): {e}")

        if not filename or not os.path.exists(filename):
            raise FileNotFoundError("Niciun format nu a reusit descarcarea")

        state.last_url = web_url
        state.last_title = selected['title']
        state.last_duration = selected.get('duration', 0)
        state.last_thumbnail = selected.get('thumbnail')
        state.is_radio_now = is_radio
        state.last_channel = selected.get('channel') or selected.get('uploader', '')
        state.last_views = 0
        state.last_likes = 0
        state.history.append({'url': web_url, 'title': selected['title']})
        if len(state.history) > 20:
            state.history.pop(0)

        if not vc.is_connected():
            raise ConnectionError("Voice deconectat in timpul descarcarii.")
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.3)
        if not vc.is_connected():
            raise ConnectionError("Voice deconectat dupa stop.")

        state.last_start_time = time.time()
        state.current_file = filename
        captured_filename = filename

        def after_play(err):
            if err:
                log.error(f"Eroare redare: {err}")
            cleanup_file(captured_filename, _loop)
            play_next(ctx)

        try:
            source = await discord.FFmpegOpusAudio.from_probe(filename, **FFMPEG_OPTS)
            vc.play(source, after=after_play)
        except Exception:
            log.warning("OpusAudio esuat, fallback PCM", exc_info=True)
            vc.play(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTS), after=after_play)

        state.is_loading = False
        state._consecutive_errors = 0
        state._last_notified_error = None
        await update_player_ui(ctx, send_new=True)

        # Enrich metadata from YouTube API (async, non-blocking)
        if yt_api.is_available():
            try:
                vid_id = web_url.split('v=')[-1].split('&')[0] if 'v=' in web_url else None
                if vid_id:
                    details = await _loop.run_in_executor(
                        None, lambda: yt_api.get_video_details([vid_id])
                    )
                    d = details.get(vid_id, {})
                    if d:
                        state.last_views = d.get('views', 0)
                        state.last_likes = d.get('likes', 0)
                        if d.get('channel'):
                            state.last_channel = d['channel']
                        if d.get('thumbnail'):
                            state.last_thumbnail = d['thumbnail']
                        if d.get('duration') and not state.last_duration:
                            state.last_duration = d['duration']
                        await update_player_ui(ctx)
            except Exception:
                pass  # Non-critical, don't break playback

        if state.queue:
            _loop.create_task(preload_next(ctx))

    except Exception as e:
        log.error(f"Eroare process_play: {e}", exc_info=True)
        cleanup_file(filename, _loop)
        state.is_loading = False
        state._consecutive_errors += 1

        # Trimite mesaj user-friendly pe Discord (o singura data per tip de eroare)
        error_type, user_msg = diagnose_error(e)
        if state._last_notified_error != error_type:
            state._last_notified_error = error_type
            try:
                await ctx.send(user_msg, delete_after=60)
            except discord.HTTPException:
                pass

        await asyncio.sleep(min(2 * state._consecutive_errors, 15))
        if state.autoplay or state.queue:
            play_next(ctx)
        else:
            start_timeout(ctx)
