"""Canarul: intreaba YouTube-ul o data pe zi dacă mai da ce trebuie.

De ce exista. yt-dlp e pinuit, iar YouTube schimba lucruri fara sa anunțe. Doua
dintre problemele cele mai scumpe ale botului au fost exact de acest fel, si
amandoua s-au aflat abia cand cineva a incercat sa asculte muzica:

- `default_search` a devenit incompatibil cu `extract_flat` — orice cautare de
  text intorcea zero rezultate, fara nicio eroare si fara nicio linie de log;
- experimentul SABR-only a Șters formatele opus pentru contul din cookies, deci
  fiecare piesa a inceput sa fie reencodata din AAC.

Canarul le-ar fi prins pe amandoua a doua zi, nu peste o luna. Ce verifica, si
NIMIC mai mult, ca sa rămâna ieftin (doua cereri pe zi):

1. o cautare de text intoarce rezultate;
2. extractia unui clip cunoscut da un format audio-only in opus.

Nu descarca nimic: transferul nu spune nimic in plus, iar plafonul de cache si
volumul nu au ce sa caute intr-o verificare de sanatate.
"""
import time

from music.config import (SEARCH_PREFIX, WEB_CLIENTS, cookies_available,
                          has_opus_audio, log, make_search_opts, yt_client_args)
from music import ytdlp

# Un clip care exista de mult si nu e nici live, nici restricționat pe varsta.
# Daca dispare, canarul o spune singur — un fals pozitiv zgomotos e exact ce
# trebuie aici, spre deosebire de o defectiune tacuta.
CANARY_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
CANARY_QUERY = 'rick astley never gonna give you up'


async def run(loop=None) -> dict:
    """Intoarce un raport: {'ok': bool, 'search': int, 'opus': bool, ...}."""
    report = {'ok': True, 'search': 0, 'opus': False, 'formats': 0,
              'cookies': cookies_available(), 'errors': []}
    started = time.time()

    opts = make_search_opts(with_cookies=cookies_available(), extract_flat=True,
                            extractor_args=yt_client_args(*WEB_CLIENTS))
    try:
        info = await ytdlp.extract(opts, f'{SEARCH_PREFIX}{CANARY_QUERY}',
                                   loop=loop, stage='canary_search')
        report['search'] = len([e for e in ((info or {}).get('entries') or []) if e])
    except Exception as e:                                     # noqa: BLE001
        # `Exception`, larg dinadins: canarul trebuie sa raporteze ORICE mod in
        # care cautarea se poate rupe, inclusiv unul pe care nu l-am prevazut. De
        # asta se si logheaza — o defectiune tacuta e chiar ce el exista sa prinda.
        log.warning(f"Canar: cautarea a eșuat: {e}", exc_info=True)
        report['errors'].append(f'cautare: {type(e).__name__}: {str(e)[:160]}')
    if not report['search']:
        report['ok'] = False

    opts = make_search_opts(with_cookies=cookies_available(),
                            extractor_args=yt_client_args(*WEB_CLIENTS))
    try:
        info = await ytdlp.extract(opts, CANARY_URL, loop=loop,
                                   stage='canary_extract')
        formats = (info or {}).get('formats') or []
        report['formats'] = len(formats)
        report['opus'] = has_opus_audio(formats)
    except Exception as e:                                     # noqa: BLE001
        log.warning(f"Canar: extractia a eșuat: {e}", exc_info=True)
        report['errors'].append(f'extractie: {type(e).__name__}: {str(e)[:160]}')
    if not report['formats']:
        report['ok'] = False

    report['elapsed'] = round(time.time() - started, 1)
    return report


def describe(report: dict) -> str:
    """O singura linie, buna de citit si de grep-at."""
    return ('CANARY ' + ' '.join([
        f"ok={int(bool(report.get('ok')))}",
        f"cautare={report.get('search', 0)}",
        f"formate={report.get('formats', 0)}",
        f"opus={int(bool(report.get('opus')))}",
        f"cookies={int(bool(report.get('cookies')))}",
        f"elapsed={report.get('elapsed', 0)}s",
    ]) + (f" erori={report['errors']}" if report.get('errors') else ''))


def regressions(report: dict, previous: dict | None) -> list[str]:
    """Ce s-a INRAUTATIT fata de raportul precedent.

    Diferenta conteaza mai mult decat starea: "0 rezultate" e alarmant abia cand
    ieri erau cinci. Fara comparatie, un canar care raporteaza aceeasi stare
    proasta zilnic devine zgomot pe care nimeni nu-l mai citește.
    """
    out = []
    if not report.get('search'):
        out.append('cautarea de text nu mai intoarce nimic')
    if not report.get('formats'):
        out.append('extractia nu mai da niciun format')
    if previous is not None:
        if previous.get('opus') and not report.get('opus'):
            out.append('formatele opus au dispărut: redarea trece pe reencodare')
        if previous.get('search') and not report.get('search'):
            out.append('cautarea functiona ieri si nu mai functioneaza')
    elif not report.get('opus'):
        out.append('niciun format opus (redarea va fi reencodata)')
    for err in report.get('errors') or []:
        out.append(err)
    return out


async def check_and_report(loop=None, previous=None, announce=None) -> dict:
    """Ruleaza canarul, logheaza, si anunța DOAR regresiile."""
    report = await run(loop=loop)
    log.info(describe(report))
    problems = regressions(report, previous)
    if problems and announce is not None:
        await announce('⚠️ **Canar YouTube:** ' + '; '.join(problems[:4]))
    return report
