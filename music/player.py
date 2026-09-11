"""Motor de redare: process_play, play_next, trigger_radio."""
import discord
import asyncio
import os
import time
from music.config import (AUTOPLAY_REFILL_BELOW, FFMPEG_OPTS, PREFETCH_AHEAD,
                          cookies_available, promote_cookies, rollback_cookies,
                          log)
from music.resolve import (cached_for, resolve_from_url, search_to_url,
                           video_id)
from music.state import begin_loading, end_loading, get_state
from music.utils import (DISCORD_ERRORS, UndecodableAudio, cleanup_file,
                         item_title,
                         make_opus_source, read_track_meta,
                         trim_download_cache, write_track_meta)
from music.autoplay import prefill_autoplay_queue
from music.diag import scrub as _scrub
from music.errors import diagnose_error
from music import youtube_api as yt_api

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

# Pauza dupa prea multe refuzuri la rand. Mai scurta decat BREAKER_COOLDOWN_SEC:
# refuzurile nu inseamna ca YouTube ne blocheaza, doar ca ce s-a cerut nu se poate
# reda (live-uri, seturi de doua ore). Dar trebuie sa fie o pauza REALA, altfel
# tick-ul de 24/7 reia radioul la 60 de secunde si mesajul "Ma opresc" e o minciuna.
REJECT_COOLDOWN_SEC = 300

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


def _history_entry(state, vid: str) -> dict | None:
    """Intrarea de history pentru acest ID, ca sa nu re-cerem metadata.

    Rămâne aici, nu in resolve.py: citeste `state.history`, adica exact ce
    resolve nu are voie sa cunoasca.
    """
    if not vid:
        return None
    for entry in reversed(state.history):
        if video_id(entry.get('url')) == vid and entry.get('title'):
            return entry
    return None


def _log_play_result(outcome: str, started_at: float, query, *, url=None,
                     reused: bool = False, duration=0, cookies: bool = False,
                     reason=None) -> None:
    """O SINGURA linie per incercare de redare, cu tot ce trebuie ca sa o explici.

    Pana acum, ca sa intelegi o seara proasta trebuia sa aduni zeci de linii
    imprastiate: clientul dintr-un log de resolve, modul de cookies din altul,
    verdictul din al treilea, iar un hit de cache nu lasa aproape nicio urma. Cu un
    format fix se poate grep-a si numara: cate redari au fost din cache, cate au
    folosit jar-ul, unde se duce timpul, care e distributia de eșecuri.
    """
    elapsed = max(0.0, time.time() - started_at)
    fields = [
        f"rezultat={outcome}",
        f"sursa={'cache' if reused else 'descarcat'}",
        f"cookies={int(bool(cookies))}",
        f"durata={int(duration or 0)}s",
        f"elapsed={elapsed:.1f}s",
        f"id={video_id(url) or '?'}",
    ]
    if reason:
        # Un singur rand, oricat de urat e textul brut de la yt-dlp.
        fields.append(f"motiv={str(reason)[:120]!r}".replace('\n', ' '))
    log.info('PLAY ' + ' '.join(fields))


def _discard_partial(filename, reused: bool) -> None:
    """Sterge fisierul doar daca redarea asta chiar l-a descarcat.

    Pe caile de refolosire (hit de cache pe disc, loop pe acelasi URL) `filename`
    e o intrare de cache care exista de INAINTE. Pana la introducerea cache-ului
    era intotdeauna un fisier proaspat descarcat, deci stergerea la eroare era
    corecta; de atunci, orice hit intrerupt (deconectare in timpul cautarii, un
    ffmpeg care pica) evacua exact intrarea pe care cache-ul exista sa o pastreze.
    Reprodus: fisier in cache pentru vid123, `!play <text>`, deconectare in timpul
    cautarii — fisierul dispărea.
    """
    if not filename or reused:
        return
    cleanup_file(filename, _loop)


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


