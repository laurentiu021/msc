"""Toate comenzile muzicale."""
import asyncio
import os
import random
import re
import time
from urllib.parse import urlparse

import discord

from music.config import (FFMPEG_OPTS, cookies_available, log,
                          make_search_opts)
from music.state import get_state, guild_states, loading, set_autoplay
from music.utils import safe_delete, format_time, cleanup_file, item_title
from music.autoplay import prefill_autoplay_queue
from music import diag
from music import ytdlp
import music.player as player_mod

# Doar aceste host-uri sunt acceptate ca URL. Fara allowlist, orice string cu
# schema ajungea la extractorul generic al yt-dlp: el urmarea URL-ul, iar un
# raspuns application/x-mpegurl devenea formate HLS reale pe care botul le reda.
# Cererea duce si cookiefile-ul, deci un Set-Cookie ostil ajungea pe volum.
ALLOWED_HOSTS = {
    'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
    'youtu.be', 'www.youtu.be',
    'open.spotify.com', 'spotify.com',
    'deezer.com', 'www.deezer.com', 'link.deezer.com',
}


def sanitize_query(raw: str) -> tuple[str | None, str | None]:
    """(interogare, motiv_respingere). Ce nu e URL devine o cautare pe YouTube."""
    q = (raw or '').strip()
    if not q:
        return None, "Nu ai scris nimic."
    if q.startswith('spotify:'):
        # URI-urile spotify: nu erau prinse de verificarea pe 'spotify.com/', deci
        # plecau la YouTube ca text brut si botul cauta ID-ul opac. Le aducem la
        # forma web, ca sa treaca prin acelasi resolver ca linkurile normale.
        parts = [part for part in q.split(':') if part]
        if len(parts) >= 3:
            return f"https://open.spotify.com/{parts[1]}/{parts[2]}", None
        return None, "Link Spotify pe care nu il pot citi."
    # Verificam SCHEMA, nu prezenta lui '://': 'data:audio/mpeg;base64,...' si
    # 'javascript:' nu au '://' si treceau ca text de cautare.
    scheme_match = re.match(r'^([a-zA-Z][a-zA-Z0-9+.\-]*):', q)
    if not scheme_match:
        return q, None
    scheme = scheme_match.group(1).lower()
    if scheme not in ('http', 'https'):
        return None, f"Schema `{scheme}` nu e permisa."
    parsed = urlparse(q)
    host = (parsed.hostname or '').lower()
    if host not in ALLOWED_HOSTS:
        return None, (f"Host neacceptat: `{host}`. "
                      f"Accept doar YouTube, Spotify si Deezer.")
    return q, None


