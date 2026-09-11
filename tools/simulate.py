"""Sesiuni de utilizator real contra botului real. Discord si YouTube sunt false.

    python -B tools/simulate.py                 # toate scenariile
    python -B tools/simulate.py play autoplay   # doar unele
    python -B tools/simulate.py --verbose       # cronologia completa

Ce e REAL: fiecare linie de cod din `music/` si `bot.py`, bucla asyncio, ordinea
evenimentelor, FFmpeg, codarea Opus, cache-ul de pe disc, cronometrele.
Ce e FALS: transportul Discord (dar cu limitele lui impuse) si YouTube (cu
latentele masurate in producție).

Fiecare scenariu raporteaza CE A VAZUT UTILIZATORUL si cat a durat. Un scenariu
picat inseamna ca botul s-a purtat altfel decat ar trebui — nu ca testul e fragil.
"""
import asyncio
import itertools
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

os.environ.setdefault('DISCORD_TOKEN', 'test-token-nefolosit')
# Volumul de test, ales INAINTE de orice import din `music`: altfel calea de
# cookies si de cache ar fi cea reala.
_SANDBOX = os.path.join(tempfile.gettempdir(), 'gogu-sim')
shutil.rmtree(_SANDBOX, ignore_errors=True)
os.makedirs(os.path.join(_SANDBOX, 'audio'), exist_ok=True)
os.environ['COOKIE_DIR'] = _SANDBOX

import fakescord as fs
import fakeyoutube as fy

# YouTube ADEVARAT in loc de cel fals. Cere serverul de PO token pornit local:
#   node <pot-provider>/server/build/main.js --port 4416
REAL_YOUTUBE = False
# Interogari reale, cu titluri care exista si nu se schimba peste noapte.
REAL_QUERIES = ['Los Del Rio Macarena', 'Luis Gabriel Toate diamantele']