def init(bot_ref, ui_func, start_to, cancel_to):
    global bot, update_player_ui, start_timeout, cancel_timeout, _loop
    bot = bot_ref
    update_player_ui = ui_func
    start_timeout = start_to
    cancel_timeout = cancel_to


def _session_alive(ctx, state, generation):
    """Predicat "sesiunea mai exista", pentru munca de fundal.

    Acelasi test pe care il face si redarea: voce conectata si generatia neschimbata.
    Acopera dintr-o singura data `!stop`, butonul Stop, handler-ul de deconectare si
    oprirea containerului — toate trec prin deconectarea clientului de voce sau prin
    `bump_play_generation`.
    """
    def alive() -> bool:
        vc = getattr(ctx, 'voice_client', None)
        return bool(vc and vc.is_connected()) and state.play_generation == generation

    return alive


async def _prefetch_worker(state, alive=None) -> None:
    """Descarca in cache primele PREFETCH_AHEAD piese din coada.

    Nu atinge NICIODATA starea de redare, si mai ales nu `is_loading`: steagul
    acela e o promisiune ca o incarcare in curs va SCURGE coada, iar `!play` il
    citește ca "pune in coada, se rezolva". Un prefetch nu scurge nimic, deci daca
    l-ar aprinde, o piesa cerută in fereastra aceea ar rămâne in coada pentru
    totdeauna.

    Doar URL-uri cu id de videoclip: un text de cautare ar cere o extractie in
    plus doar ca sa afle ce sa verifice in cache, iar cererea aceea e exact ce
    prefetch-ul incearca sa economiseasca.

    `alive` spune dacă sesiunea mai exista. Fara el, un prefetch pornit inainte de
    `!stop` continua sa țina singurul slot de cereri catre YouTube si sa descarce o
    piesa pe care nimeni nu o mai aȘteapta — deci prima comanda de dupa stop
    aȘtepta in spatele ei.
    """
    def keep_going() -> bool:
        return alive() if alive else True

    for item in list(state.queue)[:PREFETCH_AHEAD]:
        if not keep_going():
            log.info("Prefetch abandonat: sesiunea s-a incheiat")
            return
        query = (item or {}).get('query') or ''
        vid, cached = cached_for(query)
        if not vid or cached:
            continue
        log.info(f"Prefetch: {item_title(item, 40)}")
        resolved = await resolve_from_url(query, loop=_loop,
                                          should_continue=keep_going)
        if resolved.filename:
            log.info(f"Prefetch gata: {vid}")
            # Insoțitorul de metadate, scris ACUM, din extractia deja plătita.
            # Fara el, fisierul e audio pe care nimeni nu il poate descrie, iar
            # poarta de zero-cereri din `process_play` cere si fisierul SI
            # metadata (`read_track_meta(...) or _history_entry(...)`) — o piesa
            # de autoplay nu a fost niciodata in history, deci prefetch-ul
            # economisea doar transferul, nu si cele doua cereri prin poarta
            # throttled. Adica aproape tot ce exista sa evite.
            info, dl = resolved.info, resolved.download_info
            write_track_meta(resolved.filename, {
                'url': resolved.url,
                'title': _meta(dl, info, keys=('title',)) or 'Necunoscut',
                'channel': _meta(dl, info, keys=('channel', 'uploader')) or '',
                'duration': _meta(dl, info, keys=('duration',)) or 0,
                'thumbnail': _meta(dl, info, keys=('thumbnail',)),
                'views': _meta(dl, info, keys=('view_count',)) or 0,
                'likes': _meta(dl, info, keys=('like_count',)) or 0})
            # Plafonul de cache se aplica si aici: altfel un prefetch ar putea
            # umple volumul intre doua evacuari.
            trim_cache()
        else:
            log.info(f"Prefetch fara rezultat pentru {vid}: "
                     f"{_scrub(resolved.raw_error or 'necunoscut')[:120]}")