def setup_music_commands(bot, process_play, play_next, update_player_ui, start_timeout, cancel_timeout):
    """Inregistreaza toate comenzile muzicale pe bot."""

    async def _ensure_voice(ctx):
        """Conecteaza-te la canalul autorului. None daca nu se poate.

        connect() nu avea niciun guard: fara permisiunea Connect utilizatorul
        aștepta 30 de secunde fara niciun raspuns.
        """
        if ctx.voice_client:
            return ctx.voice_client
        try:
            return await ctx.author.voice.channel.connect(timeout=15.0)
        except asyncio.TimeoutError:
            await ctx.send("Nu am reusit sa intru in voce (timeout).", delete_after=10)
        except discord.ClientException as e:
            log.warning(f"Voice connect: {e}")
            await ctx.send("Sunt deja conectat altundeva.", delete_after=10)
        except discord.HTTPException as e:
            log.warning(f"Voice connect HTTP: {e}")
            await ctx.send("Nu am permisiunea sa intru in canalul tau de voce.",
                           delete_after=10)
        return None

    async def _resolve_platform_url(query: str):
        """Spotify/Deezer -> text de cautare. None cand nu putem citi linkul.

        yt-dlp nu are extractor pentru Spotify sau Deezer (verificat pe build-ul
        instalat: nimic care sa se potriveasca), deci totul depinde de un scrape
        generic al paginii. Cand acela eșua, varianta veche cauta pe YouTube
        ULTIMUL segment din URL — un ID opac precum "4cOdK2wGLETKBW3PvgPWqT" —
        si reda vesel orice rezultat, fara eroare pentru utilizator si fara nicio
        linie de log la nivel INFO.
        """
        if not any(p in query for p in ('spotify.com/', 'deezer.com/')):
            return query
        try:
            info = await ytdlp.extract(
                {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                 'socket_timeout': 15},
                query, loop=bot.loop, stage='platform_resolve')
        except Exception as e:
            log.warning(f"Nu am putut citi linkul de platforma: {e}")
            return None
        title = (info or {}).get('title') or ''
        artist = (info or {}).get('artist') or (info or {}).get('uploader') or ''
        if not title:
            log.warning("Linkul de platforma nu a intors niciun titlu")
            return None
        search = f"{artist} {title}".strip() if artist else title
        log.info(f"Link de platforma rezolvat ca: {search}")
        return search

    @bot.command()
    async def play(ctx, *, search):
        await safe_delete(ctx.message)
        search, reason = sanitize_query(search)
        if reason:
            return await ctx.send(reason, delete_after=10)
        if not ctx.author.voice:
            return await ctx.send("Intra pe voce!", delete_after=5)
        vc = await _ensure_voice(ctx)
        if vc is None:
            return
        state = get_state(ctx.guild.id)
        cancel_timeout(ctx)

        if any(p in search for p in ['spotify.com/', 'deezer.com/']):
            resolved = await _resolve_platform_url(search)
            if resolved is None:
                return await ctx.send(
                    "Nu pot citi linkul de Spotify/Deezer. Scrie artistul si titlul.",
                    delete_after=15)
            search = resolved

        if 'list=' in search and 'youtube.com' in search:
            ydl_opts_pl = make_search_opts(
                with_cookies=cookies_available(),
                extract_flat=True, playlistend=30, noplaylist=False,
            )
            try:
                # Marcam ocupat INAINTE de extractie. Fara asta, un !play dat in
                # timpul citirii unui playlist de 30 de intrari nu vedea nimic
                # ocupat si pornea propria rezolvare in paralel.
                with loading(state):
                    info = await ytdlp.extract(ydl_opts_pl, search,
                                               loop=bot.loop, stage='playlist')
                entries = info.get('entries', [])
                if not entries: raise ValueError("Playlist gol")
                first = entries.pop(0)
                for e in entries:
                    url = e.get('url') or e.get('id')
                    if url:
                        if not url.startswith('http'):
                            url = f"https://www.youtube.com/watch?v={url}"
                        state.queue.append({'query': url, 'title': e.get('title') or 'Necunoscut'})
                first_url = first.get('url') or first.get('id')
                if first_url and not first_url.startswith('http'):
                    first_url = f"https://www.youtube.com/watch?v={first_url}"
                if vc.is_playing() or vc.is_paused() or state.is_loading:
                    state.queue.insert(0, {'query': first_url, 'title': first.get('title') or 'Necunoscut'})
                    await update_player_ui(ctx)
                else:
                    await process_play(ctx, first_url)
            except Exception as e:
                log.error(f"Eroare playlist: {e}")
                state.last_raw_error = str(e)[:600]
                await ctx.send("Nu am putut citi playlist-ul.", delete_after=15)
                if not (vc.is_playing() or vc.is_paused()):
                    start_timeout(ctx)
            return

        if vc.is_playing() or vc.is_paused() or state.is_loading:
            state.queue.append({'query': search, 'title': search})
            # Confirmare explicita. Comanda isi sterge propriul mesaj, iar
            # actualizarea panoului schimba doar contorul din footer, pe un panou
            # care poate fi mult mai sus in canal — deci cea mai folosita comanda
            # putea sa nu produca nimic vizibil.
            await ctx.send(
                f"➕ **#{len(state.queue)}** in coada: {item_title(search, 60)}"
                + ("  *(se incarca altceva chiar acum)*" if state.is_loading else ""),
                delete_after=12)
            await update_player_ui(ctx)
        else:
            await process_play(ctx, search)

    @bot.command()
    async def stop(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        state.queue.clear(); set_autoplay(state, False, by_user=True)
        state.loop_mode = 0
        state.is_loading = False; state.always_on = False
        # Fisierul rămâne in cache; doar nu mai e "al" sesiunii.
        state.current_file = None
        player_mod.trim_cache()
        player_mod.bump_play_generation(state)
        cancel_timeout(ctx)
        if ctx.voice_client: await ctx.voice_client.disconnect()
        await safe_delete(state.current_msg)
        state.current_msg = None

    @bot.command()
    async def skip(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        vc = ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state.skip_request = True
            vc.stop()
        else:
            await ctx.send("Nu se reda nimic.", delete_after=5)

    @bot.command()
    async def nplay(ctx, *, search):
        await safe_delete(ctx.message)
        search, reason = sanitize_query(search)
        if reason:
            return await ctx.send(reason, delete_after=10)
        if not ctx.author.voice: return await ctx.send("Intra pe voce!", delete_after=5)
        vc = await _ensure_voice(ctx)
        if vc is None:
            return
        state = get_state(ctx.guild.id)
        cancel_timeout(ctx)
        if any(p in search for p in ['spotify.com/', 'deezer.com/']):
            resolved = await _resolve_platform_url(search)
            if resolved is None:
                return await ctx.send(
                    "Nu pot citi linkul de Spotify/Deezer. Scrie artistul si titlul.",
                    delete_after=15)
            search = resolved
        if state.is_loading:
            # O rezolvare e deja in curs. Inainte, !nplay pornea a doua in
            # paralel: doua cereri catre acelasi YouTube care ne limiteaza, si
            # doua redari care se calcau. Trece prima in coada si va porni de
            # indata ce se termina incarcarea curenta.
            state.queue.insert(0, {'query': search, 'title': search})
            await ctx.send("Se incarca deja o piesa — a ta urmeaza imediat.",
                           delete_after=10)
            return await update_player_ui(ctx)
        state.skip_request = True
        await process_play(ctx, search)

    @bot.command()
    async def np(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        if not state.last_title: return await ctx.send("Nu se reda nimic.", delete_after=5)
        await update_player_ui(ctx, send_new=True)

    @bot.command()
    async def shuffle(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        if len(state.queue) < 2: return await ctx.send("Coada e prea scurta.", delete_after=5)
        random.shuffle(state.queue)
        await ctx.send(f"Coada amestecata ({len(state.queue)} piese).", delete_after=5)
        await update_player_ui(ctx)

    @bot.command()
    async def clear(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        n = len(state.queue); state.queue.clear()
        await ctx.send(f"Coada golita ({n} piese).", delete_after=5)
        await update_player_ui(ctx)

    @bot.command()
    async def remove(ctx, index: int):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        if index < 1 or index > len(state.queue):
            return await ctx.send(f"Index invalid (1-{len(state.queue)}).", delete_after=5)
        removed = state.queue.pop(index - 1)
        await ctx.send(f"Scos: {item_title(removed, 50)}", delete_after=5)
        await update_player_ui(ctx)

    @bot.command()
    async def move(ctx, from_idx: int, to_idx: int):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        if from_idx < 1 or from_idx > len(state.queue) or to_idx < 1 or to_idx > len(state.queue):
            return await ctx.send(f"Index invalid (1-{len(state.queue)}).", delete_after=5)
        item = state.queue.pop(from_idx - 1)
        state.queue.insert(to_idx - 1, item)
        await ctx.send(f"Mutat '{item_title(item, 40)}' -> #{to_idx}.", delete_after=5)
        await update_player_ui(ctx)

    @bot.command()
    async def seek(ctx, timestamp: str):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        vc = ctx.voice_client
        if not vc or not vc.is_playing():
            return await ctx.send("Nu se reda nimic.", delete_after=5)
        if not state.current_file or not os.path.exists(state.current_file):
            return await ctx.send("Nu pot face seek.", delete_after=5)
        parts = timestamp.split(':')
        try:
            if len(parts) == 1: seconds = int(parts[0])
            elif len(parts) == 2: seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3: seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else: raise ValueError()
        except ValueError:
            return await ctx.send("Format invalid. Ex: !seek 1:30", delete_after=5)
        if state.last_duration and seconds >= state.last_duration:
            return await ctx.send("Depaseste durata piesei.", delete_after=5)
        filename = state.current_file
        if not filename or not os.path.exists(filename):
            return await ctx.send("Nu am fisierul piesei ca sa pot cauta in el.", delete_after=5)
        # Invalidam callback-ul piesei curente INAINTE de stop: altfel el avansa
        # coada si stergea exact fisierul in care cautam.
        player_mod.bump_play_generation(state)
        vc.stop()
        await asyncio.sleep(0.3)
        state.last_start_time = time.time() - seconds
        after_play = player_mod.make_after_play(ctx, state, filename)
        seek_opts = dict(FFMPEG_OPTS)
        seek_opts['before_options'] = f'-ss {seconds}'
        try:
            source = await discord.FFmpegOpusAudio.from_probe(filename, **seek_opts)
            vc.play(source, after=after_play)
        except Exception:
            vc.play(discord.FFmpegPCMAudio(filename, **seek_opts), after=after_play)
        await ctx.send(f"Seek la `{format_time(seconds)}`", delete_after=5)
        await update_player_ui(ctx)

    @bot.command(name='247')
    async def always_on(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        state.always_on = not state.always_on
        if state.always_on:
            cancel_timeout(ctx)
            set_autoplay(state, True, by_user=True); state.loop_mode = 0
            state.show_queue = True
            state.breaker_until = 0.0
            state.idle_quiet_until = 0.0
            if not state.queue and state.last_url:
                try: await prefill_autoplay_queue(state, bot.loop)
                except Exception: pass
            await ctx.send("24/7 ON - autoplay activat.", delete_after=5)
            await update_player_ui(ctx)
        else:
            set_autoplay(state, False, by_user=True)
            state.show_queue = False
            await ctx.send("24/7 OFF.", delete_after=5)
            await update_player_ui(ctx)
            if ctx.voice_client and not ctx.voice_client.is_playing(): start_timeout(ctx)

    # Numele si descrierea fiecarei comenzi, o singura data. Ajutorul se
    # construieste din tabelul asta, iar un test verifica mecanic ca fiecare
    # comanda inregistrata pe bot apare aici — altfel o comanda noua ar exista
    # fara ca nimeni sa afle de ea.
    HELP_SECTIONS = [
        ("🎵 Muzică", [
            ('play', '<piesa sau link>', 'Reda acum, sau adauga in coada'),
            ('nplay', '<piesa sau link>', 'Reda imediat, peste piesa curenta'),
            ('skip', '', 'Trece la piesa urmatoare'),
            ('stop', '', 'Opreste tot si iese din canal'),
            ('np', '', 'Ce se reda acum (panoul cu butoane)'),
            ('seek', '<1:30>', 'Salt la un moment din piesa'),
        ]),
        ("📋 Coada", [
            ('shuffle', '', 'Amesteca coada'),
            ('clear', '', 'Goleste coada'),
            ('remove', '<nr>', 'Scoate piesa cu numarul dat'),
            ('move', '<de la> <la>', 'Mută o piesa in alta poziție'),
            ('247', '', 'Rămâne in canal non-stop, cu autoplay'),
        ]),
        ("🔧 Diagnostic", [
            ('health', '', 'De ce nu merge: cookies, PO Token, cota, erori'),
            ('debug', '', 'Latenta, CPU, RAM, coada'),
            ('help', '', 'Lista asta'),
        ]),
    ]

    def _help_embed():
        embed = discord.Embed(
            title="Comenzi Gogu",
            description="Prefix `!` (si `!PLAY` merge la fel de bine).",
            color=0x2b2d31)
        for section, rows in HELP_SECTIONS:
            embed.add_field(
                name=section,
                value="\n".join(
                    f"`!{name}{' ' + args if args else ''}` — {desc}"
                    for name, args, desc in rows),
                inline=False)
        embed.set_footer(text="Butoanele de sub piesa fac acelasi lucru fara "
                              "sa scrii · YouTube, Spotify si Deezer")
        return embed

    # `!help` e numele principal. discord.py primeste help_command=None (ajutorul
    # lui implicit nu stie de comenzile noastre), deci fara inregistrarea asta
    # `!help` nu facea absolut nimic si nimeni nu putea afla ce comenzi exista.
    @bot.command(name='help', aliases=['mhelp', 'comenzi', 'h'])
    async def help_cmd(ctx):
        await safe_delete(ctx.message)
        await ctx.send(embed=_help_embed(), delete_after=60)

    @bot.command()
    async def debug(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        vc = ctx.voice_client
        import psutil
        from music.config import DOWNLOAD_DIR
        ws = round(bot.latency * 1000, 1)
        v_lat = "N/A"
        if vc and vc.is_connected():
            raw = vc.latency
            v_lat = f"{round(raw*1000,1)}ms" if raw and raw != float('inf') else "..."
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        # cpu_percent(0.1) blocheaza bucla de evenimente 100ms; masuram in executor.
        cpu = await bot.loop.run_in_executor(None, lambda: proc.cpu_percent(0.1))
        dl = os.listdir(DOWNLOAD_DIR) if os.path.exists(DOWNLOAD_DIR) else []
        embed = discord.Embed(title="Debug", color=0x5865F2)
        embed.add_field(name="Latency", value=f"WS: `{ws}ms` · Voice: `{v_lat}`", inline=True)
        embed.add_field(name="System", value=f"CPU: `{cpu}%` · RAM: `{mem.rss/1024/1024:.0f}MB`", inline=True)
        embed.add_field(name="State", value=f"Coada: `{len(state.queue)}` · History: `{len(state.history)}` · Downloads: `{len(dl)}`", inline=False)
        embed.set_footer(text="Pentru ce se strica de fapt: !health")
        await ctx.send(embed=embed, delete_after=30)

    @bot.command()
    async def health(ctx):
        """Ce e in neregula, in vocabularul lucrurilor care chiar cad.

        !debug arata latenta, CPU si RAM — adica nimic din ce se strica. Aici e
        starea reala: cookies, PO Token, intrerupator, cota API, thread-uri de
        yt-dlp abandonate si ultima eroare bruta de la yt-dlp.
        """
        await safe_delete(ctx.message)
        snapshot = await diag.refresh(bot, guild_states, bot.loop)
        issues = diag.problems(snapshot)

        embed = discord.Embed(
            title="✅ Totul in regula" if not issues else "⚠️ Probleme detectate",
            color=0x57F287 if not issues else 0xFAA61A,
        )
        if issues:
            embed.description = "\n".join(f"• {line}" for line in issues[:8])

        cookies = snapshot['cookies']
        age = cookies['age_sec']
        age_text = 'necunoscut' if age is None else (
            f"{age // 3600}h" if age >= 3600 else f"{age // 60}m")
        embed.add_field(
            name="Acces YouTube",
            value=(f"Cookies: `{cookies['entries']}` intrari, scrise acum `{age_text}`\n"
                   f"PO Token: {'`ok`' if snapshot['pot_server']['ok'] else '`CAZUT`'}\n"
                   f"Data API: `{snapshot['data_api']['units_spent']}`/"
                   f"`{snapshot['data_api']['daily_cap']}` unitati azi"),
            inline=False)

        yt = snapshot['ytdlp']
        embed.add_field(
            name="Cereri",
            value=(f"Thread-uri abandonate: `{yt['leaked_workers']}`/`{yt['max_workers']}`\n"
                   f"Pauza de throttle: `{yt['throttle_sec_left']}s`"),
            inline=True)

        guild = snapshot['guilds_detail'].get(str(ctx.guild.id), {})
        embed.add_field(
            name="Sesiune",
            value=(f"Coada: `{guild.get('queue', 0)}` · "
                   f"Erori: `{guild.get('consecutive_errors', 0)}`\n"
                   f"Intrerupator: `{guild.get('breaker_sec_left', 0)}s` · "
                   f"Incarcare: `{guild.get('is_loading')}`"),
            inline=True)

        if guild.get('last_error'):
            embed.add_field(name="Ultima eroare yt-dlp",
                            value=f"```{guild['last_error'][:300]}```", inline=False)
        if guild.get('last_idle_reason'):
            embed.add_field(name="Ultimul tick de inactivitate",
                            value=f"`{guild['last_idle_reason']}`", inline=False)

        embed.set_footer(
            text=f"uptime {snapshot['uptime_sec'] // 60}m · "
                 f"yt-dlp {snapshot['versions']['yt_dlp']} · "
                 f"commit {snapshot['commit'] or '?'} · /status pentru JSON")
        await ctx.send(embed=embed, delete_after=120)