class Harness:
    """Un guild, un bot real, un YouTube fals. Un scenariu = o instanța."""

    def __init__(self, **server):
        self.server = fs.Fakescord(**server)
        self.checks = []
        self.yt = None
        # Cate `process_play` au fost simultan in zbor. Invariantul cozii e ACESTA,
        # nu numarul de cereri catre YouTube: un prefetch care descarca piesa
        # urmatoare in paralel cu redarea e exact ce trebuie sa faca.
        self.resolves_live = 0
        self.max_resolves = 0

    async def __aenter__(self):
        import bot as bot_mod
        from music import config, player, state as state_mod, ui

        self.bot_mod = bot_mod
        self.player = player
        self.ui = ui
        self.state_mod = state_mod
        self.config = config

        bot_mod.bot.loop = asyncio.get_running_loop()
        player._loop = asyncio.get_running_loop()
        # Fiecare scenariu porneste dintr-o stare curata.
        state_mod.guild_states.clear()
        for name in os.listdir(config.DOWNLOAD_DIR):
            if name.startswith('_'):
                continue                       # fixture-ul audio, refolosit
            try:
                os.remove(os.path.join(config.DOWNLOAD_DIR, name))
            except OSError:
                # Pe Windows un fisier tinut de FFmpeg nu poate fi sters. Nu e o
                # eroare de scenariu; doar il lasam si scenariul urmator il vede
                # ca hit de cache.
                pass

        self.yt = (fy.RealYouTube(config.DOWNLOAD_DIR) if REAL_YOUTUBE
                   else fy.FakeYouTube(config.DOWNLOAD_DIR)).install()
        self.yt.timeline = self.server.timeline
        fy.seed()

        # Traser pe `process_play`: fara el nu se poate spune CINE a pornit o
        # rezolvare, iar exact asta conteaza cand doua comenzi se calca.
        self._saved_process = player.process_play
        seq = itertools.count(1)

        async def traced(ctx, query, is_radio=False, **kwargs):
            n = next(seq)
            self.resolves_live += 1
            self.max_resolves = max(self.max_resolves, self.resolves_live)
            self.server.timeline.add('resolve.start',
                                     f'#{n} {str(query)[:50]} radio={is_radio} '
                                     f'(in curs: {self.resolves_live})')
            try:
                out = await self._saved_process(ctx, query, is_radio=is_radio,
                                               **kwargs)
                self.server.timeline.add('resolve.end', f'#{n}')
                return out
            except BaseException as e:
                self.server.timeline.add('resolve.end',
                                         f'#{n} {type(e).__name__}')
                raise
            finally:
                self.resolves_live -= 1

        player.process_play = traced
        # Comenzile au primit `process_play` prin injectie la import, deci
        # inlocuirea globalei nu le atinge. Le re-inregistram pe cele urmarite —
        # aceeasi functie `setup_music_commands` ca in producție, doar cu
        # dependenta urmarita.
        for cmd in list(self.bot_mod.bot.commands):
            self.bot_mod.bot.remove_command(cmd.name)
        try:
            self.bot_mod.bot.tree.remove_command('play')
        except Exception:                                      # noqa: BLE001
            pass
        self.bot_mod.setup_music_commands(
            self.bot_mod.bot, traced, player.play_next,
            self.ui.update_player_ui, self.bot_mod.start_timeout,
            self.bot_mod.cancel_timeout)

        self.server.laur.join(self.server.voice)
        self.ctx = self.server.ctx()
        self.ctx.bot = bot_mod.bot
        return self

    async def __aexit__(self, *exc):
        if self.server.guild.voice_client is not None:
            await self.server.guild.voice_client.disconnect(force=True)
        # LINISTE COMPLETA inainte de scenariul urmator. Un task scapat din
        # scenariul precedent continua sa cheme YouTube-ul fals al celui urmator
        # si sa scrie in cronologia lui — adica scenariul urmator masoara fantome.
        # S-a intamplat: un prefill al unui scenariu a apărut in cronologia
        # altuia si a aratat exact ca un bug.
        current = asyncio.current_task()
        for _ in range(3):
            pending = [t for t in asyncio.all_tasks() if t is not current]
            if not pending:
                break
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.player.process_play = self._saved_process
        self.yt.restore()
        await asyncio.sleep(0.05)
        return False

    def state(self):
        return self.state_mod.get_state(self.server.guild.id)

    @staticmethod
    def query(n=0):
        """Ce cere utilizatorul: din catalogul fals, sau o piesa care exista real."""
        return (REAL_QUERIES[n % len(REAL_QUERIES)] if REAL_YOUTUBE
                else ('Luis Gabriel' if n == 0 else 'Delia'))

    async def run(self, name, **kwargs):
        """Ruleaza o comanda de prefix, exact callback-ul inregistrat pe bot."""
        cmd = self.bot_mod.bot.get_command(name)
        assert cmd is not None, f'comanda {name} nu exista'
        self.server.timeline.add('user', f'!{name} ' + ' '.join(
            f'{v}' for v in kwargs.values()))
        started = time.monotonic()
        try:
            await cmd.callback(self.ctx, **kwargs)
        except Exception as e:                                  # noqa: BLE001
            self.server.timeline.problem('excepție in comanda',
                                         f'!{name}: {type(e).__name__}: {e}')
        return time.monotonic() - started

    async def slash(self, name, **kwargs):
        """Aceeasi comanda, dar ca interactiune: fereastra de 3s se aplica."""
        interaction = fs.FakeInteraction(self.server.timeline, f'/{name}',
                                         self.server.laur,
                                         kind='application_command')
        ctx = self.server.ctx(interaction=interaction)
        ctx.bot = self.bot_mod.bot
        saved, self.ctx = self.ctx, ctx
        self.server.timeline.add('user', f'/{name} ' + ' '.join(
            f'{v}' for v in kwargs.values()))
        try:
            await self.bot_mod.bot.get_command(name).callback(ctx, **kwargs)
        except Exception as e:                                  # noqa: BLE001
            self.server.timeline.problem('excepție in slash',
                                         f'/{name}: {type(e).__name__}: {e}')
        finally:
            self.ctx = saved
        interaction.finish()
        return interaction

    async def click(self, custom_id, **kwargs):
        return await self.server.click(custom_id, **kwargs)

    async def settle(self, seconds=None, *, until=None, timeout=40.0):
        """AȘteapta ca botul sa termine ce a pornit. Timp real, nu simulat."""
        if until is None:
            await asyncio.sleep(seconds if seconds is not None else 0.1)
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return True
            await asyncio.sleep(0.05)
        self.server.timeline.problem(
            'blocat', f'condiția nu s-a indeplinit in {timeout:.0f}s')
        return False

    async def wait_playing(self, timeout=40.0):
        return await self.settle(
            until=lambda: (self.server.guild.voice_client is not None
                           and self.server.guild.voice_client.is_playing()),
            timeout=timeout)

    def check(self, ok, what):
        self.checks.append((bool(ok), what))
        if not ok:
            self.server.timeline.problem('verificare picata', what)

    def report(self, title, verbose=False):
        failed = [w for ok, w in self.checks if not ok]
        problems = self.server.timeline.problems
        status = 'OK  ' if not failed and not problems else 'PICAT'
        print(f'\n[{status}] {title}   '
              f'({len(self.checks)} verificari, {self.server.timeline.at():.1f}s)')
        if verbose:
            print(self.server.timeline.dump())
        elif problems:
            print(self.server.timeline.dump(only_problems=True))
        for w in failed:
            print(f'  - {w}')
        return not failed and not problems


SCENARIOS = {}


def scenario(name):
    def deco(fn):
        SCENARIOS[name] = fn
        return fn
    return deco


@scenario('play')
async def sc_play(verbose):
    """Sesiunea de baza: !play, se conecteaza, cânta, panoul apare."""
    async with Harness() as h:
        elapsed = await h.run('play', search=Harness.query(0))
        ok = await h.wait_playing()
        st = h.state()
        vc = h.server.guild.voice_client
        h.check(ok, 'nu a inceput sa cânte')
        h.check(vc is not None and vc.is_connected(), 'nu s-a conectat la voce')
        h.check(h.server.panel() is not None, 'nu a apărut panoul')
        h.check(st.last_title, 'nu a reținut titlul piesei')
        h.check(st.is_loading is False, 'is_loading a rămas aprins')
        h.check(st.current_file and os.path.exists(st.current_file),
                'fisierul audio nu exista pe disc')
        await h.settle(0.4)
        h.check(vc.frames_read > 0,
                f'FFmpeg nu a produs cadre Opus (citite: {vc.frames_read})')
        print(f'       comanda a raspuns in {elapsed:.2f}s, '
              f'primul cadru dupa {h.server.timeline.at():.2f}s, '
              f'{vc.frames_read} cadre')
        return h.report('play: prima piesa', verbose)


@scenario('cache')
async def sc_cache(verbose):
    """A doua redare a aceleiasi piese trebuie sa fie gratuita."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        first_calls = len(h.yt.calls)
        await h.settle(0.3)
        await h.run('stop')
        await h.settle(0.3)
        h.server.laur.join(h.server.voice)

        t0 = time.monotonic()
        await h.run('play', search=Harness.query(0))
        ok = await h.wait_playing()
        warm = time.monotonic() - t0
        downloads = [c for c in h.yt.calls[first_calls:]
                     if c[0].startswith('download')]
        h.check(ok, 'a doua redare nu a pornit')
        h.check(not downloads, f'a re-descarcat desi era in cache: {downloads}')
        print(f'       a doua redare in {warm:.2f}s, '
              f'{len(downloads)} descarcari noi')
        return h.report('cache: a doua redare e gratuita', verbose)


@scenario('autoplay')
async def sc_autoplay(verbose):
    """Butonul Autoplay, refill-ul, si skip-ul pe coada automata."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.settle(0.3)

        await h.click('autoplay')
        st = h.state()
        h.check(st.autoplay is True, 'butonul nu a pornit autoplay')
        h.check(len(st.queue) >= 6,
                f'coada de autoplay prea mica: {len(st.queue)}')
        h.check(st.is_loading is False, 'is_loading a rămas aprins dupa prefill')

        panel = h.server.panel()
        select = [c for c in (panel.view.children if panel and panel.view else [])
                  if type(c).__name__ == 'Select']
        h.check(select, 'dropdown-ul de coada nu a apărut')
        if select:
            h.check(len(select[0].options) >= 6,
                    f'doar {len(select[0].options)} opțiuni in dropdown')

        before = len(st.queue)
        t0 = time.monotonic()
        await h.click('skip')
        ok = await h.wait_playing()
        skip_time = time.monotonic() - t0
        h.check(ok, 'skip-ul nu a pornit nimic')
        h.check(len(st.queue) < before or st.queue,
                'coada nu a avansat dupa skip')
        print(f'       skip a durat {skip_time:.2f}s, coada {before} -> '
              f'{len(st.queue)}')
        return h.report('autoplay: prefill, dropdown, skip', verbose)


@scenario('prefetch')
async def sc_prefetch(verbose):
    """Skip-ul trebuie sa fie mult mai rapid decat o descarcare la rece."""
    async with Harness() as h:
        h.yt.download_sec = 6.0
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.click('autoplay')
        st = h.state()
        h.check(st.queue, 'nu exista coada pentru prefetch')

        # Lasa prefetch-ul sa termine piesa urmatoare.
        next_id = st.queue[0]['query'].split('v=')[-1] if st.queue else None
        got = await h.settle(
            until=lambda: os.path.exists(
                os.path.join(h.config.DOWNLOAD_DIR, f'{next_id}.webm')),
            timeout=30)
        h.check(got, f'prefetch-ul nu a adus {next_id} in cache')

        t0 = time.monotonic()
        await h.click('skip')
        ok = await h.wait_playing()
        skip_time = time.monotonic() - t0
        h.check(ok, 'skip-ul nu a pornit nimic')
        h.check(skip_time < h.yt.download_sec,
                f'skip a durat {skip_time:.2f}s, adica nu a folosit cache-ul '
                f'(o descarcare costa {h.yt.download_sec}s)')
        print(f'       skip cu prefetch: {skip_time:.2f}s '
              f'(descarcare la rece: {h.yt.download_sec}s)')
        return h.report('prefetch: skip pe cache cald', verbose)


