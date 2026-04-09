"""Motor de redare: process_play, play_next, preload, trigger_radio."""
import discord
import yt_dlp
import asyncio
import os
import time
from music.config import YDL_OPTS_SEARCH, YDL_OPTS_DOWNLOAD, FFMPEG_OPTS, log
from music.config import get_opts_with_cookies, has_real_formats
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
        with yt_dlp.YoutubeDL(YDL_OPTS_SEARCH) as ydl:
            info = await _loop.run_in_executor(
                None, lambda: ydl.extract_info(next_query, download=False)
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
            dl_info = await _loop.run_in_executor(
                None, lambda: ydl_dl.extract_info(web_url, download=True)
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

            # Search cu multiple strategii pana gasim formate reale
            _CLIENT_CHAINS = [
                ('mweb,android_vr,tv', False),   # guest + PO Token + fallbacks
                ('android_vr', False),            # android_vr singur, fara cookies
                ('mweb,tv', True),                # cu cookies
            ]
            selected = None
            for clients, use_cookies in _CLIENT_CHAINS:
                search_opts = dict(YDL_OPTS_SEARCH)
                search_opts['extractor_args'] = {'youtube': f'player_client={clients}'}
                if use_cookies:
                    cookie_search, _ = get_opts_with_cookies()
                    if not cookie_search:
                        continue
                    search_opts = cookie_search
                    search_opts['extractor_args'] = {'youtube': f'player_client={clients}'}

                try:
                    with yt_dlp.YoutubeDL(search_opts) as ydl:
                        info = await _loop.run_in_executor(
                            None, lambda: ydl.extract_info(query, download=False)
                        )
                    entries = info.get('entries', [info])
                    selected = None
                    for entry in entries:
                        fmts = entry.get('formats', [])
                        real = sum(1 for f in fmts if f.get('acodec', 'none') != 'none' or
                                   (f.get('vcodec', 'none') != 'none' and 'storyboard' not in f.get('format_note', '').lower()))
                        log.info(f"[{clients}|cookies={use_cookies}] Video {entry.get('id','?')}: {len(fmts)} formats ({real} real)")
                        if is_clean(entry.get('title', ''), entry.get('duration'), state.last_title):
                            selected = entry
                            break
                    if not selected:
                        selected = entries[0]
                    if has_real_formats(selected.get('formats', [])):
                        log.info(f"Found real formats with client={clients}")
                        break
                except Exception as e:
                    log.warning(f"Search failed with client={clients}: {e}")

            if not selected:
                raise ValueError("Nu am gasit niciun rezultat")

            web_url = selected.get('webpage_url') or \
                f"https://www.youtube.com/watch?v={selected.get('id', '')}"

            for fmt in formats_to_try:
                try:
                    dl_opts = YDL_OPTS_DOWNLOAD.copy()
                    dl_opts['format'] = fmt
                    with yt_dlp.YoutubeDL(dl_opts) as ydl_dl:
                        dl_info = await _loop.run_in_executor(
                            None, lambda: ydl_dl.extract_info(web_url, download=True)
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
                    log.warning(f"Download esuat cu format '{fmt}': {e}")

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

        await asyncio.sleep(1)
        if state.autoplay or state.queue:
            play_next(ctx)
        else:
            start_timeout(ctx)
