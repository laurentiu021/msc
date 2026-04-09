"""MusicControlView - butoane si dropdown."""
import discord
from music.config import log
from music.state import get_state
from music.utils import safe_delete
from music.autoplay import prefill_autoplay_queue


class MusicControlView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        state = get_state(ctx.guild.id)
        vc = ctx.voice_client

        for child in self.children:
            if not isinstance(child, discord.ui.Button) or not child.custom_id:
                continue
            cid = child.custom_id
            if cid == "autoplay":
                if state.autoplay:
                    child.style = discord.ButtonStyle.success
                    child.label = "Autoplay ON"
                else:
                    child.style = discord.ButtonStyle.secondary
                    child.label = "Autoplay"
            elif cid == "loop":
                if state.loop_mode == 0:
                    child.label, child.style = "Loop", discord.ButtonStyle.secondary
                elif state.loop_mode == 1:
                    child.label, child.style = "Loop: Piesa", discord.ButtonStyle.primary
                else:
                    child.label, child.style = "Loop: Coada", discord.ButtonStyle.success
            elif cid == "queue":
                child.style = discord.ButtonStyle.primary if state.show_queue else discord.ButtonStyle.secondary
            elif cid == "playpause":
                if vc and vc.is_paused():
                    child.label, child.style = "Resume", discord.ButtonStyle.success
                else:
                    child.label, child.style = "Pause", discord.ButtonStyle.primary

        if state.show_queue and state.queue:
            options = []
            for i, item in enumerate(state.queue[:25]):
                options.append(discord.SelectOption(
                    label=f"{i+1}. {item['title'][:95]}",
                    value=str(i),
                ))
            select = discord.ui.Select(
                placeholder="Sari la o piesa...", options=options,
                custom_id="jump_select", row=2,
            )
            select.callback = self._jump_callback
            self.add_item(select)

    async def _jump_callback(self, interaction: discord.Interaction):
        state = get_state(self.ctx.guild.id)
        try:
            idx = int(interaction.data['values'][0])
            if idx < 0 or idx >= len(state.queue):
                await interaction.response.send_message("Piesa nu mai exista.", ephemeral=True, delete_after=3)
                return
            state.queue = state.queue[idx:]
            state.skip_request = True
            if self.ctx.voice_client and self.ctx.voice_client.is_playing():
                self.ctx.voice_client.stop()
            await self._safe_defer(interaction)
        except Exception as e:
            log.warning(f"Jump select error: {e}")
            await self._safe_defer(interaction)

    async def _safe_defer(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except (discord.HTTPException, discord.errors.DiscordServerError):
            pass

    @discord.ui.button(label="Inapoi", style=discord.ButtonStyle.secondary, custom_id="prev", row=0)
    async def back_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        if len(state.history) >= 2:
            state.history.pop()
            prev = state.history.pop()
            state.queue.insert(0, {'query': prev['url'], 'title': prev['title']})
            state.skip_request = True
            if self.ctx.voice_client and self.ctx.voice_client.is_playing():
                self.ctx.voice_client.stop()
            await self._safe_defer(interaction)
        else:
            try:
                await interaction.response.send_message("Nu exista o piesa anterioara.", ephemeral=True, delete_after=3)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="playpause", row=0)
    async def pause_resume_btn(self, interaction: discord.Interaction, button):
        vc = self.ctx.voice_client
        if vc:
            if vc.is_playing(): vc.pause()
            elif vc.is_paused(): vc.resume()
        await self._safe_defer(interaction)
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, custom_id="skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.skip_request = True
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
        await self._safe_defer(interaction)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.queue.clear()
        state.autoplay = False
        state.loop_mode = 0
        state.is_loading = False
        import music.player as _p
        _p.cancel_timeout(self.ctx)
        if self.ctx.voice_client:
            await self.ctx.voice_client.disconnect()
        await safe_delete(state.current_msg)
        state.current_msg = None
        await self._safe_defer(interaction)

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.secondary, custom_id="autoplay", row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.autoplay = not state.autoplay
        if state.autoplay:
            state.loop_mode = 0
            state.show_queue = True
            if not state.queue and state.last_url:
                try:
                    await prefill_autoplay_queue(state, self.ctx.bot.loop)
                except Exception as e:
                    log.warning(f"Prefill esuat: {e}")
        else:
            state.queue.clear()
            state.show_queue = False
        await self._safe_defer(interaction)
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, custom_id="loop", row=1)
    async def loop_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.loop_mode = (state.loop_mode + 1) % 3
        if state.loop_mode > 0: state.autoplay = False
        await self._safe_defer(interaction)
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)

    @discord.ui.button(label="Coada", style=discord.ButtonStyle.secondary, custom_id="queue", row=1)
    async def queue_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.show_queue = not state.show_queue
        await self._safe_defer(interaction)
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)
