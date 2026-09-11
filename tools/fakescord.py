"""Un Discord fals, dar SEVER. Nu un mock — un emulator care refuza ce refuza Discord.

Scopul e sa prinda exact clasele de bug-uri pe care fake-urile din `tests/` nu le
pot vedea, fiindca acolo fiecare fals spune "da" la orice:

- o interactiune neconfirmata in 3 secunde ("Gogu didn't respond in time");
- un payload pe care Discord il respinge cu 400 (valori duplicate intr-un select,
  un embed peste 6000 de caractere, un field peste 1024);
- un click pe un component care nu mai e inregistrat (butoane moarte) — de aceea
  dispecerizarea trece prin ViewStore-ul REAL din discord.py, nu prin al nostru;
- un edit pe un mesaj sters (404).

Ce e real aici: tot codul botului, bucla asyncio, timpii, FFmpeg si Opus.
Ce e fals: transportul Discord si YouTube.
"""
import asyncio
import itertools
import time

import discord
from discord.ui.view import ViewStore

# Limitele reale ale API-ului Discord, in ordinea in care le lovește un bot de
# muzica. Verificate in documentația API si in validarile lui discord.py.
MAX_EMBED_TOTAL = 6000
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_TITLE = 256
MAX_EMBED_AUTHOR = 256
MAX_EMBED_FOOTER = 2048
MAX_EMBED_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_MESSAGE_CONTENT = 2000
MAX_ACTION_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_SELECT_OPTIONS = 25
MAX_CHOICE_LABEL = 100
MAX_CHOICE_VALUE = 100
# Cat are un bot ca sa confirme o interactiune inainte ca Discord sa o declare
# eșuata si sa arate "<bot> didn't respond in time".
INTERACTION_DEADLINE_SEC = 3.0
# Un cadru Opus la discord.py: 20ms.
FRAME_SEC = 0.02


class Rejected(Exception):
    """Ce ar fi intors Discord ca 400 Invalid Form Body."""


