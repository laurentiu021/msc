"""UI: embed player si MusicControlView."""
import time

import discord

from music.config import log
from music.state import get_state
from music.utils import (DISCORD_ERRORS, format_time, item_title,
                         playback_remaining, safe_delete)


def _format_number(n: int) -> str:
    """1234567 -> '1.2M', 12345 -> '12.3K'"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _stop_view(state):
    """Opreste view-ul curent inainte sa fie inlocuit.

    Message.delete() nu il scoate din ViewStore-ul lui discord.py, deci un view
    neoprit rămâne sa asculte interactiuni pe viata procesului.
    """
    if state.current_view is not None:
        try:
            state.current_view.stop()
        except Exception:
            log.debug("View-ul vechi nu a putut fi oprit", exc_info=True)
        state.current_view = None


async def update_player_ui(ctx, send_new=False):
    state = get_state(ctx.guild.id)
    vc = ctx.voice_client

    if vc and vc.is_paused():
        color = 0xfaa61a  # orange = paused
    elif state.autoplay:
        color = 0x57f287  # green = autoplay
    else:
        color = 0x5865f2  # blurple = normal

    embed = discord.Embed(color=color)

    # Author line with title
    embed.set_author(
        name=state.last_title[:60],
        url=state.last_url,
        icon_url="https://cdn.discordapp.com/emojis/1041054455174258689.gif"
    )

    # Description: duration, channel, stats
    lines = []

    # Line 1: channel + duration
    info_parts = []
    if state.last_channel:
        info_parts.append(f"**{state.last_channel}**")
    if state.last_duration > 0:
        info_parts.append(f"`{format_time(state.last_duration)}`")
        _, remaining = playback_remaining(
            time.time(), state.last_start_time, state.last_duration,
            state.paused_at)
        if state.paused_at:
            # Pauzat: text static. Un <t:...:R> continua sa numere in client
            # oricum, deci ar minti exact cat timp e pauzat.
            info_parts.append(f"`rămas {format_time(int(remaining))}`")
        else:
            info_parts.append(f"<t:{int(time.time() + remaining)}:R>")
    if info_parts:
        lines.append(" · ".join(info_parts))

    # Line 2: views + likes (if available from API)
    stat_parts = []
    if state.last_views:
        stat_parts.append(f"👁 {_format_number(state.last_views)}")
    if state.last_likes:
        stat_parts.append(f"👍 {_format_number(state.last_likes)}")
    if stat_parts:
        lines.append(" · ".join(stat_parts))

    # Line 3: tags
    tags = []
    if state.autoplay:
        tags.append("`🔀 Autoplay`")
    if state.always_on:
        tags.append("`📡 24/7`")
    if state.loop_mode == 1:
        tags.append("`🔂 Loop`")
    elif state.loop_mode == 2:
        tags.append("`🔁 Loop All`")
    if vc and vc.is_paused():
        tags.append("`⏸ Paused`")
    if tags:
        lines.append(" ".join(tags))

    if lines:
        embed.description = "\n".join(lines)

    # Thumbnail (HD from API if available)
    if state.last_thumbnail:
        thumb = state.last_thumbnail
        if 'ytimg.com' in thumb and '/default.' in thumb:
            thumb = thumb.replace('/default.', '/maxresdefault.')
        embed.set_thumbnail(url=thumb)

    # Footer
    footer = []
    if state.queue:
        footer.append(f"♫ {len(state.queue)} in coada")
    footer.append("!help")
    embed.set_footer(text=" · ".join(footer))

    # Queue field
    if state.show_queue and state.queue:
        q_lines = []
        for i, item in enumerate(state.queue[:8]):
            q_lines.append(f"`{i+1}.` {item_title(item, 45)}")
        q_text = "\n".join(q_lines)
        if len(state.queue) > 8:
            q_text += f"\n*+{len(state.queue)-8} mai multe*"
        embed.add_field(name="In coada", value=q_text, inline=False)
    elif state.show_queue:
        embed.add_field(name="In coada", value="*Goala*", inline=False)

    from music.views import MusicControlView
    view = MusicControlView(ctx)

    if not send_new and state.current_msg is None:
        # Panoul nu mai exista: sters de !stop, inlocuit de mesajul de plecare,
        # sau anulat dupa un 403. Inainte, ramura de edit era `elif
        # state.current_msg`, deci un !play care doar adauga in coada nu producea
        # NIMIC vizibil: nici panou, nici mesaj, nici eroare.
        send_new = True

    if send_new:
        if state._ui_sending:
            # O trimitere e deja in zbor. Fara garda, doua actualizari
            # concurente ar lasa doua panouri, fiecare cu butoane vii.
            return
        state._ui_sending = True
        try:
            _stop_view(state)
            # Stergerea intra IN try. Sta intre ridicarea gardului si `finally`,
            # iar `safe_delete` nu putea acoperi tot: cand o eroare de transport
            # scapa de acolo, `_ui_sending` rămânea True pe viata procesului. E
            # scris in exact trei locuri (state.py, aici, si finally-ul de mai
            # jos) si nimic nu il mai stingea, deci de atunci fiecare trimitere
            # ieșea imediat — iar cum lipsa panoului forteaza `send_new=True`,
            # panoul nu mai putea apărea niciodata.
            await safe_delete(state.current_msg)
            state.current_msg = await ctx.send(embed=embed, view=view)
            state.current_view = view
        except DISCORD_ERRORS as e:
            # Ramura asta nu avea niciun guard, desi cea de edit avea. Un 403
            # dupa o schimbare de permisiuni ridica excepția din interiorul
            # try-ului de redare din process_play, care apoi sterge fisierul pe
            # care FFmpeg il streameaza si avanseaza coada.
            log.warning(f"Nu am putut trimite player-ul: {e}")
            state.current_msg = None
            state.current_view = None
        finally:
            state._ui_sending = False
    else:
        try:
            await state.current_msg.edit(embed=embed, view=view)
            # Si aici, nu doar la trimitere: view-ul vechi rămânea inregistrat in
            # ViewStore-ul lui discord.py, deci fiecare piesa lasa un view viu.
            _stop_view(state)
            state.current_view = view
        except discord.NotFound:
            # Mesajul nu mai exista, deci NU mai avem panou. Inainte era inghitit
            # ca orice alta eroare HTTP si `current_msg` rămânea sa arate spre el,
            # iar auto-vindecarea de mai sus se uita doar la None — deci fiecare
            # refresh urmator era un no-op tacut. Botul producea exact starea asta
            # singur: mesajul de plecare, trimis cu delete_after=15, era pastrat
            # ca panou si dispărea 15 secunde mai tarziu.
            log.info("Panoul nu mai exista pe Discord; il uit ca sa fie retrimis")
            _stop_view(state)
            state.current_msg = None
            state.current_view = None
        except DISCORD_ERRORS as e:
            log.debug(f"Nu am putut actualiza panoul: {e}")
