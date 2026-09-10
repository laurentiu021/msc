"""Negocierea cu yt-dlp: de la o interogare la un fisier pe disc.

Erau 122 de linii in mijlocul lui `process_play`, care avea 300. Sunt exact
partea care trebuie re-reglata la fiecare schimbare a YouTube-ului, si totodata
cea mai greu de testat: ca sa verifici o singura afirmatie despre opts-urile de
descarcare trebuia un client de voce fals, un ctx fals si un harness care
inlocuia sase globale din player, doua functii din ytdlp si FFmpegOpusAudio.

Aici nu exista `ctx`, nici `GuildState`, nici discord: intra o interogare, iese un
`Resolved`. Asta scoate din cod si un defect prin construcție —
`state.last_raw_error` era o cutie poștala intre piese, folosita ca sa treaca text
peste o granita raise/except, niciodata golita la succes, si preferata excepției
reale la raportare. Acum textul brut de la yt-dlp se intoarce ca valoare.

Refuzurile (live, durata) NU se ridica: se intorc in `reject_reason`. Cine cheama
decide daca asta e o eroare sau doar o piesa sarita.
"""
import asyncio
import os
from dataclasses import dataclass, field

from music import ytdlp
from music.config import (HLS_MAX_BYTES, MAX_DOWNLOAD_BYTES, MAX_TRACK_SECONDS,
                          clear_ydl_reason, cookies_available,
                          count_real_formats, duration_within_limits,
                          has_real_formats, last_ydl_reason, log,
                          make_download_opts, make_search_opts,
                          search_query, yt_client_args, WEB_CLIENTS)
from music.errors import YtdlpTimeout, diagnose_error
from music.utils import AUDIO_EXTS, cached_download, is_clean, item_title

# Lanturile de clienti. Cerem ambii clienti in ACEEASI cerere: yt-dlp cumuleaza
# formatele, deci pool-ul e mult mai mare pe acelasi numar de cereri. Masurat in
# producție: mweb singur a dat 5 formate / 1 redabil, perechea a dat 40 / 13. Un
# singur format redabil e o marja prea subtire pentru redare.
COOKIE_CHAIN = [(WEB_CLIENTS, True)]
GUEST_CHAIN = [(WEB_CLIENTS, False)]

# (selector de format, plafon de octeti), in ordinea incercarilor.
#
# A doua incercare cere DOAR audio din HLS. `best[protocol^=m3u8]` era ultima
# alternativa si e o redare muxata video+audio, iar `-vn` arunca imaginea abia
# DUPA ce a ajuns pe disc: un bot audio descarca astfel zeci de MB de video pe un
# IP care ne limiteaza.
#
# ATENTIE: plafonul de octeti NU se aplica pe HLS. Verificat pe yt-dlp 2026.8.19 —
# `max_filesize` e citit doar de HttpFD (downloader/http.py) si de CurlFD; toate
# formatele HLS de YouTube sunt `m3u8_native`, adica HlsFD, iar nici
# downloader/hls.py nici downloader/fragment.py nu il pomenesc. Deci singurul lucru
# care marginea un transfer HLS e DOWNLOAD_TIMEOUT_SEC, si de aceea selectorul
# trebuie sa fie strict audio: comentariul de aici obișnuia sa promita un plafon pe
# care codul nu il aplica.
DOWNLOAD_ATTEMPTS = [
    ('bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
     MAX_DOWNLOAD_BYTES),
    ('bestaudio[protocol^=m3u8]/bestaudio*[protocol^=m3u8]',
     HLS_MAX_BYTES),
]


def format_summary(info: dict | None) -> str:
    """Ce a ales de fapt selectorul de format.

    Sirul de format din log e o CERERE, nu un rezultat. Fara linia asta,
    `bestaudio[acodec=opus]/.../best` acoperea la fel de bine un opus de 130 kbps
    si un AAC de 48 kbps muxat cu video — adica singura intrebare care conteaza
    cand cineva spune "se aude prost" nu avea niciun raspuns in loguri.
    """
    if not isinstance(info, dict):
        return '?'
    picked = (info.get('requested_downloads') or [{}])[0] or {}

    def field(key):
        value = picked.get(key)
        return info.get(key) if value is None else value

    return (f"id={field('format_id')} acodec={field('acodec')} "
            f"abr={field('abr')} asr={field('asr')} vcodec={field('vcodec')} "
            f"proto={field('protocol')} ext={field('ext')}")



