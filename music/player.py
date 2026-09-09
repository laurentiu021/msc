"""Motor de redare: process_play, play_next, trigger_radio."""
import discord
import asyncio
import os
import time
from music.config import FFMPEG_OPTS, log
from music.config import (cookies_available, count_real_formats,
                          has_real_formats, make_download_opts,
                          make_search_opts, yt_client_args, WEB_CLIENTS)
from music import ytdlp
from music.state import get_state
from music.utils import is_clean, cleanup_file, item_title
from music.autoplay import prefill_autoplay_queue
from music.errors import diagnose_error
from music import youtube_api as yt_api

# Lanturile de clienti, la nivel de modul ca sa fie verificabile de teste.
# Cerem ambii clienti in ACEEASI cerere: yt-dlp cumuleaza formatele, deci pool-ul
# e mult mai mare pe acelasi numar de cereri. Masurat in producție: mweb singur a
# dat 5 formate / 1 redabil, perechea a dat 40 / 13. Un singur format redabil e o
# marja prea subtire pentru redare.
COOKIE_CHAIN = [(WEB_CLIENTS, True)]
GUEST_CHAIN = [(WEB_CLIENTS, False)]

# Cat sta intrerupatorul inchis dupa 5 erori consecutive.
BREAKER_COOLDOWN_SEC = 900


def bump_play_generation(state) -> int:
    """Invalideaza callback-ul after_play al piesei curente.

    VoiceClient.stop() declanseaza ALWAYS callback-ul, deci fara asta orice
    oprire deliberata (seek, nplay, inlocuire) avansa coada si stergea
    fisierul care tocmai pornea.
    """
    state.play_generation += 1
    return state.play_generation


def make_after_play(ctx, state, filename):
    """Callback de sfarsit de piesa, valid doar pentru generatia curenta."""
    generation = bump_play_generation(state)

    def after_play(err):
        if err:
            log.error(f"Eroare redare: {err}")
        if state.play_generation != generation:
            # Oprire deliberata: altcineva preia redarea si fisierul.
            return
        if state.loop_mode != 1:
            cleanup_file(filename, _loop)
        play_next(ctx)

    return after_play

