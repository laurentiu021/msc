"""Toate comenzile muzicale."""
import discord
import yt_dlp
import asyncio
import os
import time
import random
from music.config import YDL_OPTS_SEARCH, log
from music.state import get_state
from music.utils import safe_delete, format_time, cleanup_file
from music.autoplay import prefill_autoplay_queue


def setup_music_commands(bot, process_play, play_next, update_player_ui, start_timeout, cancel_timeout):
    """Inregistreaza toate comenzile muzicale pe bot."""

    async def _resolve_platform_url(query: str) -> str:
        for platform in ['open.spotify.com/', 'spotify:', 'deezer.com/']:
            if platform in query:
                try:
                    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = await bot.loop.run_in_executor(
                            None, lambda: ydl.extract_info(query, download=False)
                        )
                        title = info.get('title', '')
                        artist = info.get('artist') or info.get('uploader', '')
                        if title:
                            search = f"{artist} {title}".strip() if artist else title
                            log.info(f"Platform resolved: {search}")
                            return f"ytsearch:{search}"
                except Exception as e:
                    log.debug(f"Platform resolve esuat: {e}")
                    parts = query.split('/')
                    return f"ytsearch:{parts[-1].split('?')[0].replace('-', ' ')}"
        return query

    @bot.command()
    async def play(ctx, *, search):
        await safe_delete(ctx.message)
        if not ctx.author.voice:
            return await ctx.send("Intra pe voce!", delete_after=5)
        vc = ctx.voice_client or await ctx.author.voice.channel.connect()
        state = get_state(ctx.guild.id)
        cancel_timeout(ctx)

        if any(p in search for p in ['spotify.com/', 'deezer.com/']):
            search = await _resolve_platform_url(search)

        if 'list=' in search and 'youtube.com' in search:
            ydl_opts_pl = YDL_OPTS_SEARCH.copy()
            ydl_opts_pl['extract_flat'] = True
            ydl_opts_pl['playlistend'] = 30
            ydl_opts_pl['noplaylist'] = False
            try:
                with yt_dlp.YoutubeDL(ydl_opts_pl) as ydl:
                    info = await bot.loop.run_in_executor(
                        None, lambda: ydl.extract_info(search, download=False)
                    )
                    entries = info.get('entries', [])
                    if not entries: raise ValueError("Playlist gol")
                    first = entries.pop(0)
                    for e in entries:
                        url = e.get('url') or e.get('id')
                        if url:
                            if not url.startswith('http'):
                                url = f"https://www.youtube.com/watch?v={url}"
                            state.queue.append({'query': url, 'title': e.get('title', 'Necunoscut')})
                    first_url = first.get('url') or first.get('id')
                    if first_url and not first_url.startswith('http'):
                        first_url = f"https://www.youtube.com/watch?v={first_url}"
                    if vc.is_playing() or vc.is_paused() or state.is_loading:
                        state.queue.insert(0, {'query': first_url, 'title': first.get('title', 'Necunoscut')})
                        await update_player_ui(ctx)
                    else:
                        await process_play(ctx, first_url)
            except Exception as e:
                log.error(f"Eroare playlist: {e}")
            return

        if vc.is_playing() or vc.is_paused() or state.is_loading:
            state.queue.append({'query': search, 'title': search})
            await update_player_ui(ctx)
        else:
            await process_play(ctx, search)

    @bot.command()
    async def stop(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        state.queue.clear(); state.autoplay = False; state.loop_mode = 0
        state.is_loading = False; state.always_on = False
        if state.preloaded:
            cleanup_file(state.preloaded.get('filename'), bot.loop)
            state.preloaded = None
        cancel_timeout(ctx)
        if ctx.voice_client: await ctx.voice_client.disconnect()
        await safe_delete(state.current_msg)
        state.current_msg = None

    @bot.command()
    async def skip(ctx):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        state.skip_request = True
        if ctx.voice_client and ctx.voice_client.is_playing(): ctx.voice_client.stop()

    @bot.command()
    async def nplay(ctx, *, search):
        await safe_delete(ctx.message)
        if not ctx.author.voice: return await ctx.send("Intra pe voce!", delete_after=5)
        vc = ctx.voice_client or await ctx.author.voice.channel.connect()
        state = get_state(ctx.guild.id)
        cancel_timeout(ctx)
        if any(p in search for p in ['spotify.com/', 'deezer.com/']):
            search = await _resolve_platform_url(search)
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
        await ctx.send(f"Scos: {removed['title'][:50]}", delete_after=5)
        await update_player_ui(ctx)

    @bot.command()
    async def move(ctx, from_idx: int, to_idx: int):
        await safe_delete(ctx.message)
        state = get_state(ctx.guild.id)
        if from_idx < 1 or from_idx > len(state.queue) or to_idx < 1 or to_idx > len(state.queue):
            return await ctx.send(f"Index invalid (1-{len(state.queue)}).", delete_after=5)
        item = state.queue.pop(from_idx - 1)
        state.queue.insert(to_idx - 1, item)
        await ctx.send(f"Mutat '{item['title'][:40]}' -> #{to_idx}.", delete_after=5)
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
        vc.stop()
        await asyncio.sleep(0.3)
        state.last_start_time = time.time() - seconds
        state.is_loading = False
        filename = state.current_file
        def after_play(err):
            if err: log.error(f"Eroare seek: {err}")
            play_next(ctx)
        seek_opts = {
            'before_options': f'-ss {seconds}',
            'options': '-vn -b:a 128k -ar 48000 -ac 2',
        }
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
            state.autoplay = True; state.loop_mode = 0
            state.show_queue = True
            if not state.queue and state.last_url:
                try: await prefill_autoplay_queue(state, bot.loop)
                except Exception: pass
            await ctx.send("24/7 ON - autoplay activat.", delete_after=5)
            await update_player_ui(ctx)
        else:
            state.autoplay = False
            state.queue.clear()
            state.show_queue = False
            await ctx.send("24/7 OFF.", delete_after=5)
            await update_player_ui(ctx)
            if ctx.voice_client and not ctx.voice_client.is_playing(): start_timeout(ctx)

    @bot.command(name='mhelp')
    async def help_cmd(ctx):
        await safe_delete(ctx.message)
        embed = discord.Embed(title="Comenzi Gogu", color=0x2b2d31)
        embed.description = (
            "**🎵 Muzică**\n"
            "`!play <piesa/url>` - Reda sau adauga in coada\n"
            "`!nplay <piesa/url>` - Inlocuieste piesa curenta\n"
            "`!stop` - Opreste si deconecteaza\n"
            "`!skip` - Piesa urmatoare\n"
            "`!seek <1:30>` - Salt la timestamp\n"
            "`!np` - Piesa curenta\n"
            "`!shuffle` / `!clear` / `!remove` / `!move`\n"
            "`!247` - 24/7 mode + autoplay"
        )
        embed.set_footer(text="YouTube · Spotify* · Deezer*")
        await ctx.send(embed=embed, delete_after=30)

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
        dl = os.listdir(DOWNLOAD_DIR) if os.path.exists(DOWNLOAD_DIR) else []
        embed = discord.Embed(title="Debug", color=0x5865F2)
        embed.add_field(name="Latency", value=f"WS: `{ws}ms` · Voice: `{v_lat}`", inline=True)
        embed.add_field(name="System", value=f"CPU: `{proc.cpu_percent(0.1)}%` · RAM: `{mem.rss/1024/1024:.0f}MB`", inline=True)
        embed.add_field(name="State", value=f"Coada: `{len(state.queue)}` · History: `{len(state.history)}` · Downloads: `{len(dl)}`", inline=False)
        await ctx.send(embed=embed, delete_after=30)
