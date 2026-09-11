"""Test end-to-end pe Discord REAL, cu voce reala si YouTube real.

Ce nu poate acoperi niciun emulator: gateway-ul de voce, criptarea, cadenta reala
de 20ms, validatorul lui Discord pentru embed-uri si componente, si formatele pe
care YouTube le da CHIAR ACUM. Tot restul (butoane, curse, edge-case-uri) e in
tools/simulate.py, care ruleaza fara retea si intra in CI.

Rulare (tokenul vine din Railway, nu se scrie nicaieri pe disc):

    railway run --service new_dsc -- python -B tools/livecheck.py GUILD VOICE [TEXT]

Doua garantii deliberate, ca o rulare de test sa nu poata strica producția:

1. Cookie-urile de YouTube sunt ARUNCATE inainte de orice import. Aceeasi sesiune
   Google folosita in paralel de pe alt IP e exact felul in care Google invalideaza
   un cont — adica felul in care botul din producție ar rămâne fara cookie-uri.
   Modul invitat merge oricum; `--with-cookies` exista doar pentru cazul in care
   chiar vrei sa verifici calea autentificata.
2. Instanța locala e SURDA la mesaje reale (`on_message` inlocuit) si nu
   sincronizeaza niciodata arborele de comenzi slash. Deci daca cineva scrie in
   Discord in timpul rularii, raspunde doar botul din producție, o singura data.
"""
import argparse
import asyncio
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_args():
    p = argparse.ArgumentParser(description='Test live pe Discord real.')
    p.add_argument('guild', type=int)
    p.add_argument('voice', type=int, help='ID canal de voce')
    p.add_argument('text', type=int, nargs='?', default=0,
                   help='ID canal text (implicit: primul in care botul poate scrie)')
    p.add_argument('--only', default='',
                   help='doar scenariile astea, separate prin virgula')
    p.add_argument('--with-cookies', action='store_true',
                   help='NU arunca YT_COOKIES_CONTENT (risc: invalideaza sesiunea din producție)')
    p.add_argument('--keep', action='store_true',
                   help='nu sterge mesajele de test la final')
    p.add_argument('--ipv6', action='store_true',
                   help='nu forta IPv4 (vezi comentariul de la _allow_ipv6)')
    return p.parse_args()


ARGS = _parse_args()

if not ARGS.with_cookies:
    os.environ.pop('YT_COOKIES_CONTENT', None)
    os.environ.pop('YT_COOKIES_B64', None)
    # Fara asta, config ar putea gasi jar-ul lasat pe disc de o rulare anterioara.
    os.environ['COOKIE_DIR'] = os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), '.livecheck')
    os.makedirs(os.environ['COOKIE_DIR'], exist_ok=True)

import discord  # noqa: E402  (dupa curatarea mediului, deliberat)

import bot as bot_mod  # noqa: E402
from music import config, ui  # noqa: E402
from music.state import get_state  # noqa: E402

log = logging.getLogger('livecheck')


def _allow_ipv6():
    """Scoate `source_address='0.0.0.0'`, adica forțarea IPv4, DOAR pentru test.

    In producție forțarea rămâne, si nu e o scapare: YouTube trateaza intervalele
    IPv6 de datacenter mult mai aspru ("Sign in to confirm you're not a bot"),
    deci un server are nevoie de IPv4.

    Pe o retea casnica insa poate fi exact invers. Masurat pe maȘina de dezvoltare:
    ruta IPv4 spre nodul GGC `sn-8vq54voxgv` (136.255.252.110:443) e blackholed —
    timeout la 10s, cu si fara bind — in timp ce acelasi host pe IPv6 accepta
    conexiunea in 0.03s. Extracția merge (aceea vorbeste cu youtube.com), doar
    transferul media cade, deci simptomul e "Eroare de retea" dupa 70 de secunde
    de reincercari. Fara acest comutator, niciun scenariu cu audio nu poate fi
    testat de aici.
    """
    removed = 0
    for opts in (config.YDL_OPTS_SEARCH, config.YDL_OPTS_DOWNLOAD):
        removed += opts.pop('source_address', None) is not None
    print(f'[live] IPv4 forțat: dezactivat pentru test ({removed} seturi de opțiuni)',
          flush=True)