@dataclass
class Resolved:
    """Rezultatul unei rezolvari. Fara efecte pe stare, doar date."""

    url: str = ''
    info: dict = field(default_factory=dict)
    download_info: dict | None = None
    filename: str | None = None
    # Textul BRUT de la yt-dlp, pentru diagnoza. Se intoarce, nu se scrie in
    # stare: asa nu poate fi raportat drept cauza pentru o piesa de mai tarziu.
    raw_error: str | None = None
    # Refuzat de reguli (live, durata, filtre) — nu o defectiune.
    reject_reason: str | None = None
    # Utilizatorul a oprit redarea intre etape; nici eroare, nici refuz.
    interrupted: bool = False
    # Clientul si modul de cookies care au produs formate redabile.
    client: tuple | None = None
    used_cookies: bool = False
    # Modul de cookies al cererii care a scris CHIAR fisierul. Separat de
    # `used_cookies` fiindca bucla de descarcare are propriul fallback la guest:
    # extractia poate reusi cu jar-ul si descarcarea fara el. Doar asta dovedeste
    # ca jar-ul a autentificat ceva, deci doar asta poate promova o copie buna.
    download_used_cookies: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.filename and os.path.exists(self.filename))


def unplayable_reason(info) -> str | None:
    """Motiv pentru care piesa nu are ce sa caute in redare, sau None.

    Se aplica si pe URL-uri directe, nu doar pe rezultatele de cautare. Pana
    acum un link de live sau de podcast de trei ore trecea intreaga extractie,
    intra in bucla de descarcare, era respins tacut de match_filter, si
    utilizatorul primea "Niciun format nu a reusit descarcarea" — un mesaj care
    arata ca o defectiune, nu ca o regula.

    Verifica DOAR ce e o limita operationala reala: un live nu se termina
    niciodata, iar peste MAX_TRACK_SECONDS trecem bugetul de descarcare si limita
    de fisier. Blocklist-ul, similaritatea si durata MINIMA din is_clean servesc
    alegerea AUTOMATA (cautare, autoplay), unde scopul e sa nu culegem teasere si
    shorts; cand cineva da explicit un link de 20 de secunde, singurul lucru
    corect e sa il redam.
    """
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
        return "E un live, nu o piesa"
    duration = info.get('duration')
    # Exact regula pe care o aplica match_filter la descarcare, prin acelasi
    # predicat — nu o a doua propozitie despre acelasi lucru. Vezi comentariul de
    # la MATCH_FILTER_EXPR: cele doua formulari nu erau echivalente, deci o piesa
    # de exact MAX_TRACK_SECONDS (sau fara durata raportata) trecea de aici, plătea
    # o descarcare completa, si era refuzata tacut de yt-dlp.
    if not duration_within_limits(duration):
        if not duration:
            return "YouTube nu spune cat dureaza, deci nu o pot descarca"
        return (f"Piesa are {int(duration // 60)} minute, limita e "
                f"{MAX_TRACK_SECONDS // 60}")
    return None


def worth_another_format(raw_error: str | None) -> bool:
    """Merita a doua incercare cu alt selector de format?

    Doar cand eșecul e chiar despre formate. Un 429, un cookie expirat sau un
    video indisponibil dau acelasi raspuns oricat de diferit ai scrie selectorul,
    deci o a doua rundă e doar o cerere in plus pe un IP deja limitat — si inca
    una cu buget propriu de 240 de secunde.
    """
    if not raw_error:
        # Nicio eroare raportata inseamna respins de filtru (durata, live), nu o
        # problema de format.
        return False
    error_type, _ = diagnose_error(raw_error)
    worth = error_type in ('format', 'unknown')
    if not worth:
        log.info(f"Nu mai incerc alt format: cauza e '{error_type}', "
                 f"nu selectorul de format")
    return worth


def video_id(url: str) -> str | None:
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