def schedule_prefetch(state, alive=None) -> str:
    """Porneste prefetch-ul in fundal. Intoarce ce a decis, pentru loguri/teste."""
    if PREFETCH_AHEAD <= 0:
        return 'dezactivat'
    if _loop is None:
        return 'fara bucla'
    task = getattr(state, 'prefetch_task', None)
    if task is not None and not task.done():
        # Unul e deja in zbor. A porni al doilea ar dubla cererile pe un IP care
        # deja ne limiteaza, si ambele ar scrie in acelasi fisier din cache:
        # `outtmpl` e `%(id)s.%(ext)s`, deci calea de pe disc E cheia de cache.
        return 'deja in curs'
    if not state.queue:
        return 'coada goala'

    async def guarded():
        try:
            await _prefetch_worker(state, alive)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Un prefetch eșuat nu are voie sa atinga redarea care cânta acum.
            log.warning(f"Prefetch esuat: {e}", exc_info=True)

    state.prefetch_task = _loop.create_task(guarded())
    return 'pornit'


async def trigger_radio(ctx, token: int | None = None):
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
        log.warning(f"Autoplay error (guild {ctx.guild.id}): {e}", exc_info=True)
        _release_loading(state, token)
        state.autoplay = False
        try:
            await ctx.send("Autoplay s-a oprit.", delete_after=10)
        except discord.HTTPException:
            pass
        start_timeout(ctx)


def _release_loading(state, token: int | None) -> None:
    """Stinge steagul de incarcare respectand proprietatea.

    Cu un token, `end_loading` verifica intai daca incarcarea care ține steagul e
    chiar a noastra. Fara (apeluri mai vechi care nu il duc), cade pe atribuirea
    directa — comportamentul de dinainte, pastrat ca sa nu rămâna un steag aprins.
    """
    if token is None:
        state.is_loading = False
    else:
        end_loading(state, token)


async def _play_next_async(ctx, token: int | None = None):
    state = get_state(ctx.guild.id)
    next_item = None
    try:
        async with state._lock:
            vc = ctx.voice_client
            if not vc or not vc.is_connected():
                _release_loading(state, token)
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
            if (state.autoplay and state.last_url
                    and len(state.queue) < AUTOPLAY_REFILL_BELOW):
                try:
                    await prefill_autoplay_queue(state, _loop)
                    log.info(f"Refill dupa skip: coada={len(state.queue)}")
                    # Coada era goala cand a pornit piesa asta, deci prefetch-ul
                    # de atunci n-a avut ce sa ia. Acum are.
                    schedule_prefetch(
                        state,
                        _session_alive(ctx, state, state.play_generation))
                    await update_player_ui(ctx)
                except Exception as e:
                    log.warning(f"Prefill dupa skip esuat: {e}", exc_info=True)
        elif state.autoplay and state.last_url:
            cancel_timeout(ctx)
            await trigger_radio(ctx, token)
        else:
            _release_loading(state, token)
            start_timeout(ctx)
    except Exception as e:
        log.error(f"play_next EROARE: {e}", exc_info=True)
        _release_loading(state, token)
        start_timeout(ctx)


