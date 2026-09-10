"""Motor de redare: process_play, play_next, trigger_radio."""
import discord
import asyncio
import os
import time
from music.config import (FFMPEG_OPTS, MAX_DOWNLOAD_BYTES,
                          MAX_TRACK_SECONDS, log)
from music.config import (HLS_MAX_BYTES, clear_ydl_reason, cookies_available,
                          count_real_formats, has_real_formats,
                          last_ydl_reason, make_download_opts,
                          make_search_opts, yt_client_args, WEB_CLIENTS)
from music import ytdlp
from music.state import begin_loading, end_loading, get_state, loading
from music.utils import (cached_download, cleanup_file, is_clean, item_title,
                         trim_download_cache)
from music.autoplay import prefill_autoplay_queue
from music.diag import scrub as _scrub
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


class PlaybackInterrupted(Exception):
    """Redarea a fost oprita intentionat (stop, deconectare), nu a eșuat."""


class TrackRejected(Exception):
    """Piesa a fost refuzata de reguli (live, durata), nu a eșuat tehnic.

    Tip separat pentru ca tratamentul e diferit: mesaj clar, fara numaratoare de
    erori si fara diagnoza de cookies. Ambalate ca Exception generica, refuzurile
    urcau spre intrerupatorul de 5 erori si utilizatorul primea "Eroare
    necunoscuta" pentru o regula pe care noi am scris-o.
    """


# Cate refuzuri consecutive acceptam inainte sa ne oprim din a avansa coada.
MAX_CONSECUTIVE_REJECTS = 5

# (selector de format, plafon de octeti), in ordinea incercarilor. La nivel de
# modul ca testele sa citeasca valoarea reala, nu textul sursei.
#
# A doua incercare cere AUDIO din HLS, nu `best`. `best[protocol=m3u8*]` e o
# redare muxata video+audio, iar `-vn` arunca imaginea abia DUPA ce a ajuns pe
# disc: un bot audio descarca astfel zeci de MB de video pe un IP care ne
# limiteaza. Plafonul ei e mai strans, fiindca HLS-ul e o plasa de siguranta, nu
# calea normala.
DOWNLOAD_ATTEMPTS = [
    ('bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
     MAX_DOWNLOAD_BYTES),
    ('bestaudio[protocol^=m3u8]/bestaudio*[protocol^=m3u8]/best[protocol^=m3u8]',
     HLS_MAX_BYTES),
]


async def _resolve_query_to_url(state, query: str) -> str:
    """Transforma o interogare in URL-ul unui videoclip, cat mai ieftin posibil.

    Un URL trece direct. Un text devine o cautare FLAT: yt-dlp intoarce doar
    metadata de lista (id, titlu, durata, live_status), fara sa atinga pagina si
    API-ul player pentru fiecare rezultat. Filtram apoi cu is_clean si extragem
    complet exact un videoclip.

    Inainte, `default_search='ytsearch5'` fara extract_flat extragea integral
    toate cele cinci rezultate — aproximativ 20 de cereri pentru un singur
    !play, si toate in interiorul unui singur slot de throttle si al unui singur
    buget de 90s.
    """
    if str(query).startswith(('http://', 'https://')):
        return query

    opts = make_search_opts(
        with_cookies=cookies_available(),
        extract_flat=True,
        extractor_args=yt_client_args(*WEB_CLIENTS),
    )
    info = await _yt_extract_info(opts, query, download=False, stage='search_flat')
    entries = [e for e in (info.get('entries') or []) if e]
    if not entries:
        raise ValueError("Nu am gasit niciun rezultat")

    chosen = None
    for entry in entries:
        if is_clean(entry.get('title'), entry.get('duration'), state.last_title):
            chosen = entry
            break
        log.info(f"Sarit (filtru): {item_title(entry, 50)} "
                 f"durata={entry.get('duration')} live={entry.get('live_status')}")
    if chosen is None:
        # Niciunul nu trece filtrul. Inainte se lua orbeste entries[0], deci
        # filtrul nu putea respinge nimic si un live de 3 ore ajungea in redare.
        raise TrackRejected(
            f"toate cele {len(entries)} rezultate au fost filtrate "
            f"(live, prea scurte sau prea lungi)")

    log.info(f"Ales din {len(entries)} rezultate: {item_title(chosen, 60)}")
    url = chosen.get('url') or chosen.get('id')
    if url and not str(url).startswith('http'):
        url = f"https://www.youtube.com/watch?v={url}"
    if not url:
        raise ValueError("Rezultatul nu are URL")
    return url