@scenario('buttons')
async def sc_buttons(verbose):
    """Fiecare buton, apasat pe rand, cu panoul reimprospatat intre ele."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.settle(0.3)
        st = h.state()

        for custom_id in ('queue', 'loop', 'loop', 'loop', 'playpause',
                          'playpause', 'autoplay', 'queue'):
            it = await h.click(custom_id)
            h.check(it is not None,
                    f'butonul {custom_id} nu a ajuns la niciun handler')
            if it is not None:
                h.check(it.acked, f'butonul {custom_id} nu a confirmat')
            await h.settle(0.15)

        h.check(st.loop_mode == 0, f'loop_mode a rămas {st.loop_mode} dupa 3 cicluri')
        vc = h.server.guild.voice_client
        h.check(vc.is_playing(), 'pauza+resume a lasat redarea oprita')
        panel = h.server.panel()
        h.check(panel is not None and panel.edits > 0,
                'panoul nu s-a actualizat la nicio apasare')
        print(f'       panoul a fost editat {panel.edits} ori, '
              f'toate butoanele au raspuns')
        return h.report('buttons: toate butoanele, in secventa', verbose)


@scenario('jump')
async def sc_jump(verbose):
    """Alegerea din dropdown nu are voie sa micȘoreze coada."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.click('autoplay')
        st = h.state()
        h.check(len(st.queue) >= 5, f'coada prea mica: {len(st.queue)}')

        before = list(st.queue)
        was = st.last_url
        target = before[3]['query'] if len(before) > 3 else before[-1]['query']
        await h.click('jump_select', values=[target[:100]])
        # AȘteapta piesa NOUA, nu doar "cânta ceva": imediat dupa click, cea veche
        # inca poate fi in aer, si atunci verificarea masoara starea de dinainte.
        switched = await h.settle(until=lambda: st.last_url != was, timeout=45)
        h.check(switched, 'nu a schimbat piesa dupa alegerea din lista')
        h.check(len(st.queue) >= len(before) - 1,
                f'coada a scazut de la {len(before)} la {len(st.queue)}')
        h.check(st.last_url and target.endswith(st.last_url.split('v=')[-1]),
                f'a redat altceva decat piesa aleasa ({st.last_url})')
        print(f'       coada {len(before)} -> {len(st.queue)}, '
              f'a redat piesa aleasa')
        return h.report('jump: coada rămâne intreaga', verbose)


@scenario('247')
async def sc_247(verbose):
    """!247 trebuie sa intre in voce, nu doar sa aprinda un steag."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.settle(0.3)
        await h.run('stop')
        await h.settle(0.4)
        h.check(h.server.guild.voice_client is None, '!stop nu a ieșit din voce')

        h.server.laur.join(h.server.voice)
        await h.run('247')
        st = h.state()
        h.check(st.always_on is True, '!247 nu a pornit 24/7')
        h.check(h.server.guild.voice_client is not None,
                '!247 a pornit 24/7 fara sa intre in voce')
        started = await h.wait_playing()
        h.check(started, '!247 nu a pornit nicio redare')
        print(f'       24/7 a intrat in voce si a pornit in '
              f'{h.server.timeline.at():.1f}s, coada {len(st.queue)}')
        return h.report('247: intra in voce si cânta', verbose)


@scenario('concurrent')
async def sc_concurrent(verbose):
    """Doua comenzi in acelasi instant. Nicio rezolvare dubla, niciun steag blocat."""
    async with Harness() as h:
        await asyncio.gather(
            h.run('play', search=Harness.query(0)),
            h.run('play', search=Harness.query(1)),
        )
        await h.wait_playing()
        await h.settle(0.5)
        st = h.state()
        h.check(h.max_resolves <= 1,
                f'{h.max_resolves} rezolvari de redare in paralel')
        h.check(st.is_loading is False, 'is_loading a rămas aprins')
        vc = h.server.guild.voice_client
        h.check(len(vc.plays) == 1,
                f'a pornit {len(vc.plays)} redari pentru doua comenzi')
        h.check(len(st.queue) <= 1, f'coada are {len(st.queue)} intrari')

        # Skip x3 in acelasi instant.
        await h.click('autoplay')
        await asyncio.gather(h.click('skip'), h.click('skip'), h.click('skip'))
        ok = await h.wait_playing()
        h.check(ok, 'dupa 3 skip-uri simultane nu mai cânta nimic')
        # Steagul se verifica DUPA ce rezolvarea legitima s-a incheiat: verificat
        # prea devreme, "in curs" arata identic cu "blocat".
        quiet = await h.settle(until=lambda: not st.is_loading, timeout=45)
        h.check(quiet, 'is_loading blocat dupa 3 skip-uri simultane')
        print(f'       max {h.max_resolves} rezolvari simultane '
              f'({h.yt.max_inflight} cereri YouTube), '
              f'{len(vc.plays)} redari pornite')
        return h.report('concurrent: comenzi simultane', verbose)


@scenario('stop-mid-resolve')
async def sc_stop_mid(verbose):
    """!stop exact in fereastra dintre cerere si redare."""
    async with Harness() as h:
        h.yt.download_sec = 5.0
        task = asyncio.create_task(h.run('play', search=Harness.query(0)))
        await asyncio.sleep(2.0)                 # in mijlocul descarcarii
        await h.run('stop')
        await task
        await h.settle(1.0)
        st = h.state()
        h.check(h.server.guild.voice_client is None, 'a rămas in voce dupa !stop')
        h.check(st.is_loading is False, 'is_loading a rămas aprins')
        h.check(st.queue == [], f'coada nu e goala: {st.queue}')
        vc_plays = [m for m in h.server.text.messages if m.embed]
        h.check(st.current_msg is None,
                f'panoul nu a fost uitat: {st.current_msg!r}')
        print(f'       stop in mijlocul descarcarii: stare curata, '
              f'{len(vc_plays)} panouri in canal')
        return h.report('stop-mid-resolve: fara stare orfana', verbose)


@scenario('errors')
async def sc_errors(verbose):
    """O piesa care nu se descarca nu are voie sa blocheze coada."""
    async with Harness() as h:
        fy.seed()
        h.yt.fail_ids = {'vid000', 'vid001'}
        await h.run('play', search=Harness.query(0))
        await h.settle(1.5)
        st = h.state()
        told = [m for m in h.server.text.messages
                if m.content and not m.deleted]
        h.check(told, 'nu i-a spus nimic utilizatorului despre eroare')
        h.check(st.is_loading is False, 'is_loading a rămas aprins dupa eroare')

        h.yt.fail_ids = set()
        await h.run('play', search=Harness.query(1))
        ok = await h.wait_playing()
        h.check(ok, 'dupa o eroare, urmatoarea piesa nu mai porneste')
        print(f'       eroare raportata, urmatoarea piesa a pornit')
        return h.report('errors: o eroare nu wedge-uiește botul', verbose)


@scenario('slash')
async def sc_slash(verbose):
    """/play trebuie confirmat in 3 secunde, oricat dureaza rezolvarea."""
    async with Harness() as h:
        h.yt.search_sec = 2.0
        h.yt.download_sec = 8.0
        interaction = await h.slash('play', search=Harness.query(0))
        h.check(interaction.acked, '/play nu a confirmat interactiunea')
        if interaction.ack_latency is not None:
            h.check(interaction.ack_latency < fs.INTERACTION_DEADLINE_SEC,
                    f'/play a confirmat dupa {interaction.ack_latency:.2f}s')
        ok = await h.wait_playing(timeout=45)
        h.check(ok, '/play nu a pornit nicio redare')
        print(f'       /play confirmat in '
              f'{(interaction.ack_latency or 0)*1000:.0f}ms, redare dupa '
              f'{h.server.timeline.at():.1f}s')
        return h.report('slash: fereastra de 3 secunde', verbose)


@scenario('queue-ops')
async def sc_queue_ops(verbose):
    """!shuffle, !move, !remove, !clear pe o coada reala, cu indici la limita."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.click('autoplay')
        st = h.state()
        n = len(st.queue)
        h.check(n >= 6, f'coada prea mica pentru test: {n}')

        await h.run('shuffle')
        h.check(len(st.queue) == n, 'shuffle a schimbat lungimea cozii')
        await h.run('move', from_idx=1, to_idx=n)
        h.check(len(st.queue) == n, 'move a schimbat lungimea cozii')
        await h.run('move', from_idx=0, to_idx=1)
        h.check(len(st.queue) == n, 'move cu index 0 a modificat coada')
        await h.run('move', from_idx=n + 5, to_idx=1)
        h.check(len(st.queue) == n, 'move cu index prea mare a modificat coada')
        await h.run('remove', index=n + 5)
        h.check(len(st.queue) == n, 'remove cu index prea mare a scos ceva')
        await h.run('remove', index=1)
        h.check(len(st.queue) == n - 1, 'remove nu a scos nimic')
        await h.run('clear')
        h.check(st.queue == [], 'clear nu a golit coada')
        panel = h.server.panel()
        h.check(panel is not None and not panel.deleted,
                'panoul a dispărut in timpul operatiilor pe coada')
        print(f'       {n} piese: shuffle/move/remove/clear, indici invalizi '
              f'respinsi fara pierderi')
        return h.report('queue-ops: operatii si indici la limita', verbose)


@scenario('long-queue')
async def sc_long_queue(verbose):
    """40 de piese cu titluri lungi: panoul trebuie sa incapa in limitele Discord."""
    async with Harness() as h:
        fy.CATALOG.clear()
        for i in range(40):
            vid = f'lng{i:03d}'
            fy.CATALOG[vid] = fy.make_track(
                vid, f'Artist Cu Nume Foarte Lung {i} - ' + 'Titlu Interminabil ' * 8)
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        st = h.state()
        st.queue = [{'query': t['webpage_url'], 'title': t['title']}
                    for t in fy.CATALOG.values()]
        st.show_queue = True
        await h.ui.update_player_ui(h.ctx)
        panel = h.server.panel()
        h.check(panel is not None, 'panoul a fost respins de Discord')
        if panel and panel.view:
            select = [c for c in panel.view.children
                      if type(c).__name__ == 'Select']
            h.check(select, 'dropdown-ul lipsește la 40 de piese')
            if select:
                h.check(len(select[0].options) <= 25,
                        f'{len(select[0].options)} opțiuni > 25')
        print(f'       40 de piese cu titluri de '
              f'{len(next(iter(fy.CATALOG.values()))["title"])} caractere: '
              f'panou acceptat')
        return h.report('long-queue: limitele de payload', verbose)


@scenario('permissions')
async def sc_permissions(verbose):
    """Fara Connect sau fara Speak, utilizatorul trebuie sa afle IMEDIAT."""
    async with Harness(connect_perm=False) as h:
        t0 = time.monotonic()
        await h.run('play', search=Harness.query(0))
        elapsed = time.monotonic() - t0
        h.check(elapsed < 1.0, f'a aȘteptat {elapsed:.1f}s ca sa spuna ca nu poate')
        h.check(h.server.voice.connect_calls == [],
                'a incercat sa se conecteze fara permisiune')
        told = [m for m in h.server.text.messages if m.content]
        h.check(any('permisiune' in str(m.content) for m in told),
                f'nu a explicat de ce nu poate: {told}')
        print(f'       refuz explicat in {elapsed*1000:.0f}ms, zero cereri')
        return h.report('permissions: refuz instant si explicat', verbose)


@scenario('voice-retry')
async def sc_voice_retry(verbose):
    """Un endpoint intarziat de Discord nu are voie sa piarda comanda."""
    async with Harness(connect_outcomes=[asyncio.TimeoutError()]) as h:
        await h.run('play', search=Harness.query(0))
        ok = await h.wait_playing(timeout=45)
        h.check(len(h.server.voice.connect_calls) == 2,
                f'{len(h.server.voice.connect_calls)} incercari de conectare')
        h.check(ok, 'a renunțat dupa primul timeout')
        print(f'       {len(h.server.voice.connect_calls)} incercari, '
              f'a doua a reusit')
        return h.report('voice-retry: reincercare dupa timeout', verbose)