# Piese de test: scurte, publice, si sigur nu live-uri.
TRACK_A = 'https://www.youtube.com/watch?v=ZGWcvJELOt4'      # Vama - Perfect
TRACK_B = 'https://www.youtube.com/watch?v=yKNxeF4KMsY'      # Coldplay - Yellow
TRACK_SEARCH = 'gheboasa gasca zurli'
TRACK_LIVE = 'https://www.youtube.com/watch?v=jfKfPfyJRdk'   # lofi girl, live permanent
TRACK_GONE = 'https://www.youtube.com/watch?v=aaaaaaaaaaa'   # id inexistent
TRACK_LONG = 'https://www.youtube.com/watch?v=5qap5aO4i9A'   # YouTube: 'we are processing'


class Result:
    def __init__(self, name):
        self.name = name
        self.notes: list[str] = []
        self.failures: list[str] = []
        self.seconds = 0.0

    def check(self, ok, message):
        (self.notes if ok else self.failures).append(
            ('OK  ' if ok else 'FAIL') + ' ' + message)
        return ok

    def note(self, message):
        self.notes.append('    ' + message)


class Live:
    """Singurul loc care stie sa vorbeasca cu Discord in numele unui om."""

    def __init__(self, guild, voice_channel, text_channel, member):
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.member = member
        self.sent: list[discord.Message] = []       # ce a postat botul
        self.mine: list[discord.Message] = []       # ce am postat eu, de sters
        self.mine_ids: set[int] = set()             # ... si de ignorat la citire

    # --- comenzi ------------------------------------------------------------

    async def run_command(self, text, *, author=None):
        """Executa o comanda EXACT pe drumul din producție.

        Mesajul e real (trimis prin API), deci are id real si poate fi sters sau
        editat; ii schimbam doar autorul, fiindca un bot nu poate scrie ca om.
        `bot.invoke` trece prin checks, before_invoke si on_command_error — adica
        acelasi lanț ca un `!play` scris de mana.
        """
        msg = await self.text_channel.send(text)
        self.mine.append(msg)
        self.mine_ids.add(msg.id)
        msg.author = author or self.member
        ctx = await bot_mod.bot.get_context(msg)
        if ctx.command is None:
            raise AssertionError(f'{text!r}: nicio comanda recunoscuta')
        await bot_mod.bot.invoke(ctx)
        return ctx

    # --- observare ----------------------------------------------------------

    @property
    def state(self):
        return get_state(self.guild.id)

    @property
    def vc(self):
        return self.guild.voice_client

    def since(self):
        """Marcheaza punctul din care contorizam mesajele botului."""
        return len(self.sent)

    def said(self, mark):
        # Filtrat AICI, nu la inregistrare: evenimentul de gateway poate ajunge
        # inainte ca `send()` sa se intoarca, deci la momentul inregistrarii id-ul
        # meu poate sa nu fie inca notat.
        return [m for m in self.sent[mark:] if m.id not in self.mine_ids]

    def text_of(self, mark):
        out = []
        for m in self.said(mark):
            out.append(m.content or '')
            for e in m.embeds:
                out.append(' '.join(filter(None, [
                    e.title or '', e.description or '',
                    ' '.join(f.name + ' ' + f.value for f in e.fields),
                    (e.footer.text or '') if e.footer else '',
                    (e.author.name or '') if e.author else ''])))
        return '\n'.join(out)

    async def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def cleanup_session(self):
        """Sesiune curata intre scenarii, fara sa treaca prin !stop."""
        state = self.state
        bot_mod.cancel_timeout(self.guild)
        state.play_generation += 1
        if self.vc:
            try:
                if self.vc.is_playing() or self.vc.is_paused():
                    self.vc.stop()
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        state.queue.clear()
        state.history.clear()
        state.autoplay = False
        state.autoplay_user_off = False
        state.always_on = False
        state.loop_mode = 0
        state.is_loading = False
        state.current_file = None
        state.last_title = ''
        state.breaker_until = 0.0
        state.idle_quiet_until = 0.0
        state._consecutive_errors = 0
        state._consecutive_rejects = 0
        await ui.forget_panel(state)
        await asyncio.sleep(1.0)