def _unplayable_reason(info) -> str | None:
    """Motiv pentru care piesa nu are ce sa caute in redare, sau None.

    Se aplica si pe URL-uri directe, nu doar pe rezultatele de cautare. Pana
    acum un link de live sau de podcast de trei ore trecea intreaga extractie,
    intra in bucla de descarcare, era respins tacut de match_filter, si
    utilizatorul primea "Niciun format nu a reusit descarcarea" — un mesaj care
    arata ca o defectiune, nu ca o regula.

    Verifica DOAR ce e o limita operationala reala: un live nu se termina
    niciodata, iar peste MAX_TRACK_SECONDS trecem bugetul de descarcare si
    limita de fisier. Blocklist-ul, similaritatea si durata MINIMA din is_clean
    servesc alegerea AUTOMATA (cautare, autoplay), unde scopul e sa nu culegem
    teasere si shorts; cand cineva da explicit un link de 20 de secunde, singurul
    lucru corect e sa il redam.
    """
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
        return "E un live, nu o piesa"
    duration = info.get('duration')
    if duration and duration > MAX_TRACK_SECONDS:
        return (f"Piesa are {int(duration // 60)} minute, limita e "
                f"{MAX_TRACK_SECONDS // 60}")
    return None


def _worth_another_format(state) -> bool:
    """Merita a doua incercare cu alt format?

    Doar cand eșecul e chiar despre formate. Un 429, un cookie expirat sau un
    video indisponibil dau acelasi raspuns oricat de diferit ai scrie selectorul,
    deci o a doua rundă e doar o cerere in plus pe un IP deja limitat — si inca
    una cu buget propriu de 240 de secunde.
    """
    if not state.last_raw_error:
        # Nicio eroare raportata inseamna respins de filtru (durata, live), nu o
        # problema de format.
        return False
    error_type, _ = diagnose_error(state.last_raw_error)
    worth = error_type in ('format', 'unknown')
    if not worth:
        log.info(f"Nu mai incerc alt format: cauza e '{error_type}', "
                 f"nu selectorul de format")
    return worth


def _meta(*sources, keys):
    """Prima valoare utila pentru oricare dintre chei, in ordinea surselor.

    Exista pentru ca metadata de la DESCARCARE (`dl_info`) era atribuita si
    niciodata citita, iar panoul se umplea din extractia de selectie, mai
    sarace. yt-dlp intoarce `view_count` si `like_count` la o extractie completa,
    deci datele pentru care se plateau o unitate de cota API, o runda HTTPS si un
    al doilea update de panou erau deja in memorie.
    """
    for source in sources:
        if not source:
            continue
        for key in keys:
            value = source.get(key)
            if value:
                return value
    return None


def _video_id(url: str) -> str | None:
    """ID-ul de videoclip dintr-un URL de YouTube, sau None.

    Cheia de cache: `outtmpl` e deja `%(id)s.%(ext)s`, deci numele fisierului de
    pe disc ESTE ID-ul.
    """
    text = str(url or '')
    if 'v=' in text:
        return text.split('v=')[-1].split('&')[0] or None
    if 'youtu.be/' in text:
        return text.split('youtu.be/')[-1].split('?')[0] or None
    return None


def _history_entry(state, video_id: str) -> dict | None:
    """Intrarea de history pentru acest ID, ca sa nu re-cerem metadata."""
    if not video_id:
        return None
    for entry in reversed(state.history):
        if _video_id(entry.get('url')) == video_id and entry.get('title'):
            return entry
    return None