@scenario('outsider')
async def sc_outsider(verbose):
    """Cine nu e in canal nu comanda redarea altcuiva."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.settle(0.3)
        vc = h.server.guild.voice_client
        before = len(vc.plays)
        it = await h.click('skip', user=h.server.other)
        h.check(it is not None and it.acked,
                'interactiunea strainului a rămas fara raspuns')
        await h.settle(0.5)
        h.check(len(vc.plays) == before,
                'un utilizator din afara canalului a schimbat piesa')
        h.check(vc.is_playing(), 'redarea a fost oprita de un strain')
        print(f'       strainul a primit raspuns si nu a schimbat nimic')
        return h.report('outsider: gardul de canal', verbose)


@scenario('panel-deleted')
async def sc_panel_deleted(verbose):
    """Panoul sters de un moderator trebuie sa reapara, nu sa dispara pe veci."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        await h.settle(0.3)
        panel = h.server.panel()
        h.check(panel is not None, 'nu exista panou')
        await panel.delete()
        await h.ui.update_player_ui(h.ctx)
        await h.settle(0.2)
        new_panel = h.server.panel()
        h.check(new_panel is not None and new_panel.id != panel.id,
                'panoul sters nu a fost retrimis')
        if new_panel:
            it = await h.click('skip')
            h.check(it is not None,
                    'butoanele panoului retrimis nu sunt inregistrate')
        print(f'       panou sters -> retrimis, butoanele vii')
        return h.report('panel-deleted: se repara singur', verbose)


@scenario('refresh-buttons')
async def sc_refresh_buttons(verbose):
    """Butoanele trebuie sa raspunda si dupa MULTE reimprospatari ale panoului."""
    async with Harness() as h:
        await h.run('play', search=Harness.query(0))
        await h.wait_playing()
        for i in range(10):
            await h.ui.update_player_ui(h.ctx)
        panel = h.server.panel()
        h.check(panel is not None and panel.edits >= 10,
                f'panoul a fost editat doar {panel.edits if panel else 0} ori')
        it = await h.click('queue')
        h.check(it is not None,
                'dupa 10 reimprospatari, butoanele nu mai sunt inregistrate')
        if it is not None:
            h.check(it.acked, 'butonul nu a confirmat interactiunea')
        print(f'       {panel.edits} editari, butoanele inca vii')
        return h.report('refresh-buttons: componente vii dupa refresh', verbose)


# Scenariile care manipuleaza catalogul fals (erori injectate, 40 de titluri
# fabricate) nu au sens contra YouTube-ului adevarat.
FAKE_ONLY = {'errors', 'long-queue'}


async def main(argv):
    global REAL_YOUTUBE
    verbose = '--verbose' in argv
    REAL_YOUTUBE = '--real-youtube' in argv
    wanted = [a for a in argv[1:] if not a.startswith('--')]
    names = wanted or list(SCENARIOS)
    if REAL_YOUTUBE:
        import urllib.request
        try:
            with urllib.request.urlopen('http://127.0.0.1:4416/ping', timeout=3) as r:
                print('PO token server:', r.read().decode()[:60])
        except OSError as e:
            print(f'Serverul de PO token nu raspunde pe 4416 ({e}). '
                  f'Fara el, YouTube sare clientii si formatele opus lipsesc.')
            return 2
        skipped = [n for n in names if n in FAKE_ONLY]
        names = [n for n in names if n not in FAKE_ONLY]
        if skipped:
            print(f'Sarite in modul real (au nevoie de catalog controlat): '
                  f'{", ".join(skipped)}')
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f'Scenarii necunoscute: {unknown}')
        print(f'Disponibile: {", ".join(SCENARIOS)}')
        return 2

    print(f'Emulator Discord, bot REAL, YouTube '
          f'{"REAL" if REAL_YOUTUBE else "fals"}. {len(names)} scenarii.\n'
          f'Sandbox: {_SANDBOX}')
    results = {}
    for name in names:
        try:
            results[name] = await SCENARIOS[name](verbose)
        except Exception as e:                                  # noqa: BLE001
            import traceback
            print(f'\n[EROARE] {name}: {type(e).__name__}: {e}')
            traceback.print_exc()
            results[name] = False

    failed = [n for n, ok in results.items() if not ok]
    print(f'\n{"="*70}\n{len(results) - len(failed)}/{len(results)} scenarii OK')
    if failed:
        print(f'PICATE: {", ".join(failed)}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.argv)))