def resume_if_idle(ctx) -> str:
    """Repune redarea in mișcare dupa o operatie care nu a pornit nimic.

    `is_loading` e o PROMISIUNE: fiecare consumator il citeste ca "o incarcare e
    in curs si va scurge coada cand se termina" — de aceea `!play` raspunde "in
    coada" in loc sa redea. Orice operatie care ia steagul, sau doar anuleaza
    timer-ul, fara sa porneasca redare — prefill-ul butonului Autoplay, `!247` ON,
    citirea unui playlist — trebuie sa treaca pe aici la final.

    Fara asta, o piesa cerută in exact acea fereastra rămânea in coada si NIMIC nu
    o mai scotea: `after_play` are nevoie de o piesa care chiar cânta, iar
    `idle_timer` fusese anulat de comanda si se re-armeaza doar in 24/7. Botul
    rămânea in canal, tacut, cu panoul aratand "13 in coada".

    Intoarce ce a facut, ca sa poata fi verificat de teste si citit in loguri.
    """
    vc = getattr(ctx, 'voice_client', None)
    if not vc or not vc.is_connected():
        return 'deconectat'
    state = get_state(ctx.guild.id)
    # Coada s-a schimbat, deci merita incalzita — INAINTE de orice ieșire, si mai
    # ales inainte de "canta deja", care e chiar cazul obișnuit: apeși Autoplay
    # peste o piesa care cânta. Prefetch-ul pornea doar din `process_play`, unde
    # coada e aproape mereu goala, deci in fluxul normal (redau, apoi pornesc
    # autoplay) primul skip plătea integral extracția si descarcarea. Masurat in
    # emulator: 30 de secunde cu coada plina si cache-ul gol.
    schedule_prefetch(state, _session_alive(ctx, state, state.play_generation))
    if vc.is_playing() or vc.is_paused():
        return 'canta deja'
    if state.is_loading:
        # Chiar exista o incarcare in curs: ea va scurge coada, deci a porni si
        # noi una ar insemna doua rezolvari in paralel pe acelasi guild.
        return 'se incarca altceva'
    if state.queue:
        play_next(ctx)
        return 'pornit'
    if start_timeout:
        # Nimic de redat acum. Timer-ul e singurul lucru care mai poate decide
        # ceva (radio in 24/7, deconectare altfel), deci nu il lasam nearmat.
        start_timeout(ctx)
        return 'timer armat'
    return 'nimic'


def play_next(ctx):
    """Programeaza urmatoarea piesa. Sincrona: se cheama si din thread-ul FFmpeg.

    Doua defecte reparate aici, ambele in jurul steagului de incarcare:

    1. `state.is_loading = True` direct ocolea `begin_loading`, deci NU incrementa
       token-ul de proprietate. Un `process_play` care se termina imediat dupa isi
       chema `end_loading` cu token-ul lui — care inca se potrivea — si stingea
       steagul pus aici. Comanda urmatoare vedea "liber" si pornea o a doua
       rezolvare in paralel. Aceeasi clasa cu bug-ul din ramura de playlist.
    2. Cand nu exista bucla, functia ieșea prin `return` lasand steagul APRINS si
       nimic programat: din acel moment fiecare `!play` raspundea "in coada"
       pentru o incarcare care nu exista, iar nimic nu mai scurgea coada.
    """
    global _loop
    state = get_state(ctx.guild.id)
    # Aceeasi regula ca in `resume_if_idle`: o incarcare in curs VA scurge coada,
    # deci a porni si noi una inseamna doua rezolvari in paralel pe acelasi guild.
    # Fara garda, un `!skip` sau sfarșitul unei piese in timpul unui `!nplay`
    # pornea a doua rezolvare, consuma capul cozii, tăia piesa din aer si scria in
    # history o piesa pe care nimeni nu a ascultat-o.
    #
    # Garda sta INAINTE de `begin_loading`: acela aprinde steagul, deci aceeasi
    # verificare pusa in `_play_next_async` ar vedea mereu True. Si ieșim fara
    # token, ca sa nu il furam de la incarcarea in curs — altfel `finally`-ul ei nu
    # ar mai stinge steagul niciodata.
    if state.is_loading:
        log.info("play_next: se incarca altceva, nu pornesc a doua rezolvare")
        return
    token = begin_loading(state)
    if _loop is None:
        try:
            _loop = asyncio.get_event_loop()
        except RuntimeError as e:
            log.error(f"play_next fara bucla de evenimente: {e}")
            _release_loading(state, token)
            return
    try:
        asyncio.run_coroutine_threadsafe(_play_next_async(ctx, token), _loop)
    except RuntimeError as e:
        # Bucla inchisa (oprire in curs). Tot ce conteaza e sa nu lasam steagul.
        log.error(f"play_next nu a putut programa redarea: {e}")
        _release_loading(state, token)