async def search_to_url(query: str, *, avoid_title: str = '',
                        loop=None) -> tuple[str | None, str | None]:
    """(url, motiv_refuz). Un URL trece direct; un text devine o cautare FLAT.

    Cautarea flat intoarce doar metadata de lista (id, titlu, durata,
    live_status), fara sa atinga pagina si API-ul player pentru fiecare rezultat.
    Fara extract_flat, yt-dlp extragea integral toate cele cinci rezultate:
    aproximativ 20 de cereri pentru un singur !play, toate in acelasi slot de
    throttle si acelasi buget de 90s.
    """
    if str(query).startswith(('http://', 'https://')):
        return query, None

    opts = make_search_opts(
        with_cookies=cookies_available(),
        extract_flat=True,
        extractor_args=yt_client_args(*WEB_CLIENTS),
    )
    # Prefixul EXPLICIT, nu `default_search`: acela e aplicat de extractorul generic
    # sub forma unui url_result care trebuie procesat, iar `extract_flat=True`
    # inseamna "nu procesa niciodata" — combinatia intorcea zero rezultate, fara
    # nicio eroare. Vezi config.SEARCH_PREFIX.
    info = await ytdlp.extract(opts, search_query(query), loop=loop,
                               stage='search_flat')
    entries = [e for e in ((info or {}).get('entries') or []) if e]
    if not entries:
        return None, None            # nimic gasit: eroare, nu refuz

    for entry in entries:
        if is_clean(entry.get('title'), entry.get('duration'), avoid_title):
            log.info(f"Ales din {len(entries)} rezultate: {item_title(entry, 60)}")
            url = entry.get('url') or entry.get('id')
            if url and not str(url).startswith('http'):
                url = f"https://www.youtube.com/watch?v={url}"
            return url, None
        log.info(f"Sarit (filtru): {item_title(entry, 50)} "
                 f"durata={entry.get('duration')} live={entry.get('live_status')}")

    # Niciunul nu trece filtrul. Inainte se lua orbeste entries[0], deci filtrul
    # nu putea respinge nimic si un live de 3 ore ajungea in redare.
    return None, (f"toate cele {len(entries)} rezultate au fost filtrate "
                  f"(live, prea scurte sau prea lungi)")


async def _pick_format_source(target_url: str, loop) -> tuple[dict | None, tuple | None,
                                                              bool, str | None]:
    """Lantul de clienti: (candidat, client_reusit, cu_cookies, eroare_bruta).

    Cookies primele: de pe IP-ul de datacenter al Railway calea de guest ajunge la
    429 pe webpage -> lipsa Visitor Data -> niciun GVS PO Token -> zero formate
    redabile. Guest rămâne in coada pentru cand IP-ul nu e limitat.
    """
    chains = COOKIE_CHAIN + GUEST_CHAIN if cookies_available() else GUEST_CHAIN
    selected = None
    raw_error = None
    for clients, use_cookies in chains:
        label = '+'.join(clients)
        if use_cookies and not cookies_available():
            continue
        search_opts = make_search_opts(
            with_cookies=use_cookies,
            extractor_args=yt_client_args(*clients),
            )
        try:
            info = await ytdlp.extract(search_opts, target_url, loop=loop,
                                       stage=f"extract_{label}")
            entries = (info or {}).get('entries') or [info]
            candidate = entries[0] if entries else None
            if not candidate:
                continue
            fmts = candidate.get('formats', [])
            log.info(f"[{label}|cookies={use_cookies}] Video "
                     f"{candidate.get('id','?')}: {len(fmts)} formats "
                     f"({count_real_formats(fmts)} redabile)")
            selected = candidate
            if has_real_formats(fmts):
                log.info(f"Formate redabile cu client={label}, cookies={use_cookies}")
                return selected, clients, use_cookies, raw_error
        except Exception as e:
            raw_error = str(e)[:600]
            log.warning(f"Extractia a eșuat cu client={label}: {e}", exc_info=True)
    return selected, None, False, raw_error


