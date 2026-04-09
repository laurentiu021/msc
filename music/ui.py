"""UI: embed player si MusicControlView."""
import discord
from music.config import log
from music.state import get_state
from music.utils import format_time, safe_delete


def _format_number(n: int) -> str:
    """1234567 -> '1.2M', 12345 -> '12.3K'"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


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
        end_time = int(state.last_start_time + state.last_duration)
        info_parts.append(f"<t:{end_time}:R>")
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
    footer.append("!mhelp")
    embed.set_footer(text=" · ".join(footer))

    # Queue field
    if state.show_queue and state.queue:
        q_lines = []
        for i, item in enumerate(state.queue[:8]):
            q_lines.append(f"`{i+1}.` {item['title'][:45]}")
        q_text = "\n".join(q_lines)
        if len(state.queue) > 8:
            q_text += f"\n*+{len(state.queue)-8} mai multe*"
        embed.add_field(name="In coada", value=q_text, inline=False)
    elif state.show_queue:
        embed.add_field(name="In coada", value="*Goala*", inline=False)

    from music.views import MusicControlView
    view = MusicControlView(ctx)

    if send_new:
        await safe_delete(state.current_msg)
        state.current_msg = await ctx.send(embed=embed, view=view)
    elif state.current_msg:
        try:
            await state.current_msg.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass
