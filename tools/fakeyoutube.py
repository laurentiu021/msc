"""YouTube fals, cu latente realiste, plus FFmpeg REAL peste audio real.

Latentele nu sunt inventate: sunt cele masurate in logurile de producție ale
botului — ~1.4s pentru o cautare flat, ~2s pentru extracția unui videoclip,
~6-25s pentru descarcare in funcție de 429-uri. Contează, fiindca bug-urile de
concurenta din botul asta apar exact in fereastra dintre cerere si raspuns.

Audio-ul e real: FFmpeg genereaza un ton, iar `make_opus_source` il trece prin
FFmpeg-ul adevarat, deci `vc.play` citește cadre Opus reale. Asa se verifica si ce
bitrate cere codul, nu doar ce crede el ca cere.
"""
import asyncio
import os
import subprocess
import time

CATALOG = {}


def make_track(vid, title, *, duration=180, channel='Canalul', live=False,
               acodec='opus'):
    return {
        'id': vid,
        'title': title,
        'duration': duration,
        'channel': channel,
        'uploader': channel,
        'webpage_url': f'https://www.youtube.com/watch?v={vid}',
        'live_status': 'is_live' if live else None,
        'thumbnail': f'https://i.ytimg.com/vi/{vid}/default.jpg',
        'view_count': 1234567,
        'like_count': 4321,
        'acodec': acodec,
        'formats': [{'acodec': acodec, 'vcodec': 'none', 'abr': 130.0,
                     'url': f'https://x/{vid}', 'protocol': 'https',
                     'format_id': '251', 'ext': 'webm', 'asr': 48000}],
        'requested_downloads': [{'format_id': '251', 'acodec': acodec,
                                 'abr': 130.0, 'protocol': 'https',
                                 'ext': 'webm', 'vcodec': 'none',
                                 'asr': 48000}],
    }


def seed(n=40):
    """Un catalog determinist: artiȘti repetati, ca sa se vada plafonul de artist."""
    CATALOG.clear()
    artists = ['Luis Gabriel', 'Los Del Rio', 'Delia', 'Carla', 'Guta']
    for i in range(n):
        vid = f'vid{i:03d}'
        artist = artists[i % len(artists)]
        CATALOG[vid] = make_track(vid, f'{artist} - Piesa {i}',
                                  duration=120 + (i * 7) % 300,
                                  channel=f'{artist} Official')
    return CATALOG


def audio_fixture(directory, seconds=600):
    """Un fisier audio REAL, generat cu FFmpeg. Cache-uit intre rulari.

    LUNG dinadins. Cu un fixture de cateva secunde, piesa se termina singura in
    mijlocul scenariului, `after_play` avanseaza coada, iar tot ce urmeaza masoara
    altceva decat crede — s-a intamplat, si arata exact ca doua bug-uri false
    ("is_loading a rămas aprins", "pauza a lasat redarea oprita").
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, '_fixture.webm')
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', f'sine=frequency=440:duration={seconds}',
         '-c:a', 'libopus', '-b:a', '128k', path],
        check=True, timeout=120)
    return path


class FakeYouTube:
    """Inlocuiește `music.ytdlp.extract` si `extract_and_prepare_filename`."""

    def __init__(self, download_dir, *, search_sec=1.4, extract_sec=2.0,
                 download_sec=6.0, fail_ids=(), timeout_ids=(),
                 unavailable_ids=()):
        self.download_dir = download_dir
        self.search_sec = search_sec
        self.extract_sec = extract_sec
        self.download_sec = download_sec
        self.fail_ids = set(fail_ids)
        self.timeout_ids = set(timeout_ids)
        self.unavailable_ids = set(unavailable_ids)
        self.calls = []
        self.timeline = None
        self.fixture = None
        # Concurenta observata: cate cereri au fost simultan in zbor.
        self.inflight = 0
        self.max_inflight = 0

    def install(self):
        from music import ytdlp
        self._saved = (ytdlp.extract, ytdlp.extract_and_prepare_filename)
        ytdlp.extract = self.extract
        ytdlp.extract_and_prepare_filename = self.extract_and_prepare_filename
        self.fixture = audio_fixture(self.download_dir)
        return self

    def restore(self):
        from music import ytdlp
        ytdlp.extract, ytdlp.extract_and_prepare_filename = self._saved

    def _note(self, stage, detail=''):
        self.calls.append((stage, detail, time.monotonic()))
        if self.timeline is not None:
            self.timeline.add(f'yt.{stage}', detail)

    async def _wait(self, seconds):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.inflight -= 1

    def _id_from(self, query):
        query = str(query or '')
        if 'v=' in query:
            return query.split('v=')[-1].split('&')[0]
        if 'youtu.be/' in query:
            return query.split('youtu.be/')[-1].split('?')[0]
        return None

    async def extract(self, opts, query, download=False, loop=None, stage=''):
        from yt_dlp.utils import DownloadError

        self._note(stage or 'extract', str(query)[:70])
        if stage == 'search_flat':
            await self._wait(self.search_sec)
            text = str(query).split(':', 1)[-1].lower()
            hits = [t for t in CATALOG.values() if text.split()[0] in t['title'].lower()]
            entries = (hits or list(CATALOG.values()))[:5]
            return {'entries': [{'id': t['id'], 'title': t['title'],
                                 'duration': t['duration'],
                                 'live_status': t['live_status'],
                                 'channel': t['channel'],
                                 'url': t['webpage_url']} for t in entries]}
        if stage == 'playlist':
            await self._wait(self.extract_sec)
            entries = list(CATALOG.values())[:30]
            return {'entries': [{'id': t['id'], 'title': t['title'],
                                 'url': t['webpage_url']} for t in entries]}
        if stage.startswith('mix') or 'list=RD' in str(query):
            await self._wait(self.extract_sec)
            entries = list(CATALOG.values())[:50]
            return {'entries': [{'id': t['id'], 'title': t['title'],
                                 'duration': t['duration'],
                                 'live_status': None,
                                 'channel': t['channel'],
                                 'url': t['webpage_url']} for t in entries]}

        vid = self._id_from(query)
        if vid in self.timeout_ids:
            from music.ytdlp import YtdlpTimeout
            await self._wait(self.extract_sec)
            raise YtdlpTimeout(f'{vid}: timeout simulat')
        await self._wait(self.extract_sec)
        if vid in self.unavailable_ids:
            raise DownloadError(f'ERROR: {vid}: Video unavailable')
        track = CATALOG.get(vid)
        if track is None:
            raise DownloadError(f'ERROR: {vid}: Video unavailable')
        return dict(track)

    async def extract_and_prepare_filename(self, opts, query, loop=None, stage=''):
        import shutil

        vid = self._id_from(query)
        self._note(stage or 'download', vid or str(query)[:40])
        if vid in self.timeout_ids:
            from music.ytdlp import YtdlpTimeout
            await self._wait(self.download_sec)
            raise YtdlpTimeout(f'{vid}: timeout la descarcare')
        await self._wait(self.download_sec)
        if vid in self.fail_ids or vid is None:
            from yt_dlp.utils import DownloadError
            raise DownloadError(f'ERROR: {vid}: Requested format is not available')
        track = CATALOG.get(vid)
        if track is None:
            from yt_dlp.utils import DownloadError
            raise DownloadError(f'ERROR: {vid}: Video unavailable')
        target = os.path.join(self.download_dir, f'{vid}.webm')
        if not os.path.exists(target):
            shutil.copyfile(self.fixture, target)
        return dict(track), target


class RealYouTube:
    """YouTube ADEVARAT. Aceeasi interfata ca FakeYouTube, dar fara sa mimeze nimic.

    Nu inlocuiește `music.ytdlp`: il inveleste, ca sa treaca prin exact acelasi
    throttle, acelasi executor si acelasi lant de clienti ca in producție. Doua
    ajustari, ambele strict locale:

    - `js_runtimes={'node': {}}`: yt-dlp activeaza implicit doar Deno pentru
      challenge-ul `n`, iar aici e Node. Fara asta, formatele opus lipsesc si
      masuratoarea de calitate nu inseamna nimic.
    - fara `source_address='0.0.0.0'`: pe Windows legarea aceea face ca
      conexiunea la googlevideo sa expire. Pe Linux, adica in producție, e
      corecta si rămâne acolo.
    """

    def __init__(self, download_dir, *, socket_timeout=60):
        self.download_dir = download_dir
        self.socket_timeout = socket_timeout
        self.calls = []
        self.timeline = None
        self.inflight = 0
        self.max_inflight = 0
        # Compatibilitate cu scenariile scrise pentru YouTube-ul fals.
        self.search_sec = self.extract_sec = self.download_sec = 0.0
        self.fail_ids = set()
        self.timeout_ids = set()
        self.unavailable_ids = set()
        self.fixture = None

    def _fix(self, opts):
        opts = dict(opts)
        opts.pop('source_address', None)
        opts['js_runtimes'] = {'node': {}}
        opts['socket_timeout'] = max(opts.get('socket_timeout') or 0,
                                     self.socket_timeout)
        return opts

    def install(self):
        from music import ytdlp
        self._saved = (ytdlp.extract, ytdlp.extract_and_prepare_filename)
        real_extract, real_download = self._saved

        # Semnaturile se preiau prin passthrough, nu se re-declara: `extract` are
        # si `want_filename`, iar o copie scrisa de mana l-a pierdut si fiecare
        # descarcare a picat cu TypeError — un eșec al uneltei care arata exact ca
        # un bug al botului.
        async def extract(opts, query, **kwargs):
            stage = kwargs.get('stage') or 'extract'
            return await self._timed(stage, query,
                                     real_extract(self._fix(opts), query, **kwargs))

        async def extract_and_prepare_filename(opts, query, **kwargs):
            stage = kwargs.get('stage') or 'download'
            return await self._timed(stage, query,
                                     real_download(self._fix(opts), query, **kwargs))

        ytdlp.extract = extract
        ytdlp.extract_and_prepare_filename = extract_and_prepare_filename
        self.fixture = audio_fixture(self.download_dir)
        return self

    def restore(self):
        from music import ytdlp
        ytdlp.extract, ytdlp.extract_and_prepare_filename = self._saved

    async def _timed(self, stage, query, awaitable):
        started = time.monotonic()
        self.calls.append((stage, str(query)[:60], started))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            out = await awaitable
        except Exception as e:
            if self.timeline is not None:
                self.timeline.add(f'yt.{stage}',
                                  f'{str(query)[:40]} EȘEC dupa '
                                  f'{time.monotonic()-started:.1f}s: '
                                  f'{type(e).__name__}')
            raise
        finally:
            self.inflight -= 1
        if self.timeline is not None:
            self.timeline.add(f'yt.{stage}',
                              f'{str(query)[:45]} in {time.monotonic()-started:.1f}s')
        return out