# --- scenarii -----------------------------------------------------------------

SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
async def help_lists_every_command(live, r):
    """!help trebuie sa arate TOATE comenzile: e singurul loc din care cineva afla
    ce exista. Si trebuie sa treaca validatorul de embed al lui Discord."""
    mark = live.since()
    await live.run_command('!help')
    got = await live.wait(lambda: len(live.said(mark)) >= 1, 10)
    r.check(got, 'raspunde la !help')
    if not got:
        return
    text = live.text_of(mark).lower()
    registered = {c.name for c in bot_mod.bot.commands}
    registered |= {a for c in bot_mod.bot.commands for a in c.aliases}
    # `!nume`, nu doar `nume`: altfel aliasul `h` "apare" in orice cuvant cu h, iar
    # `comenzi` in titlul embed-ului — adica verificarea trecea din intamplare.
    missing = sorted(n for n in registered if f'!{n.lower()}' not in text)
    r.check(not missing, f'toate comenzile apar in !help (lipsesc: {missing})')


@scenario
async def play_a_search_connects_and_plays(live, r):
    """Drumul principal: text -> cautare -> descarcare -> voce -> audio."""
    t0 = time.perf_counter()
    mark = live.since()
    await live.run_command(f'!play {TRACK_SEARCH}')
    connected = await live.wait(lambda: live.vc and live.vc.is_connected(), 30)
    r.check(connected, 'intra in canalul de voce')
    playing = await live.wait(lambda: live.vc and live.vc.is_playing(), 90)
    r.check(playing, 'porneste audio')
    r.note(f'timp pana la audio: {time.perf_counter() - t0:.1f}s')
    if not playing:
        r.note('ce a spus botul: ' + live.text_of(mark)[:400])
        return
    r.check(live.vc.channel.id == live.voice_channel.id,
            'e in canalul din care s-a cerut')
    r.check(bool(live.state.last_title),
            f'titlu pus in stare: {live.state.last_title!r}')
    src = getattr(live.vc, 'source', None)
    r.note(f'sursa: {type(src).__name__}')
    r.check(isinstance(src, discord.FFmpegOpusAudio),
            'sursa e opus (nu PCM re-encodat de discord.py)')
    # Audio continuu: dupa 6 secunde trebuie sa cânte inca.
    await asyncio.sleep(6)
    r.check(live.vc.is_playing(), 'inca cânta dupa 6 secunde')
    r.check(live.state.current_msg is not None, 'panoul a fost postat')
    if live.state.current_msg is not None:
        m = live.state.current_msg
        r.check(bool(m.components),
                f'panoul are butoane ({len(m.components)} randuri)')


@scenario
async def a_second_play_queues_without_interrupting(live, r):
    """A doua cerere trebuie sa intre in coada, nu sa taie piesa care cânta."""
    if not (live.vc and live.vc.is_playing()):
        r.note('sarit: nu cânta nimic')
        return
    before = live.state.last_title
    depth = len(live.state.queue)
    mark = live.since()
    await live.run_command(f'!play {TRACK_A}')
    grew = await live.wait(lambda: len(live.state.queue) > depth, 45)
    r.check(grew, f'a intrat in coada ({depth} -> {len(live.state.queue)})')
    r.check(live.state.last_title == before, 'nu a intrerupt piesa curenta')
    r.check(live.vc.is_playing(), 'inca cânta')
    said = live.text_of(mark)
    r.check(bool(said.strip()), 'a confirmat adaugarea in coada')
    r.note('confirmare: ' + said.replace('\n', ' ')[:160])


@scenario
async def skip_moves_to_the_next_track(live, r):
    if not (live.vc and live.vc.is_playing()):
        r.note('sarit: nu cânta nimic')
        return
    if not live.state.queue:
        await live.run_command(f'!play {TRACK_A}')
        await live.wait(lambda: len(live.state.queue) >= 1, 45)
    before = live.state.last_title
    t0 = time.perf_counter()
    await live.run_command('!skip')
    changed = await live.wait(
        lambda: live.state.last_title and live.state.last_title != before, 60)
    r.check(changed,
            f'a trecut la urmatoarea ({before!r} -> {live.state.last_title!r})')
    r.note(f'skip a durat {time.perf_counter() - t0:.1f}s')
    r.check(bool(live.vc and live.vc.is_playing()), 'cânta piesa noua')


@scenario
async def seek_works_and_refuses_nonsense(live, r):
    if not (live.vc and live.vc.is_playing()):
        r.note('sarit: nu cânta nimic')
        return
    mark = live.since()
    await live.run_command('!seek 0:30')
    await asyncio.sleep(3)
    r.check(bool(live.vc and live.vc.is_playing()), 'cânta dupa seek')
    r.note('raspuns: ' + live.text_of(mark).replace('\n', ' ')[:120])
    mark = live.since()
    await live.run_command('!seek -10')
    said = live.text_of(mark).lower()
    r.check('negativ' in said or 'nu' in said, f'refuza timp negativ: {said[:120]!r}')
    r.check(bool(live.vc and live.vc.is_playing()),
            'nu a oprit redarea din cauza refuzului')


@scenario
async def queue_editing_commands_agree_with_the_queue(live, r):
    state = live.state
    while len(state.queue) < 3:
        depth = len(state.queue)
        await live.run_command(f'!play {TRACK_B if depth % 2 else TRACK_A}')
        if not await live.wait(lambda: len(state.queue) > depth, 45):
            break
    if len(state.queue) < 2:
        r.note(f'sarit: coada are doar {len(state.queue)} elemente')
        return
    depth = len(state.queue)
    first = state.queue[0]
    await live.run_command('!move 1 2')
    r.check(state.queue[1] is first, 'move 1 2 a mutat chiar primul element')
    r.check(len(state.queue) == depth, 'move nu a pierdut elemente')
    mark = live.since()
    await live.run_command('!remove 1')
    r.check(len(state.queue) == depth - 1,
            f'remove a scos exact unul ({depth} -> {len(state.queue)})')
    r.note('raspuns remove: ' + live.text_of(mark).replace('\n', ' ')[:120])
    await live.run_command('!shuffle')
    r.check(len(state.queue) == depth - 1, 'shuffle nu a pierdut elemente')
    await live.run_command('!clear')
    r.check(not state.queue, 'clear goleste coada')


@scenario
async def stop_disconnects_and_forgets_the_panel(live, r):
    if not (live.vc and live.vc.is_connected()):
        r.note('sarit: nu e in voce')
        return
    panel = live.state.current_msg
    await live.run_command('!stop')
    gone = await live.wait(lambda: not (live.vc and live.vc.is_connected()), 20)
    r.check(gone, 'a ieșit din voce')
    r.check(not live.state.queue, 'a golit coada')
    r.check(live.state.current_msg is None, 'a uitat panoul din stare')
    if panel is not None:
        try:
            await live.text_channel.fetch_message(panel.id)
            r.check(False, 'panoul a rămas in canal cu butoane moarte')
        except discord.NotFound:
            r.check(True, 'panoul a fost sters din canal')
        except discord.HTTPException as e:
            r.note(f'nu am putut verifica panoul: {e}')