def _current_track_survived(ctx, state) -> bool:
    """True cand piesa care se auzea e inca in aer, deci nu avem ce avansa.

    `process_play` poate fi chemat PESTE o piesa care cânta — singura cale e
    `!nplay`, care marcheaza `skip_request` si conteaza pe inlocuire. Cand
    incercarea eșuează sau e refuzata, nu s-a inlocuit nimic: un `play_next` de
    aici scoate capul cozii, iar `process_play` opreste apoi deliberat piesa
    curenta ca sa porneasca ce a scos — deci o eroare la `!nplay` tăia din aer
    piesa care mergea si mânca o intrare din coada.

    Steagul de skip se stinge tot aici: altfel promite o inlocuire care nu s-a
    intamplat si ar mânca prima re-inserare de loop.
    """
    vc = getattr(ctx, 'voice_client', None)
    if not vc or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
        return False
    state.skip_request = False
    return True


async def process_play(ctx, query, is_radio=False, *, after_rollback=False):
    state = get_state(ctx.guild.id)
    started_at = time.time()
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
    # Cutia poștala de erori brute e a PIESEI ASTA, nu a procesului. Verdictul
    # "0 formate reale" trăia inainte doar ca mesaj de excepție al piesei care il
    # producea; acum se scrie in stare, deci supravietuia piesei. Urmatoarea piesa
    # care eșua fara text brut propriu (o cautare fara rezultate, de exemplu) era
    # diagnosticata cu textul rămas, iar pentru ca diagnoza cadea pe acelasi
    # `error_type` dedup-ul de mai jos inghitea si mesajul catre utilizator: al
    # doilea eșec nu producea NIMIC pe Discord.
    #
    # Golirea sta dupa `begin_loading`, nu mai sus: ramura de intrerupator citeste
    # `state.last_raw_error` inainte, ca sa spuna de ce s-a inchis.
    state.last_raw_error = None
    failure = None
    rejected = None
    filename = None
    dl_info = None
    reused = False
    web_url = None
    selected = None
    # A autentificat jar-ul de cookies CEVA in redarea asta? Numai atunci are
    # sens sa il stampilam drept "ultimul bun cunoscut" (vezi mai jos).
    jar_authenticated = False

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

        if not reused:
            # 1. Text -> URL. Cautarea e FLAT: doar metadata de lista, apoi o
            #    singura extractie completa a videoclipului ales.
            # `avoid_title` DOAR pentru radio. Regula lui `is_clean` respinge
            # orice titlu care conține primele 15 caractere ale piesei anterioare
            # — ceea ce e exact ce vrei de la autoplay, si exact ce nu vrei de la
            # un om. Aplicata peste o cerere explicita, insemna: nu poți pune
            # aceeasi piesa a doua oara, si nu poți cere o a doua piesa a
            # aceluiasi artist ("Luis Gabriel - ..." se potrivește cu toate ale
            # lui). Iar refuzul minea, spunand "live, prea scurte sau prea lungi".
            target_url, reject = await search_to_url(
                query, avoid_title=state.last_title if is_radio else '',
                loop=_loop)
            if reject:
                raise TrackRejected(reject)
            if not target_url:
                raise ValueError("Nu am gasit niciun rezultat")

            # 2. Cache pe disc. Verificarea vine DUPA rezolvarea la URL (un text
            #    de cautare nu are ID) si INAINTE de extractia completa, deci un
            #    hit costa zero cereri catre YouTube, zero octeti de media si
            #    niciun slot de throttle.
            #
            #    Metadatele vin din fisierul insoțitor de langa audio, nu din
            #    `state.history`: acela are 20 de intrari, e golit de `!stop`, de
            #    plecarea din voce si de butonul Inapoi, iar intrarile lui nu
            #    purtau nici durata, nici thumbnail. Un hit pornea deci cu durata
            #    0 (panou fara lungime si fara timp rămas, `!seek` fara plafon) si
            #    cumpara o unitate de Data API pentru statistici — exact costul pe
            #    care cache-ul exista sa il elimine. History rămâne ca rezerva
            #    pentru fisierele descarcate inainte de insoțitori.
            cached_id, cached_path = cached_for(target_url)
            known = None
            if cached_path:
                known = (read_track_meta(cached_path)
                         or _history_entry(state, cached_id))
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
                    'view_count': known.get('views'),
                    'like_count': known.get('likes'),
                    'webpage_url': web_url,
                }

        if not reused:
            # 3. Negocierea cu yt-dlp e toata in music/resolve.py: lant de
            #    clienti, reincercare pentru formate, verificare de reguli, bucla
            #    de descarcare. Aici rămâne doar Discord si starea.
            resolved = await resolve_from_url(
                target_url, loop=_loop, should_continue=vc.is_connected)

            if resolved.raw_error:
                # Textul brut se intoarce ca VALOARE, nu se scrie intre piese:
                # `last_raw_error` era o cutie poștala niciodata golita la succes,
                # deci eroarea unei piese era raportata drept cauza pentru alta.
                state.last_raw_error = resolved.raw_error
            if resolved.reject_reason:
                raise TrackRejected(resolved.reject_reason)
            if resolved.interrupted:
                raise PlaybackInterrupted("Voice deconectat in timpul descarcarii.")
            if not resolved.info:
                raise ValueError("Nu am gasit niciun rezultat")
            if not resolved.ok:
                raise FileNotFoundError("Niciun format nu a reusit descarcarea")

            filename = resolved.filename
            dl_info = resolved.download_info
            selected = resolved.info
            web_url = resolved.url
            jar_authenticated = resolved.download_used_cookies

        if not filename or not os.path.exists(filename):
            raise FileNotFoundError("Niciun format nu a reusit descarcarea")

        # Verificarea sta DEASUPRA scrierilor de stare, si intreaba si de
        # PROPRIETATE. `!stop`, butonul Stop si plecarea din voce sting `is_loading`
        # fara sa opreasca descarcarea in curs, deci o rezolvare orfana ajungea
        # aici si isi scria piesa peste cea care chiar cânta: panoul arata alt titlu
        # si alta durata, iar `last_url` greșit facea ca urmatoarea repornire de
        # coada sa redea alt fisier.
        #
        # `PlaybackInterrupted` e tipul potrivit: nu atinge contorul de erori, nu
        # trece prin `diagnose_error`, iar `_discard_partial` curata oricum
        # descarcarea orfanului. `end_loading` cu token-ul lui e deja un no-op, deci
        # incarcarea vie isi pastreaza steagul.
        if not vc.is_connected() or state.load_token != my_load_token:
            raise PlaybackInterrupted("Sesiunea s-a incheiat in timpul descarcarii.")

        state.last_url = web_url
        state.last_title = _meta(dl_info, selected, keys=('title',)) or 'Necunoscut'
        state.last_duration = _meta(dl_info, selected, keys=('duration',)) or 0
        state.last_thumbnail = _meta(dl_info, selected, keys=('thumbnail',))
        state.is_radio_now = is_radio
        state.last_channel = _meta(dl_info, selected,
                                   keys=('channel', 'uploader')) or ''
        state.last_views = _meta(dl_info, selected, keys=('view_count',)) or 0
        state.last_likes = _meta(dl_info, selected, keys=('like_count',)) or 0
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
            source = await make_opus_source(filename, vc.channel, **FFMPEG_OPTS)
            vc.play(source, after=after_play)
        except UndecodableAudio:
            # NU pe fallback-ul PCM: acela ar reda acelasi fisier stricat si ar
            # raporta iar succes. Intrarea otravita pleaca de pe disc, ca reluarea
            # sa nu o mai serveasca, si eroarea urca la calea normala de eroare —
            # cu mesaj pentru utilizator si cu contorul de erori bătut.
            log.warning(f"Intrare de cache stricata, o Șterg: {filename}")
            cleanup_file(filename, _loop)
            if state.current_file == filename:
                state.current_file = None
            raise
        except Exception:
            log.warning("OpusAudio esuat, fallback PCM", exc_info=True)
            # Daca sursa s-a construit dar vc.play a crapat, procesul FFmpeg
            # pornit de el rămânea in viata; il inchidem inainte de fallback.
            if source is not None:
                try:
                    source.cleanup()
                except (OSError, AttributeError, ValueError) as e:
                    log.debug(f"Curatarea sursei audio a eșuat: {e}")
            vc.play(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTS), after=after_play)

        # A ieșit audio din proces. Momentul asta e singurul raspuns la "mai
        # merge?" care nu costa nicio cerere catre YouTube.
        state.last_play_ok = time.time()
        _log_play_result('ok', started_at, query, url=web_url, reused=reused,
                         duration=state.last_duration, cookies=jar_authenticated)

        # Piesa urmatoare se descarca ACUM, cat timp asta cânta. Altfel fiecare
        # skip plateste extractia plus descarcarea in fața utilizatorului: 5-30s
        # de liniște. Pornit dupa `vc.play`, deci nu intarzie cu nimic redarea.
        log.info(f"Prefetch: {schedule_prefetch(state, _session_alive(ctx, state, state.play_generation))}")

        # History si insoțitorul se scriu DUPA ce redarea a pornit cu adevarat.
        # Cand erau mai sus, o deconectare intre pregatire si `vc.play` lasa in
        # history o piesa care nu s-a auzit niciodata — iar history alimenteaza
        # `skip_ids` si plafonul de artist ai autoplay-ului, deci radio-ul ocolea
        # apoi o piesa pe care nimeni n-o ascultase.
        #
        # Canalul intra si el in history: artist_key cade pe el cand titlul nu are
        # separator, dar intrarile nu il purtau, deci plafonul de diversitate nu se
        # aplica pieselor cu titlu de un singur cuvant.
        state.history.append({'url': web_url, 'title': state.last_title,
                              'channel': state.last_channel,
                              'duration': state.last_duration,
                              'thumbnail': state.last_thumbnail})
        if len(state.history) > 20:
            state.history.pop(0)

        # Insoțitorul de langa fisierul audio, scris la fiecare redare reusita —
        # inclusiv la un hit de cache, ca un fisier vechi sa capete metadate cand e
        # ascultat din nou si cache-ul sa nu mai depinda de cele 20 de intrari de
        # history, care oricum se golesc la `!stop` si la plecarea din voce.
        if not write_track_meta(filename, {
                'url': web_url, 'title': state.last_title,
                'channel': state.last_channel, 'duration': state.last_duration,
                'thumbnail': state.last_thumbnail, 'views': state.last_views,
                'likes': state.last_likes}):
            log.debug("Metadatele piesei nu au putut fi scrise langa fisier")

        state._consecutive_errors = 0
        state._consecutive_rejects = 0
        state.breaker_until = 0.0
        # Jar-ul care A AUTENTIFICAT devine ultima copie buna — nu orice redare
        # reusita. Distinctia e critica: cookies_valid() e pur STRUCTURALA (are
        # jar-ul un nume din COOKIE_CRITICAL?), iar putrezirea tipica pastreaza
        # numele si omoara valorile. Deci un `promote` la fiecare succes stampila
        # un jar deja refuzat de YouTube peste singura copie care functionase, in
        # exact cazurile care nu vorbesc deloc cu YouTube: cache hit pe disc,
        # loop pe acelasi URL, sau o descarcare care a căzut inapoi pe guest.
        # Dupa aceea prima piesa nouă eșua, revenirea restaura jar-ul mort, si
        # ultimele credentiale bune nu mai existau nicaieri.
        if jar_authenticated and cookies_available():
            promote_cookies()
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
                vid_id = video_id(web_url)
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
        _discard_partial(filename, reused)
        raise
    except PlaybackInterrupted as e:
        # !stop, deconectare sau mutare din canal in timpul descarcarii. Nu e o
        # defectiune: inainte urca numaratoarea de erori spre intrerupator si ii
        # arunca utilizatorului "Eroare necunoscuta" pentru propria lui comanda.
        # Tip propriu, nu ConnectionError: acela acopera si erorile reale de
        # retea, care trebuie sa rămâna vizibile.
        log.info(f"Redare intrerupta: {e}")
        _discard_partial(filename, reused)
        _log_play_result('intrerupt', started_at, query, url=web_url,
                         reused=reused, reason=e)
    except TrackRejected as e:
        # Regula noastra, nu defectiune: nu atinge _consecutive_errors si nu
        # trece prin diagnose_error, care ar traduce-o in "Eroare necunoscuta".
        log.info(f"Piesa refuzata: {e}")
        _discard_partial(filename, reused)
        state._consecutive_rejects += 1
        rejected = str(e)
        _log_play_result('refuzat', started_at, query, url=web_url,
                         reused=reused, reason=e)
    except Exception as e:
        log.error(f"Eroare process_play: {e}", exc_info=True)
        _discard_partial(filename, reused)
        state._consecutive_errors += 1
        failure = e
        _log_play_result('eroare', started_at, query, url=web_url,
                         reused=reused, reason=state.last_raw_error or e)
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
            # `breaker_until` si golirea cozii nu sunt opționale, si lipsa lor facea
            # din mesajul "Ma opresc" o minciuna sub 24/7. `decide_idle_action`
            # citeste `autoplay=False` cu `autoplay_user_off=False` ca pe o
            # defectiune si reia radioul, iar tick-ul sare peste prefill cat timp
            # coada nu e goala — deci la 60 de secunde `play_next` se intorcea pe
            # exact aceleasi elemente. Rezultat masurat: 6 mesaje pe Discord si 5
            # extractii complete pe minut, pana la epuizarea cozii.
            #
            # Coada se goleste fiindca tocmai a fost declarata neredabila: asa
            # tick-ul urmator aduce material nou in loc sa reia acelasi.
            state.breaker_until = time.time() + REJECT_COOLDOWN_SEC
            state.queue.clear()
            log.info(f"Prea multe refuzuri la rand: pauza {REJECT_COOLDOWN_SEC}s "
                     f"si coada golita")
            start_timeout(ctx)
        elif _current_track_survived(ctx, state):
            log.info("Refuz peste o piesa care cânta: coada rămâne neatinsa")
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

    # Cookie-urile sunt singura cale reala de pe un IP de datacenter, iar cand
    # YouTube invalideaza sesiunea yt-dlp scrie peste fisier valori care nu mai
    # autentifica. Daca avem o copie buna, o punem la loc si reincercam o singura
    # data, in loc sa cerem omului sa lipeasca cookie-uri noi.
    if error_type == 'cookies' and not after_rollback and rollback_cookies():
        log.warning("Reincerc piesa cu jar-ul de cookies restaurat")
        state._consecutive_errors = max(0, state._consecutive_errors - 1)
        return await process_play(ctx, query, is_radio=is_radio,
                                  after_rollback=True)

    # Dedup-ul e pentru redarile AUTOMATE: cand radio-ul arde o coada intreaga cu
    # aceeasi cauza, un mesaj per piesa e spam. O piesa pe care a cerut-o un om
    # primeste insa mereu un raspuns — altfel comanda lui pare pur si simplu
    # ignorata. Pe `/play` era chiar mai rau: interactiunea rămânea in "Gogu is
    # thinking..." pentru totdeauna, fiindca al doilea eșec identic nu trimitea nimic.
    if state._last_notified_error != error_type or not is_radio:
        state._last_notified_error = error_type
        try:
            await ctx.send(user_msg, delete_after=60)
        except DISCORD_ERRORS as e:
            log.warning(f"Nu am putut raporta eroarea utilizatorului: {e}")

    await asyncio.sleep(min(2 * state._consecutive_errors, 15))
    if _current_track_survived(ctx, state):
        log.info("Eroare peste o piesa care cânta: coada rămâne neatinsa")
    elif state.autoplay or state.queue:
        play_next(ctx)
    else:
        start_timeout(ctx)