# Referinte setate din bot.py la startup
bot = None
update_player_ui = None
start_timeout = None
cancel_timeout = None
_loop = None
async def _yt_extract_info(ydl_opts, query_or_url, download=False, stage=""):
    """Delegat catre music.ytdlp: un singur throttle pentru tot botul."""
    return await ytdlp.extract(ydl_opts, query_or_url, download=download,
                               loop=_loop, stage=stage)


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
            log.info(f"play_next: {item_title(next_item, 40)}")
            await process_play(ctx, next_item['query'], is_radio=False)
            if state.autoplay and len(state.queue) < 3 and state.last_url:
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
        # Pauza reala: fara ea, timer-ul de 24/7 punea autoplay=True dupa 60s si
        # ciclul de 5 erori repornea la infinit, batand un IP deja limitat.
        state.breaker_until = time.time() + BREAKER_COOLDOWN_SEC
        try:
            # diagnose_error primeste textul BRUT, nu cheia proprie de tip:
            # cheia ("cookies", "ratelimit", ...) nu se re-mapeaza pe ea insasi
            # si raportarea ieseau mereu "unknown".
            error_type, _ = diagnose_error(state.last_raw_error or "unknown")
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

    state.is_loading = True
    failure = None
    filename = None
    formats_to_try = [
        'bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'best[protocol=m3u8_native]/best[protocol=m3u8]',
    ]

    try:
        # Repetare (loop pe piesa) sau re-adaugarea aceluiasi URL: fisierul e
        # deja pe disc, deci nu mai cerem nimic de la YouTube.
        if (query and query == state.last_url and state.current_file
                and os.path.exists(state.current_file)):
            log.info("Refolosesc fisierul deja descarcat (loop / acelasi URL)")
            filename = state.current_file
            reused = True
            web_url = state.last_url
            selected = {
                'title': state.last_title,
                'duration': state.last_duration,
                'thumbnail': state.last_thumbnail,
                'channel': state.last_channel,
                'webpage_url': state.last_url,
            }
        else:
            reused = False

        if state.preloaded:
            # Nu mai preîncarcam nimic; daca a rămas ceva dintr-o versiune veche
            # a botului, il curatam.
            cleanup_file(state.preloaded.get('filename'), _loop)
            state.preloaded = None

        if not reused:
            # Cookies primele, pentru ca de pe IP-ul de datacenter al Railway
            # calea de guest ajunge la 429 pe webpage -> lipsa Visitor Data ->
            # niciun GVS PO Token -> zero formate redabile. Guest ramane in
            # coada pentru cand IP-ul nu e limitat.
            _CLIENT_CHAINS = (
                COOKIE_CHAIN + GUEST_CHAIN if cookies_available() else GUEST_CHAIN
            )
            selected = None
            successful_client = None
            successful_cookies = False
            for clients, use_cookies in _CLIENT_CHAINS:
                label = '+'.join(clients)
                if use_cookies and not cookies_available():
                    continue
                search_opts = make_search_opts(
                    with_cookies=use_cookies,
                    extractor_args=yt_client_args(*clients),
                )

                try:
                    info = await _yt_extract_info(
                        search_opts, query, download=False, stage=f"search_{label}"
                    )
                    entries = info.get('entries', [info])
                    selected = None
                    for entry in entries:
                        fmts = entry.get('formats', [])
                        real = count_real_formats(fmts)
                        log.info(f"[{label}|cookies={use_cookies}] Video {entry.get('id','?')}: {len(fmts)} formats ({real} real)")
                        if is_clean(entry.get('title'), entry.get('duration'), state.last_title):
                            selected = entry
                            break
                    if not selected:
                        selected = entries[0]
                    if has_real_formats(selected.get('formats', [])):
                        log.info(f"Found real formats with client={label}, cookies={use_cookies}")
                        successful_client = clients
                        successful_cookies = use_cookies
                        break
                except Exception as e:
                    state.last_raw_error = str(e)[:600]
                    log.warning(f"Search failed with client={label}: {e}")

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
                # Retry-ul vechi refolosea acelasi lant SI arunca cookie-urile,
                # deci difera de incercarea eșuata doar prin cele 5 secunde.
                # ignore_no_formats_error=False ca sa aflam motivul REAL
                # ("Sign in to confirm you're not a bot" era doar warning).
                retry_opts = make_search_opts(
                    with_cookies=cookies_available(),
                    extractor_args=yt_client_args(*WEB_CLIENTS),
                    default_search=None,
                    ignore_no_formats_error=False,
                )
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
                    state.last_raw_error = str(e)[:600]
                    log.warning(f"Retry failed for {vid_id}: {e}")
                    raise ValueError("YouTube a blocat acest video (0 formate reale)")

            # Download: incearca prima data cu combinatia care a mers la search
            cookie_order = [True, False] if successful_cookies else [False, True]
            for use_cookies_dl in cookie_order:
                if filename and os.path.exists(filename):
                    break
                for fmt in formats_to_try:
                    try:
                        if use_cookies_dl and not cookies_available():
                            break
                        overrides = {'format': fmt}
                        if successful_client:
                            overrides['extractor_args'] = yt_client_args(*successful_client)
                        dl_opts = make_download_opts(with_cookies=use_cookies_dl,
                                                     **overrides)
                        client_label = ('+'.join(successful_client)
                                        if successful_client else 'default')
                        log.info(f"Download cookies={use_cookies_dl}, "
                                 f"client={client_label}, format={fmt}")
                        # O singura instanta YoutubeDL descarca SI construieste
                        # numele fisierului; inainte erau doua, iar cea externa
                        # exista doar pentru prepare_filename.
                        dl_info, filename = await ytdlp.extract_and_prepare_filename(
                            dl_opts, web_url, loop=_loop, stage=f"download_{fmt}"
                        )
                        if not os.path.exists(filename):
                            base = os.path.splitext(filename)[0]
                            for ext in ['.opus', '.m4a', '.webm', '.mp3', '.ogg']:
                                if os.path.exists(base + ext):
                                    filename = base + ext
                                    break
                        if filename and os.path.exists(filename):
                            break
                    except Exception as e:
                        state.last_raw_error = str(e)[:600]
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
            # Oprire deliberata: invalidam callback-ul piesei vechi INAINTE de
            # stop, altfel el avanseaza coada si sterge fisierul pe care tocmai
            # il pornim (bug-ul de la !nplay).
            bump_play_generation(state)
            vc.stop()
            await asyncio.sleep(0.3)
        if not vc.is_connected():
            raise ConnectionError("Voice deconectat dupa stop.")

        state.last_start_time = time.time()
        state.current_file = filename
        captured_filename = filename
        after_play = make_after_play(ctx, state, captured_filename)

        try:
            source = await discord.FFmpegOpusAudio.from_probe(filename, **FFMPEG_OPTS)
            vc.play(source, after=after_play)
        except Exception:
            log.warning("OpusAudio esuat, fallback PCM", exc_info=True)
            vc.play(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTS), after=after_play)

        state._consecutive_errors = 0
        state.breaker_until = 0.0
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

    except asyncio.CancelledError:
        # O comanda noua ne-a anulat. CancelledError e BaseException, deci fara
        # aceasta ramura si fara finally-ul de mai jos is_loading ramanea True
        # pentru totdeauna si botul tacea, conectat, la orice !play.
        cleanup_file(filename, _loop)
        raise
    except Exception as e:
        log.error(f"Eroare process_play: {e}", exc_info=True)
        cleanup_file(filename, _loop)
        state._consecutive_errors += 1
        failure = e
    finally:
        state.is_loading = False

    if failure is None:
        return

    # Diagnoza pe textul BRUT de la yt-dlp, nu pe mesajul nostru in romana:
    # altfel toate erorile ieseau "Eroare necunoscuta" si sfatul despre
    # reinnoirea cookie-urilor nu putea fi afisat niciodata.
    error_type, user_msg = diagnose_error(state.last_raw_error or failure)
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