@scenario
async def two_plays_at_the_same_time_do_not_double_up(live, r):
    """Doua comenzi simultane: una porneste, cealalta intra in coada. Niciodata
    doua redari peste aceeasi conexiune."""
    await live.cleanup_session()
    mark = live.since()
    await asyncio.gather(live.run_command(f'!play {TRACK_A}'),
                         live.run_command(f'!play {TRACK_B}'))
    playing = await live.wait(lambda: live.vc and live.vc.is_playing(), 90)
    r.check(playing, 'una din ele a pornit')
    await asyncio.sleep(4)
    r.check(bool(live.vc and live.vc.is_playing()), 'nu s-au taiat una pe alta')
    total = len(live.state.queue) + (1 if live.state.last_title else 0)
    r.check(total <= 2, f'exact doua piese in sistem, nu mai multe ({total})')
    r.note(f'coada: {len(live.state.queue)}, curenta: {live.state.last_title!r}')
    r.note('mesaje: ' + live.text_of(mark).replace('\n', ' ')[:200])


@scenario
async def a_member_outside_the_channel_cannot_control(live, r):
    """Gardul de canal: cine nu e in voce nu are voie sa strice sesiunea altora."""
    if not (live.vc and live.vc.is_connected()):
        r.note('sarit: nu e in voce')
        return

    class _Elsewhere:
        id = 999999999999999999
        name = 'strain'
        display_name = 'strain'
        bot = False
        mention = '<@999999999999999999>'
        voice = None

    mark = live.since()
    await live.run_command('!skip', author=_Elsewhere())
    said = live.text_of(mark).lower()
    r.check(live.vc and live.vc.is_connected(), 'sesiunea a supraviețuit')
    r.check(bool(said.strip()), f'a raspuns strainului: {said[:120]!r}')


@scenario
async def always_on_joins_voice_and_turns_on_the_radio(live, r):
    await live.cleanup_session()
    mark = live.since()
    await live.run_command('!247')
    joined = await live.wait(lambda: live.vc and live.vc.is_connected(), 40)
    r.check(joined, 'intra in voce la !247 chiar fara coada')
    r.check(live.state.always_on, '24/7 e activ in stare')
    playing = await live.wait(lambda: live.vc and live.vc.is_playing(), 150)
    r.check(playing, 'radioul porneste ceva de la sine')
    r.note(f'autoplay={live.state.autoplay} coada={len(live.state.queue)}')
    r.note('mesaje: ' + live.text_of(mark).replace('\n', ' ')[:200])
    if playing:
        r.check(len(live.state.queue) >= 1, 'a umplut coada in avans pentru radio')


@scenario
async def always_on_off_leaves_voice(live, r):
    if not live.state.always_on:
        r.note('sarit: 24/7 nu e activ')
        return
    await live.run_command('!247')
    r.check(not live.state.always_on, '24/7 se stinge la a doua comanda')


@scenario
async def unplayable_links_fail_politely(live, r):
    await live.cleanup_session()
    for label, url in (('live', TRACK_LIVE), ('inexistent', TRACK_GONE),
                       ('in procesare', TRACK_LONG)):
        mark = live.since()
        try:
            await live.run_command(f'!play {url}')
        except Exception as e:
            r.check(False, f'{label}: comanda a aruncat {type(e).__name__}: {e}')
            continue
        await live.wait(lambda: len(live.said(mark)) >= 1, 90)
        said = live.text_of(mark).replace('\n', ' ')
        r.check(bool(said.strip()), f'{label}: a spus ceva ({said[:140]!r})')
        # Motivul trebuie sa fie REAL. Toate trei ieseau ca "Eroare necunoscuta"
        # cu textul in engleza citat inapoi si un "trimite-mi mesajul asta" —
        # pentru situatii pe care YouTube le spune limpede.
        r.check('necunoscut' not in said.lower(),
                f'{label}: raportat ca eroare necunoscuta ({said[:140]!r})')
        r.check(not (live.vc and live.vc.is_playing()),
                f'{label}: nu a pornit nimic')
        await asyncio.sleep(1)


@scenario
async def a_playlist_link_adds_more_than_one(live, r):
    await live.cleanup_session()
    mark = live.since()
    playlist = ('https://www.youtube.com/playlist?'
                'list=PLBCF2DAC6FFB574DE')          # playlist public, vechi si stabil
    await live.run_command(f'!play {playlist}')
    grew = await live.wait(lambda: len(live.state.queue) >= 2
                           or (live.vc and live.vc.is_playing()), 150)
    said = live.text_of(mark).replace('\n', ' ')
    r.check(grew, f'a adaugat piese din playlist (coada={len(live.state.queue)})')
    r.check(bool(said.strip()), f'a confirmat: {said[:160]!r}')


@scenario
async def a_long_queue_still_renders_a_valid_panel(live, r):
    """Discord respinge un embed peste 6000 de caractere, un cAmp peste 1024 si un
    select cu peste 25 de opțiuni. Un panou refuzat = butoane care nu exista."""
    state = live.state
    if not (live.vc and live.vc.is_connected()):
        await live.run_command(f'!play {TRACK_A}')
        await live.wait(lambda: live.vc and live.vc.is_connected(), 40)
    long_title = ('Formatia Bombastic feat. Cineva - Melodia Cu Numele Cel Mai '
                  'Lung Din Univers Care Nu Se Mai Termina Niciodata ')
    state.queue.extend({'query': f'https://youtu.be/id{i:05d}',
                        'title': long_title + str(i)} for i in range(40))
    state.show_queue = True
    ctx = await live.run_command('!np')
    try:
        await ui.update_player_ui(ctx)
        r.check(True, 'panoul cu 40 de piese in coada a fost acceptat de Discord')
        msg = state.current_msg
        if msg is not None and msg.embeds:
            r.note(f'embed: {len(msg.embeds[0])} caractere, '
                   f'{len(msg.components)} randuri')
    except discord.HTTPException as e:
        r.check(False, f'Discord a refuzat panoul: {e}')
    finally:
        state.queue.clear()
        state.show_queue = False


@scenario
async def health_and_debug_answer(live, r):
    for cmd in ('!health', '!debug'):
        mark = live.since()
        await live.run_command(cmd)
        got = await live.wait(lambda: len(live.said(mark)) >= 1, 15)
        r.check(got, f'{cmd} raspunde')
        if got:
            r.note(f'{cmd}: ' + live.text_of(mark).replace('\n', ' ')[:220])


@scenario
async def commands_on_an_empty_session_do_not_explode(live, r):
    await live.cleanup_session()
    for cmd in ('!skip', '!stop', '!np', '!shuffle', '!clear', '!remove 1',
                '!move 1 2', '!seek 1:00'):
        mark = live.since()
        try:
            await live.run_command(cmd)
        except Exception as e:
            r.check(False, f'{cmd} a aruncat {type(e).__name__}: {e}')
            continue
        said = live.text_of(mark).replace('\n', ' ')
        r.check(bool(said.strip()), f'{cmd} -> {said[:90]!r}')
        await asyncio.sleep(0.4)


# --- rulare -------------------------------------------------------------------