class Timeline:
    """Tot ce s-a intamplat, in ordine, cu momentul exact."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.events = []
        self.problems = []

    def at(self) -> float:
        return time.monotonic() - self.t0

    def add(self, kind: str, detail: str) -> None:
        self.events.append((self.at(), kind, detail))

    def problem(self, kind: str, detail: str) -> None:
        self.problems.append((self.at(), kind, detail))
        self.events.append((self.at(), f'!! {kind}', detail))

    def dump(self, only_problems: bool = False) -> str:
        rows = self.problems if only_problems else self.events
        out = []
        for entry in rows:
            elapsed, kind, detail = entry
            out.append(f'  {elapsed:7.2f}s  {kind:<22} {detail}')
        return '\n'.join(out)


def validate_embed(embed) -> None:
    """Exact verificarile care produc un 400 pe un embed."""
    if embed is None:
        return
    total = 0
    title = embed.title or ''
    if len(title) > MAX_EMBED_TITLE:
        raise Rejected(f'embed.title {len(title)} > {MAX_EMBED_TITLE}')
    total += len(title)
    desc = embed.description or ''
    if len(desc) > MAX_EMBED_DESCRIPTION:
        raise Rejected(f'embed.description {len(desc)} > {MAX_EMBED_DESCRIPTION}')
    total += len(desc)
    author = (embed.author.name if embed.author else None) or ''
    if len(author) > MAX_EMBED_AUTHOR:
        raise Rejected(f'embed.author.name {len(author)} > {MAX_EMBED_AUTHOR}')
    total += len(author)
    footer = (embed.footer.text if embed.footer else None) or ''
    if len(footer) > MAX_EMBED_FOOTER:
        raise Rejected(f'embed.footer.text {len(footer)} > {MAX_EMBED_FOOTER}')
    total += len(footer)
    if len(embed.fields) > MAX_EMBED_FIELDS:
        raise Rejected(f'{len(embed.fields)} fields > {MAX_EMBED_FIELDS}')
    for field in embed.fields:
        name, value = field.name or '', field.value or ''
        if len(name) > MAX_FIELD_NAME:
            raise Rejected(f'field.name {len(name)} > {MAX_FIELD_NAME}')
        if len(value) > MAX_FIELD_VALUE:
            raise Rejected(f'field.value {len(value)} > {MAX_FIELD_VALUE}')
        if not value:
            raise Rejected('field.value gol')
        total += len(name) + len(value)
    if total > MAX_EMBED_TOTAL:
        raise Rejected(f'embed total {total} > {MAX_EMBED_TOTAL}')


def validate_view(view) -> None:
    """Exact verificarile care produc un 400 pe componente."""
    if view is None:
        return
    rows = {}
    for child in view.children:
        row = getattr(child, 'row', None)
        rows.setdefault(row, []).append(child)
        custom_id = getattr(child, 'custom_id', None)
        if not custom_id:
            raise Rejected(f'component fara custom_id: {child!r}')
        label = getattr(child, 'label', None)
        if label is not None and len(label) > MAX_CHOICE_LABEL:
            raise Rejected(f'label {len(label)} > {MAX_CHOICE_LABEL}: {label[:40]!r}')
        if isinstance(child, discord.ui.Select):
            options = child.options
            if not options:
                raise Rejected('select fara nicio opțiune')
            if len(options) > MAX_SELECT_OPTIONS:
                raise Rejected(f'{len(options)} opțiuni > {MAX_SELECT_OPTIONS}')
            seen = set()
            for opt in options:
                if not opt.value:
                    raise Rejected('SelectOption cu valoare goala')
                if len(opt.value) > MAX_CHOICE_VALUE:
                    raise Rejected(f'valoare {len(opt.value)} > {MAX_CHOICE_VALUE}')
                if len(opt.label) > MAX_CHOICE_LABEL:
                    raise Rejected(f'label opțiune {len(opt.label)} > {MAX_CHOICE_LABEL}')
                if opt.value in seen:
                    raise Rejected(f'valori duplicate in select: {opt.value[:40]!r}')
                seen.add(opt.value)
    if len(rows) > MAX_ACTION_ROWS:
        raise Rejected(f'{len(rows)} randuri > {MAX_ACTION_ROWS}')
    for row, children in rows.items():
        buttons = [c for c in children if isinstance(c, discord.ui.Button)]
        if len(buttons) > MAX_BUTTONS_PER_ROW:
            raise Rejected(f'randul {row}: {len(buttons)} butoane > {MAX_BUTTONS_PER_ROW}')


_ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, channel, embed=None, content=None, view=None,
                 delete_after=None):
        self.id = next(_ids)
        self.channel = channel
        self.embed = embed
        self.content = content
        self.view = view
        self.deleted = False
        self.edits = 0
        self.flags = type('F', (), {'components_v2': False})()

    def __repr__(self):
        if self.content:
            return f'<msg {self.id} {self.content[:60]!r}>'
        author = (self.embed.author.name if self.embed and self.embed.author
                  else None)
        return f'<panel {self.id} {author!r}>'

    async def edit(self, **kwargs):
        if self.deleted:
            raise discord.NotFound(_FakeResponse(404), 'Unknown Message')
        embed = kwargs.get('embed', self.embed)
        view = kwargs.get('view', self.view)
        validate_embed(embed)
        validate_view(view)
        self.embed, self.view = embed, view
        self.edits += 1
        self.channel.timeline.add('panel.edit', repr(self))
        # Exact ce face Message.edit in discord.py 2.7.1 (message.py:1417).
        if view and not view.is_finished() and view.is_dispatchable():
            self.channel.store.add_view(view, self.id)
        return self

    async def delete(self, delay=None):
        if self.deleted:
            raise discord.NotFound(_FakeResponse(404), 'Unknown Message')
        self.deleted = True
        self.channel.timeline.add('msg.delete', repr(self))


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = 'simulat'


class FakeTextChannel:
    def __init__(self, timeline, store):
        self.timeline = timeline
        self.store = store
        self.messages = []

    async def send(self, content=None, *, embed=None, view=None,
                   delete_after=None, **kwargs):
        if content is not None and len(str(content)) > MAX_MESSAGE_CONTENT:
            raise Rejected(f'content {len(str(content))} > {MAX_MESSAGE_CONTENT}')
        validate_embed(embed)
        validate_view(view)
        msg = FakeMessage(self, embed=embed, content=content, view=view)
        self.messages.append(msg)
        if content:
            self.timeline.add('msg.send', str(content)[:110])
        else:
            self.timeline.add('panel.send', repr(msg))
        if view and view.is_dispatchable():
            self.store.add_view(view, msg.id)
        if delete_after is not None:
            async def later():
                await asyncio.sleep(delete_after)
                if not msg.deleted:
                    msg.deleted = True
            asyncio.get_running_loop().create_task(later())
        return msg


class FakeVoiceChannel:
    def __init__(self, timeline, *, name='General', bitrate=64000,
                 connect_perm=True, speak_perm=True, connect_delay=0.2,
                 outcomes=()):
        self.timeline = timeline
        self.name = name
        self.id = 555
        self.bitrate = bitrate
        self.members = []
        self.connect_calls = []
        self._perms = type('P', (), {'connect': connect_perm,
                                     'speak': speak_perm})()
        self._delay = connect_delay
        self._outcomes = list(outcomes)
        self.guild = None

    def permissions_for(self, member):
        return self._perms

    async def connect(self, *, timeout=None, **kwargs):
        # Exact ordinea din discord.py: refuzul e IMEDIAT si slotul se ocupa
        # INAINTE de orice await. Fara asta, doua comenzi simultane ar primi
        # fiecare propriul client de voce, a doua l-ar suprascrie pe prima, iar
        # prima ar cânta intr-un client orfan — o situatie care in producție nu
        # exista, deci un scenariu construit pe ea ar masura fantome.
        if self.guild.voice_client is not None:
            raise discord.ClientException('Already connected to a voice channel.')
        started = time.monotonic()
        self.connect_calls.append(timeout)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        vc = FakeVoiceClient(self)
        self.guild.voice_client = vc
        try:
            await asyncio.sleep(self._delay)
            if isinstance(outcome, BaseException):
                raise outcome
        except BaseException:
            # discord.py isi curata clientul pe jumatate deschis inainte sa
            # re-ridice (voice_state.py:_wrap_connect -> disconnect()).
            self.guild.voice_client = None
            self.timeline.add('voice.connect',
                              f'EȘEC dupa {time.monotonic()-started:.2f}s: '
                              f'{type(outcome).__name__}')
            raise
        self.timeline.add('voice.connect',
                          f'{self.name} in {time.monotonic()-started:.2f}s '
                          f'(bitrate {self.bitrate})')
        return vc


class FakeVoiceClient:
    """Consuma cadre REALE din sursa audio, la cadenta reala de 20ms."""

    def __init__(self, channel):
        self.channel = channel
        self.timeline = channel.timeline
        self._connected = True
        self._source = None
        self._task = None
        self._paused = False
        self._stopped = False
        self.frames_read = 0
        self.plays = []

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._task is not None and not self._task.done() and not self._paused

    def is_paused(self):
        return self._task is not None and not self._task.done() and self._paused

    def play(self, source, *, after=None):
        if self.is_playing():
            raise discord.ClientException('Already playing audio.')
        self._source = source
        self._paused = False
        self._stopped = False
        self.frames_read = 0
        self.plays.append(source)
        self.timeline.add('voice.play', type(source).__name__)
        self._task = asyncio.get_running_loop().create_task(self._pump(source, after))

    async def _pump(self, source, after):
        error = None
        try:
            # Cadenta REALA de 20ms per cadru, ca la discord.py. Consumul rapid
            # pare o economie de timp, dar termina piesa in mijlocul scenariului si
            # declanseaza `after_play` — adica masori altceva decat crezi.
            loop = asyncio.get_running_loop()
            started = loop.time()
            while not self._stopped:
                while self._paused and not self._stopped:
                    await asyncio.sleep(0.01)
                    started = loop.time() - self.frames_read * FRAME_SEC
                if self._stopped:
                    break
                data = await loop.run_in_executor(None, source.read)
                if not data:
                    break
                self.frames_read += 1
                drift = started + self.frames_read * FRAME_SEC - loop.time()
                await asyncio.sleep(max(0.0, drift))
        except Exception as e:                                  # noqa: BLE001
            error = e
        finally:
            try:
                source.cleanup()
            except Exception:                                  # noqa: BLE001
                pass
            self.timeline.add('voice.ended', f'{self.frames_read} cadre opus')
            if after is not None:
                after(error)

    def pause(self):
        self._paused = True
        self.timeline.add('voice.pause', '')

    def resume(self):
        self._paused = False
        self.timeline.add('voice.resume', '')

    def stop(self):
        self._stopped = True
        self._paused = False
        self.timeline.add('voice.stop', '')

    async def disconnect(self, *, force=False):
        self.stop()
        self._connected = False
        if self.channel.guild is not None:
            self.channel.guild.voice_client = None
        self.timeline.add('voice.disconnect', f'force={force}')

    async def move_to(self, channel):
        self.channel = channel
        self.timeline.add('voice.move', channel.name)


class FakeGuild:
    def __init__(self, timeline, guild_id=999):
        self.id = guild_id
        self.timeline = timeline
        self.voice_client = None
        self.me = FakeMember('Gogu', bot=True)


class FakeMember:
    def __init__(self, name, *, bot=False, channel=None):
        self.id = next(_ids)
        self.name = name
        self.display_name = name
        self.bot = bot
        self.voice = type('V', (), {'channel': channel})() if channel else None

    def join(self, channel):
        self.voice = type('V', (), {'channel': channel})()
        if self not in channel.members:
            channel.members.append(self)

    def leave(self):
        if self.voice and self in self.voice.channel.members:
            self.voice.channel.members.remove(self)
        self.voice = None


class FakeContext:
    """Ce atinge codul din `ctx`, si nimic mai mult."""

    def __init__(self, guild, channel, author, *, interaction=None, message=None):
        self.guild = guild
        self.channel = channel
        self.author = author
        self.interaction = interaction
        self.message = message
        self.bot = None          # setat de harness
        self.command = None
        self.id = guild.id

    @property
    def voice_client(self):
        return self.guild.voice_client

    async def send(self, content=None, **kwargs):
        if self.interaction is not None and not self.interaction.acked:
            self.interaction.ack('send prin ctx.send')
        return await self.channel.send(content, **kwargs)

    async def defer(self, *a, **k):
        if self.interaction is not None:
            self.interaction.ack('defer')


class FakeInteraction:
    """Impune fereastra de 3 secunde. Un ack ratat = "didn't respond in time"."""

    def __init__(self, timeline, custom_id, user, message=None, values=None,
                 kind='component'):
        self.timeline = timeline
        self.custom_id = custom_id
        self.user = user
        self.message = message
        self.data = {'custom_id': custom_id}
        if values is not None:
            self.data['values'] = values
        self.type = type('T', (), {'name': kind})()
        self.created = time.monotonic()
        self.created_at = discord.utils.utcnow()
        self.acked = False
        self.ack_latency = None
        self.response = _InteractionResponse(self)

    def ack(self, how: str) -> None:
        if self.acked:
            raise discord.InteractionResponded(self)
        self.acked = True
        self.ack_latency = time.monotonic() - self.created
        if self.ack_latency > INTERACTION_DEADLINE_SEC:
            self.timeline.problem(
                'interactiune expirata',
                f'{self.custom_id}: confirmata dupa {self.ack_latency:.2f}s '
                f'(> {INTERACTION_DEADLINE_SEC}s) — utilizatorul vede '
                f'"didn\'t respond in time"')
        else:
            self.timeline.add('interaction.ack',
                              f'{self.custom_id} in {self.ack_latency*1000:.0f}ms '
                              f'({how})')

    def finish(self) -> None:
        if not self.acked:
            self.timeline.problem(
                'interactiune neconfirmata',
                f'{self.custom_id}: niciun raspuns — utilizatorul vede '
                f'"didn\'t respond in time"')


class _InteractionResponse:
    def __init__(self, interaction):
        self._it = interaction

    def is_done(self):
        return self._it.acked

    async def defer(self, **kwargs):
        self._it.ack('defer')

    async def send_message(self, content=None, **kwargs):
        self._it.ack(f'mesaj: {str(content)[:60]!r}')
        if self._it.message is not None:
            self._it.message.channel.timeline.add('ephemeral', str(content)[:110])


class Fakescord:
    """Serverul de test: un guild, un canal text, un canal de voce, membri."""

    def __init__(self, *, bitrate=64000, connect_perm=True, speak_perm=True,
                 connect_delay=0.2, connect_outcomes=()):
        self.timeline = Timeline()
        self.store = ViewStore(None)
        self.guild = FakeGuild(self.timeline)
        self.text = FakeTextChannel(self.timeline, self.store)
        self.voice = FakeVoiceChannel(self.timeline, bitrate=bitrate,
                                      connect_perm=connect_perm,
                                      speak_perm=speak_perm,
                                      connect_delay=connect_delay,
                                      outcomes=connect_outcomes)
        self.voice.guild = self.guild
        self.laur = FakeMember('laurentiu')
        self.other = FakeMember('altcineva')

    def ctx(self, author=None, *, interaction=None):
        return FakeContext(self.guild, self.text, author or self.laur,
                           interaction=interaction)

    def panel(self):
        """Ultimul panou nesters din canal."""
        for msg in reversed(self.text.messages):
            if msg.embed is not None and not msg.deleted:
                return msg
        return None

    async def click(self, custom_id, *, user=None, values=None):
        """Apasa un component EXACT cum o face Discord: prin ViewStore.

        Cand nimic nu e inregistrat pentru (tip, mesaj, custom_id), discord.py
        arunca clickul cu un `_log.debug` si utilizatorul vede o interactiune
        eșuata. Aici asta devine o problema raportata, nu o linie invizibila.
        """
        panel = self.panel()
        if panel is None:
            self.timeline.problem('click fara panou', custom_id)
            return None
        component_type = 3 if values is not None else 2
        item = None
        for key in ((component_type, panel.id, custom_id),):
            dispatch = self.store._views.get(key[1]) or {}
            item = dispatch.get((key[0], key[2]))
        if item is None:
            self.timeline.problem(
                'component neinregistrat',
                f'{custom_id}: Discord nu gasește niciun handler — '
                f'utilizatorul vede o interactiune eșuata')
            return None
        interaction = FakeInteraction(self.timeline, custom_id,
                                      user or self.laur, message=panel,
                                      values=values)
        self.timeline.add('click', custom_id + (f' -> {values}' if values else ''))
        view = item.view
        if values is not None:
            item._refresh_state(interaction, {'values': values})
        try:
            allow = await view.interaction_check(interaction)
            if allow:
                await item.callback(interaction)
        except Exception as e:                                  # noqa: BLE001
            self.timeline.problem('excepție in buton',
                                  f'{custom_id}: {type(e).__name__}: {e}')
        interaction.finish()
        return interaction