async def _retry_for_formats(selected: dict, web_url: str, loop):
    """O a doua extractie cand nu avem niciun format redabil.

    Valoarea ei nu e ca "poate merge acum": e ca `ignore_no_formats_error=False`
    scoate motivul REAL ("Sign in to confirm you're not a bot" era doar warning),
    iar acela ajunge la diagnoza si la utilizator.

    Intoarce (info, eroare_bruta, client, cu_cookies): ULTIMELE doua conteaza, si
    lipseau. Reincercarea foloseste WEB_CLIENTS si `cookies_available()`, dar
    apelantul pastra `(None, False)` de la selectia care eșuase, deci descarcarea
    pornea exact cu modul de cookies care abia dăduse 0 formate redabile.
    """
    vid_id = selected.get('id', '?')
    log.warning(f"0 formate redabile pentru {vid_id} — aștept 5s si reincerc")
    await asyncio.sleep(5)
    retry_url = web_url if web_url.startswith('http') else \
        f"https://www.youtube.com/watch?v={vid_id}"
    with_cookies = cookies_available()
    retry_opts = make_search_opts(
        with_cookies=with_cookies,
        extractor_args=yt_client_args(*WEB_CLIENTS),
        ignore_no_formats_error=False,
    )
    try:
        retry_info = await ytdlp.extract(retry_opts, retry_url, loop=loop,
                                         stage="retry_mweb")
        retry_fmts = (retry_info or {}).get('formats', [])
        log.info(f"[retry] Video {vid_id}: {len(retry_fmts)} formate "
                 f"({count_real_formats(retry_fmts)} redabile)")
        if has_real_formats(retry_fmts):
            log.info(f"Reincercarea a reusit pentru {vid_id}")
            return retry_info, None, WEB_CLIENTS, with_cookies
        log.warning(f"Si reincercarea a dat 0 formate redabile pentru {vid_id}")
        return None, None, None, False
    except Exception as e:
        log.warning(f"Reincercarea a eșuat pentru {vid_id}: {e}", exc_info=True)
        return None, str(e)[:600], None, False


async def _download(web_url: str, client: tuple | None, prefer_cookies: bool,
                    loop, raw_error: str | None
                    ) -> tuple[dict | None, str | None, str | None, bool]:
    """(download_info, filename, eroare_bruta, cu_cookies).

    Ultimul element spune daca fisierul a fost scris de o cerere care DUCEA
    jar-ul. Se intoarce pentru ca apelantul promoveaza copia "ultima buna" de
    cookies pe baza lui: fara el, o descarcare reusita ca guest era luata drept
    dovada ca jar-ul mai autentifica si stampila peste singura copie care chiar
    functionase.

    Format-ul in bucla EXTERIOARA, modul de cookies in cea interioara: un 429 sau
    un cookie expirat nu devine alt raspuns daca intrebi cu alt sir de format,
    deci varianta cu format-ul in interior putea plati patru extractii complete
    plus patru transferuri partiale, fiecare cu buget propriu de 240s, pentru
    aceeasi cauza.
    """
    clear_ydl_reason()
    cookie_order = [True, False] if prefer_cookies else [False, True]
    dl_info = None
    filename = None
    # Eroarea de la EXTRACTIE nu se amesteca cu cele de la descarcare. Cand era
    # transmisa incoace ca valoare de start, poarta HLS de mai jos decidea pe baza
    # unei erori de la o cerere complet diferita, iar garda din apelant
    # (`not resolved.raw_error`) nu ajungea niciodata sa consulte
    # `last_ydl_reason()` — singurul canal prin care vine refuzul real al lui
    # yt-dlp, care pe calea de match_filter nu ridica nicio excepție.
    dl_error = None
    # "Merita alt selector de format?" se decide pe ORICE incercare care a eșuat
    # din motiv de format, nu doar pe ultima: un 'format' de la modul cu cookies
    # era altfel aruncat cand modul guest cadea cu 429.
    format_worthy = False
    for fmt, size_cap in DOWNLOAD_ATTEMPTS:
        if filename and os.path.exists(filename):
            break
        if fmt != DOWNLOAD_ATTEMPTS[0][0] and not format_worthy:
            break
        for use_cookies in cookie_order:
            if use_cookies and not cookies_available():
                continue
            try:
                overrides = {'format': fmt, 'max_filesize': size_cap}
                if client:
                    overrides['extractor_args'] = yt_client_args(*client)
                dl_opts = make_download_opts(with_cookies=use_cookies, **overrides)
                log.info(f"Download cookies={use_cookies}, "
                         f"client={'+'.join(client) if client else 'default'}, "
                         f"format={fmt}")
                # O singura instanta YoutubeDL descarca SI construieste numele
                # fisierului; inainte erau doua, iar cea externa exista doar
                # pentru prepare_filename.
                dl_info, filename = await ytdlp.extract_and_prepare_filename(
                    dl_opts, web_url, loop=loop, stage=f"download_{fmt}")
                if filename and not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in AUDIO_EXTS:
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                if filename and os.path.exists(filename):
                    log.info(f"Format descarcat: {format_summary(dl_info)}")
                    # Succes: nu raportam nicio eroare, nici a extractiei, nici a
                    # incercarilor anterioare. Un text brut lasat aici ar fi ajuns
                    # in `state.last_raw_error` pe calea reusita si ar fi devenit
                    # diagnoza pentru urmatorul eșec.
                    return dl_info, filename, None, use_cookies
            except YtdlpTimeout as e:
                # OPRIRE, nu urmatoarea incercare. `outtmpl` e `%(id)s.%(ext)s`,
                # deci calea de pe disc E cheia de cache si nu exista lock per id:
                # thread-ul abandonat continua sa scrie in ACELASI fisier. Al
                # doilea scriitor vede `.part`-ul existent, seteaza `resume_len` si
                # `open_mode='ab'` (yt-dlp downloader/http.py) si adauga in fisierul
                # in care primul inca scrie; mai rau, poate decide ca fisierul e
                # complet si sa redenumeasca un `.part` care inca creste peste
                # numele final din cache. Excluderea `.part` din cached_download nu
                # apara de asta, iar rezultatul stricat s-ar pastra apoi la infinit.
                dl_error = str(e)[:600]
                log.warning(f"Renunt la reincercari: thread-ul abandonat inca "
                            f"scrie in acelasi fisier ({e})")
                return dl_info, None, dl_error, False
            except Exception as e:
                dl_error = str(e)[:600]
                if worth_another_format(dl_error):
                    format_worthy = True
                log.warning(f"Download esuat (cookies={use_cookies}, "
                            f"fmt='{fmt}'): {e}")
    return dl_info, filename, dl_error or raw_error, False