def trim_cache() -> None:
    """Evacueaza cache-ul audio, protejand ce se reda chiar acum.

    Inlocuieste stergerea de dupa fiecare piesa. O piesa stearsa la 2 secunde
    dupa final trebuie re-descarcata integral cand cineva o cere din nou —
    acelasi link, butonul Back, loop pe coada, saltul din select, sau autoplay
    care o re-propune. Cu cache, a doua redare costa zero cereri.
    """
    from music.state import guild_states
    keep = {st.current_file for st in guild_states.values() if st.current_file}
    trim_download_cache(keep)


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
            # NU stergem fisierul: rămâne in cache pentru urmatoarea redare.
            trim_cache()
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

    my_load_token = begin_loading(state)
    failure = None
    rejected = None
    filename = None
    # Trebuie sa existe si pe calea de refolosire (loop / acelasi URL), unde
    # bucla de descarcare nu ruleaza deloc: altfel citirea de mai jos ar fi un
    # NameError exact pe calea cea mai frecventa.
    dl_info = None

    reused = False
    web_url = None
    selected = None
    target_url = None

    try:
        # Repetare (loop pe piesa) sau re-adaugarea aceluiasi URL: fisierul e
        # chiar cel care se reda, deci nu cerem absolut nimic.
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
            # Alegerea piesei se face pe metadata IEFTINA, apoi extragem complet
            # exact un videoclip. Cu ytsearch5 fara extract_flat, yt-dlp extragea
            # integral toate cele cinci rezultate: ~20 de cereri catre YouTube
            # pentru un singur !play, toate intr-un singur slot de throttle.
            target_url = await _resolve_query_to_url(state, query)

            # Cache pe disc, de la o redare anterioara. Verificarea vine DUPA
            # rezolvarea la URL (un text de cautare nu are ID) si INAINTE de
            # extractia completa, deci un hit costa zero cereri catre YouTube,
            # zero octeti de media, niciun slot de throttle si nicio expunere la
            # 429. Titlul si durata le luam din history, unde piesa e deja.
            cached_id = _video_id(target_url)
            cached_path = cached_download(cached_id) if cached_id else None
            known = _history_entry(state, cached_id) if cached_path else None
            if cached_path and known:
                log.info(f"Cache audio: refolosesc {os.path.basename(cached_path)}")
                filename = cached_path
                reused = True
                web_url = known.get('url') or target_url
                selected = {
                    'title': known.get('title'),
                    'duration': known.get('duration'),
                    'thumbnail': known.get('thumbnail'),
                    'channel': known.get('channel'),
                    'webpage_url': web_url,
                }

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
                    default_search=None,      # avem deja un URL
                )

                try:
                    info = await _yt_extract_info(
                        search_opts, target_url, download=False,
                        stage=f"extract_{label}"
                    )
                    entries = info.get('entries') or [info]
                    candidate = entries[0] if entries else None
                    if not candidate:
                        continue
                    fmts = candidate.get('formats', [])
                    real = count_real_formats(fmts)
                    log.info(f"[{label}|cookies={use_cookies}] Video "
                             f"{candidate.get('id','?')}: {len(fmts)} formats "
                             f"({real} redabile)")
                    selected = candidate
                    if has_real_formats(fmts):
                        log.info(f"Formate redabile cu client={label}, "
                                 f"cookies={use_cookies}")
                        successful_client = clients
                        successful_cookies = use_cookies
                        break
                except Exception as e:
                    state.last_raw_error = str(e)[:600]
                    log.warning(f"Extractia a eșuat cu client={label}: {e}")

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

            # Regulile se aplica si pe URL-uri directe, nu doar pe cautare.
            # Altfel un link de live trecea toata extractia, intra in bucla de
            # descarcare si era respins tacut de match_filter.
            reason = _unplayable_reason(selected)
            if reason:
                raise TrackRejected(reason)

            # Download: format-ul in bucla EXTERIOARA, modul de cookies in cea
            # interioara. Un 429 sau un cookie expirat nu devine alt raspuns daca
            # intrebi cu alt sir de format, deci varianta veche (format in
            # interior) putea plati patru extractii complete plus patru
            # transferuri partiale, fiecare cu buget propriu de 240s, pentru
            # aceeasi cauza.
            clear_ydl_reason()
            cookie_order = [True, False] if successful_cookies else [False, True]
            for fmt, size_cap in DOWNLOAD_ATTEMPTS:
                if filename and os.path.exists(filename):
                    break
                if fmt != DOWNLOAD_ATTEMPTS[0][0] and not _worth_another_format(state):
                    break
                for use_cookies_dl in cookie_order:
                    try:
                        if use_cookies_dl and not cookies_available():
                            continue
                        overrides = {'format': fmt, 'max_filesize': size_cap}
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
            # Nicio excepție nu a fost ridicata: yt-dlp raporteaza refuzul doar
            # prin to_screen, care fara logger nu scrie nimic. Cu logger avem
            # propozitia lui, care e mult mai buna decat o presupunere a noastra.
            if not state.last_raw_error:
                state.last_raw_error = last_ydl_reason() or (
                    "yt-dlp nu a scris fisierul si nu a raportat nicio eroare; "
                    f"probabil respins de filtru (live sau durata peste "
                    f"{MAX_TRACK_SECONDS}s)")
                log.warning(f"Descarcare fara fisier: {state.last_raw_error}")
            raise FileNotFoundError("Niciun format nu a reusit descarcarea")

        state.last_url = web_url
        state.last_title = _meta(dl_info, selected, keys=('title',)) or 'Necunoscut'
        state.last_duration = _meta(dl_info, selected, keys=('duration',)) or 0
        state.last_thumbnail = _meta(dl_info, selected, keys=('thumbnail',))
        state.is_radio_now = is_radio
        state.last_channel = _meta(dl_info, selected,
                                   keys=('channel', 'uploader')) or ''
        state.last_views = _meta(dl_info, selected, keys=('view_count',)) or 0
        state.last_likes = _meta(dl_info, selected, keys=('like_count',)) or 0
        # Canalul intra si in history: artist_key cade pe el cand titlul nu are
        # separator, dar intrarile de history nu il purtau, deci plafonul de
        # diversitate nu se aplica piesele cu titlu de un singur cuvant.
        state.history.append({'url': web_url, 'title': state.last_title,
                              'channel': state.last_channel})
        if len(state.history) > 20:
            state.history.pop(0)

        if not vc.is_connected():
            raise PlaybackInterrupted("Voice deconectat in timpul descarcarii.")
        if vc.is_playing() or vc.is_paused():
            # Oprire deliberata. Invalidam callback-ul piesei vechi INAINTE de
            # stop, altfel el avanseaza coada si sterge fisierul pe care tocmai
            # il pornim (bug-ul de la !nplay).
            #
            # `is_paused()` conteaza la fel de mult ca `is_playing()`:
            # discord.py raporteaza is_playing()==False cat timp e pauzat, iar
            # vc.play() suprascrie _player fara sa se plânga, lasand thread-ul
            # vechi parcat in _resumed.wait() cu procesul lui ffmpeg viu pana la
            # oprirea containerului.
            displaced = state.current_file
            bump_play_generation(state)
            vc.stop()
            await asyncio.sleep(0.3)
            # Callback-ul invechit nu mai curata nimic, deci fisierul inlocuit
            # e responsabilitatea noastra. Nu il stergem daca e chiar cel pe
            # care urmeaza sa il redam (loop / acelasi URL).
            if displaced and displaced != filename:
                # Piesa inlocuita rămâne in cache; doar evacuam daca e nevoie.
                trim_cache()
        if not vc.is_connected():
            raise PlaybackInterrupted("Voice deconectat dupa stop.")

        state.last_start_time = time.time()
        state.paused_at = 0.0
        state.current_file = filename
        captured_filename = filename
        after_play = make_after_play(ctx, state, captured_filename)

        source = None
        try:
            source = await discord.FFmpegOpusAudio.from_probe(filename, **FFMPEG_OPTS)
            vc.play(source, after=after_play)
        except Exception:
            log.warning("OpusAudio esuat, fallback PCM", exc_info=True)
            # Daca from_probe a reusit dar vc.play a crapat, procesul FFmpeg
            # pornit de el rămânea in viata; il inchidem inainte de fallback.
            if source is not None:
                try:
                    source.cleanup()
                except Exception:
                    pass
            vc.play(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTS), after=after_play)

        state._consecutive_errors = 0
        state._consecutive_rejects = 0
        state.breaker_until = 0.0
        # Altfel eroarea unei piese de acum o ora era raportata ca motiv pentru
        # urmatoarea care eșua fara sa spuna nimic.
        state.last_raw_error = None
        state._last_notified_error = None
        await update_player_ui(ctx, send_new=True)

        # Completare din Data API, DOAR pentru ce nu am primit de la yt-dlp.
        # Inainte rula la fiecare piesa: o unitate de cota, o runda HTTPS si un
        # al doilea update de panou, ca sa scrie peste valori pe care extractia
        # le adusese deja. Singurele campuri pe care le foloseste panoul si care
        # pot lipsi sunt views si likes (ui.py le citeste, nimic altceva).
        missing_stats = not (state.last_views or state.last_likes)
        if missing_stats and yt_api.is_available():
            try:
                vid_id = web_url.split('v=')[-1].split('&')[0] if 'v=' in web_url else None
                if vid_id:
                    details = await _loop.run_in_executor(
                        None, lambda: yt_api.get_video_details([vid_id])
                    )
                    d = details.get(vid_id, {})
                    changed = False
                    for field, key in (('last_views', 'views'),
                                       ('last_likes', 'likes'),
                                       ('last_channel', 'channel'),
                                       ('last_thumbnail', 'thumbnail')):
                        value = d.get(key)
                        if value and not getattr(state, field):
                            setattr(state, field, value)
                            changed = True
                    if d.get('duration') and not state.last_duration:
                        state.last_duration = d['duration']
                        changed = True
                    if changed:
                        # Doar cand s-a schimbat ceva: un edit inutil consuma din
                        # limita de rata a Discord si re-creeaza view-ul.
                        await update_player_ui(ctx)
            except Exception:
                log.debug("Completarea din Data API a eșuat", exc_info=True)

    except asyncio.CancelledError:
        # O comanda noua ne-a anulat. CancelledError e BaseException, deci fara
        # aceasta ramura si fara finally-ul de mai jos is_loading ramanea True
        # pentru totdeauna si botul tacea, conectat, la orice !play.
        cleanup_file(filename, _loop)
        raise
    except PlaybackInterrupted as e:
        # !stop, deconectare sau mutare din canal in timpul descarcarii. Nu e o
        # defectiune: inainte urca numaratoarea de erori spre intrerupator si ii
        # arunca utilizatorului "Eroare necunoscuta" pentru propria lui comanda.
        # Tip propriu, nu ConnectionError: acela acopera si erorile reale de
        # retea, care trebuie sa rămâna vizibile.
        log.info(f"Redare intrerupta: {e}")
        cleanup_file(filename, _loop)
    except TrackRejected as e:
        # Regula noastra, nu defectiune: nu atinge _consecutive_errors si nu
        # trece prin diagnose_error, care ar traduce-o in "Eroare necunoscuta".
        log.info(f"Piesa refuzata: {e}")
        cleanup_file(filename, _loop)
        state._consecutive_rejects += 1
        rejected = str(e)
    except Exception as e:
        log.error(f"Eroare process_play: {e}", exc_info=True)
        cleanup_file(filename, _loop)
        state._consecutive_errors += 1
        failure = e
    finally:
        # Doar ultimul proprietar elibereaza steagul: altfel un process_play
        # care se termina ar debloca un altul aflat inca in lucru.
        end_loading(state, my_load_token)

    if rejected:
        too_many = state._consecutive_rejects >= MAX_CONSECUTIVE_REJECTS
        try:
            if too_many:
                await ctx.send(
                    f"⏭️ **{state._consecutive_rejects} piese refuzate la rand** "
                    f"(ultima: {rejected}). Ma opresc, da-mi altceva cu `!play`.",
                    delete_after=60)
            else:
                await ctx.send(f"⏭️ **Sarita:** {rejected}", delete_after=30)
        except discord.HTTPException:
            pass
        if too_many:
            # Coada e plina de lucruri pe care nu le putem reda; fiecare element
            # costa o extractie completa, deci nu o parcurgem pana la capat.
            state._consecutive_rejects = 0
            state.autoplay = False
            start_timeout(ctx)
        elif state.autoplay or state.queue:
            play_next(ctx)
        else:
            start_timeout(ctx)
        return

    if failure is None:
        return

    # Diagnoza pe textul BRUT de la yt-dlp, nu pe mesajul nostru in romana:
    # altfel toate erorile ieseau "Eroare necunoscuta" si sfatul despre
    # reinnoirea cookie-urilor nu putea fi afisat niciodata.
    error_type, user_msg = diagnose_error(_scrub(state.last_raw_error or failure))
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