async def drive():
    b = bot_mod.bot
    # NU `wait_until_ready()`: acela ridica RuntimeError daca e chemat inainte ca
    # `start()` sa fi ajuns la login, adica exact in cursa de la pornire.
    # `is_ready()` verifica intai daca event-ul exista, deci se poate interoga
    # oricand.
    for _ in range(4 * 150):
        if b.is_ready():
            break
        await asyncio.sleep(0.25)
    else:
        print('[live] nu s-a conectat la Discord in 150s', flush=True)
        return 2
    print(f'[live] conectat ca {b.user} in {len(b.guilds)} guild(uri)', flush=True)

    guild = b.get_guild(ARGS.guild)
    if guild is None:
        print(f'[live] botul nu e in guild {ARGS.guild}', flush=True)
        return 2
    voice = guild.get_channel(ARGS.voice)
    if not isinstance(voice, discord.VoiceChannel):
        print(f'[live] {ARGS.voice} nu e canal de voce in {guild.name}', flush=True)
        return 2

    text = guild.get_channel(ARGS.text) if ARGS.text else None
    if text is None:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                text = ch
                break
    if text is None:
        print('[live] niciun canal text in care sa pot scrie', flush=True)
        return 2

    members = [m for m in voice.members if not m.bot]
    if not members:
        print(f'[live] nu e nimeni in {voice.name}: intra in canal si porneste iar '
              f'(am nevoie de un membru real ca sa dau comenzile in numele lui)',
              flush=True)
        return 2
    member = members[0]

    perms = voice.permissions_for(guild.me)
    print(f'[live] guild={guild.name} voce={voice.name} text=#{text.name} '
          f'ca={member.display_name} | connect={perms.connect} speak={perms.speak}',
          flush=True)
    if not (perms.connect and perms.speak):
        print('[live] lipsesc permisiunile de Connect/Speak — Discord ar ignora '
              'cererea de voce fara nicio eroare', flush=True)
        return 2

    live = Live(guild, voice, text, member)

    async def record(message):
        if message.channel.id == text.id and message.author.id == b.user.id:
            live.sent.append(message)

    b.add_listener(record, 'on_message')

    wanted = {n.strip() for n in ARGS.only.split(',') if n.strip()}
    results = []
    try:
        for fn in SCENARIOS:
            if wanted and fn.__name__ not in wanted:
                continue
            r = Result(fn.__name__)
            t0 = time.perf_counter()
            print(f'\n[live] === {fn.__name__}', flush=True)
            try:
                await fn(live, r)
            except Exception as e:
                r.failures.append(f'FAIL excepție: {type(e).__name__}: {e}')
                traceback.print_exc()
            r.seconds = time.perf_counter() - t0
            for line in r.notes + r.failures:
                if line.strip():
                    print('   ' + line, flush=True)
            print(f'   ({r.seconds:.1f}s)', flush=True)
            results.append(r)
    finally:
        try:
            await live.cleanup_session()
        except Exception:
            traceback.print_exc()
        if not ARGS.keep:
            for m in live.mine + live.sent:
                try:
                    await m.delete()
                except Exception:
                    pass

    bad = [r for r in results if r.failures]
    print('\n[live] ================ RAPORT ================', flush=True)
    for r in results:
        print(('FAIL' if r.failures else 'OK  ') + f' {r.name} ({r.seconds:.1f}s)',
              flush=True)
        for f in r.failures:
            print('        ' + f, flush=True)
    print(f'[live] {len(results) - len(bad)}/{len(results)} scenarii OK', flush=True)
    return 1 if bad else 0


async def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print('[live] DISCORD_TOKEN lipseste. Ruleaza prin: '
              'railway run --service new_dsc -- python -B tools/livecheck.py ...',
              flush=True)
        return 2

    b = bot_mod.bot

    if ARGS.ipv6:
        _allow_ipv6()

    # Surd la mesaje reale: raspunde doar producția, o singura data.
    async def _deaf(_message):
        return
    b.on_message = _deaf

    # Si niciodata o sincronizare de comenzi slash din test: ar putea Șterge
    # inregistrarea reala a lui /play pentru toata lumea.
    async def _no_sync(*a, **k):
        print('[live] sync de comenzi blocat (deliberat)', flush=True)
        return []
    b.tree.sync = _no_sync

    code = 2
    driver = asyncio.create_task(drive())
    runner = asyncio.create_task(b.start(token))
    try:
        done, _ = await asyncio.wait({driver, runner},
                                     return_when=asyncio.FIRST_COMPLETED)
        if driver in done:
            code = driver.result()
        else:
            runner.result()          # ridica motivul real al deconectarii
    finally:
        driver.cancel()
        try:
            await b.close()
        except Exception:
            pass
    return code


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    logging.getLogger('discord').setLevel(logging.WARNING)
    sys.exit(asyncio.run(main()))