async def resolve_from_url(target_url: str, *, loop=None,
                           should_continue=None) -> Resolved:
    """De la un URL de videoclip la un fisier pe disc. Nu atinge nicio stare.

    `should_continue` se consulta intre etape, ca redarea sa poata fi abandonata
    cand utilizatorul a dat deja `!stop` — inainte se verifica doar la final, dupa
    ce toate cererile fuseseră deja plătite.
    """
    def keep_going() -> bool:
        return should_continue() if should_continue else True

    resolved = Resolved(url=target_url)

    selected, client, used_cookies, raw_error = await _pick_format_source(
        target_url, loop)
    resolved.raw_error = raw_error
    if not selected:
        return resolved

    resolved.info = selected
    resolved.client = client
    resolved.used_cookies = used_cookies
    resolved.url = (selected.get('webpage_url')
                    or f"https://www.youtube.com/watch?v={selected.get('id', '')}")

    if not has_real_formats(selected.get('formats', [])):
        (retry_info, retry_error, retry_client,
         retry_cookies) = await _retry_for_formats(selected, resolved.url, loop)
        if retry_error:
            resolved.raw_error = retry_error
        if retry_info:
            resolved.info = selected = retry_info
            # Descarcarea porneste cu combinatia care A FUNCTIONAT, nu cu cea care
            # abia dăduse 0 formate redabile.
            resolved.client = client = retry_client
            resolved.used_cookies = used_cookies = retry_cookies
        else:
            resolved.raw_error = (resolved.raw_error
                                  or "YouTube a blocat acest video (0 formate reale)")
            return resolved

    # Regulile se aplica si pe URL-uri directe, nu doar pe cautare.
    reason = unplayable_reason(selected)
    if reason:
        resolved.reject_reason = reason
        return resolved

    if not keep_going():
        resolved.interrupted = True
        return resolved

    (resolved.download_info, resolved.filename, resolved.raw_error,
     resolved.download_used_cookies) = await _download(
        resolved.url, client, used_cookies, loop, resolved.raw_error)

    if not resolved.ok and not resolved.raw_error:
        # Nicio excepție ridicata: yt-dlp raporteaza refuzul doar prin to_screen,
        # care fara logger nu scrie nimic. Cu logger avem propozitia lui, care e
        # mult mai buna decat o presupunere a noastra.
        resolved.raw_error = last_ydl_reason() or (
            "yt-dlp nu a scris fisierul si nu a raportat nicio eroare; "
            f"probabil respins de filtru (live sau durata peste "
            f"{MAX_TRACK_SECONDS}s)")
        log.warning(f"Descarcare fara fisier: {resolved.raw_error}")
    return resolved


def cached_for(url: str) -> tuple[str | None, str | None]:
    """(id, cale_in_cache) pentru un URL. Cale None cand nu avem fisierul."""
    vid = video_id(url)
    return vid, (cached_download(vid) if vid else None)
