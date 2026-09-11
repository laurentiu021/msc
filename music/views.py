"""MusicControlView - butoane si dropdown."""
import time

import discord
from music.config import log
from music.state import (get_state, loading, mark_paused, mark_resumed,
                         set_autoplay)
from music.utils import DISCORD_ERRORS, safe_delete, item_title
from music.autoplay import prefill_autoplay_queue


class MusicControlView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Doar cine e in acelasi canal de voce poate comanda redarea.

        Inainte, orice membru al serverului putea apasa Stop pe sesiunea
        altcuiva, din orice canal.
        """
        vc = self.ctx.voice_client
        if not vc or not vc.channel:
            return True
        author_voice = getattr(interaction.user, 'voice', None)
        if author_voice and author_voice.channel == vc.channel:
            return True
        try:
            await interaction.response.send_message(
                "Trebuie sa fii in canalul de voce al botului.", ephemeral=True)
        except discord.HTTPException:
            pass
        return False

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

        options = []
        if state.show_queue and state.queue:
            # Valorile unui select trebuie sa fie UNICE si nevide. Aceeasi piesa
            # apare in coada mai des decat pare (loop pe coada, autoplay care
            # re-propune, acelasi link dat de doi oameni), iar Discord refuza
            # atunci tot componentul cu 400 — deci edit-ul panoului eșua si
            # `except DISCORD_ERRORS: log.debug(...)` il inghitea: panoul incepea
            # sa arate piesa veche pentru totdeauna, fara nicio urma la INFO.
            #
            # Deduplicarea e si corecta semantic: `_jump_callback` sare oricum la
            # PRIMA intrare cu valoarea aceea.
            seen = set()
            for i, item in enumerate(state.queue[:25]):
                # Valoarea e interogarea, nu poziția: coada se poate schimba intre
                # randare si click (refill de autoplay, skip, remove) si un index
                # pozitional ar sari la alta piesa.
                value = str(item.get('query') or '')[:100]
                if not value or value in seen:
                    continue
                seen.add(value)
                options.append(discord.SelectOption(
                    label=f"{i+1}. {item_title(item, 95)}",
                    value=value,
                ))
        if options:
            select = discord.ui.Select(
                placeholder="Sari la o piesa...", options=options,
                custom_id="jump_select", row=2,
            )
            select.callback = self._jump_callback
            self.add_item(select)

    async def _jump_callback(self, interaction: discord.Interaction):
        state = get_state(self.ctx.guild.id)
        try:
            wanted = interaction.data['values'][0]
            idx = next((i for i, it in enumerate(state.queue)
                        if str(it.get('query', ''))[:100] == wanted), None)
            if idx is None:
                await interaction.response.send_message(
                    "Piesa nu mai e in coada.", ephemeral=True, delete_after=3)
                return
            # ROTIRE, nu tăiere. `queue[idx:]` arunca tot ce era inaintea piesei
            # alese, deci fiecare alegere micșora coada — iar lista de sub panou
            # ESTE coada, deci exact gestul de "vreau sa aleg" iți lua opțiunile
            # din care sa alegi. Acum piesele sărite trec la sfarșit.
            state.queue = state.queue[idx:] + state.queue[:idx]
            await self._safe_defer(interaction)
            await self._advance(state)
        except Exception as e:
            log.warning(f"Jump select error: {e}", exc_info=True)
            await self._safe_defer(interaction)

    async def _advance(self, state) -> str:
        """Trece la capul cozii, din orice stare a redarii.

        `vc.stop()` singur nu ajunge: cand nimic nu iese pe voce (pauza deja
        oprita, o rezolvare care s-a incheiat fara sa porneasca, coada care
        aȘteapta) nu exista niciun `after_play` care sa avanseze, deci butonul
        arata ca a functionat si nu se intampla nimic. `resume_if_idle` e exact
        raspunsul, si spune si ce a decis.
        """
        import music.player as _p
        vc = self.ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state.skip_request = True
            vc.stop()
            return 'oprit ca sa avanseze'
        decision = _p.resume_if_idle(self.ctx)
        log.info(f"Buton pe idle: {decision}")
        return decision

    async def _safe_defer(self, interaction: discord.Interaction):
        """Confirma interactiunea. Un eșec de aici se VEDE.

        `DISCORD_ERRORS`, nu doar `HTTPException`: discord.py nu invelește
        eșecurile de transport, iar `DiscordServerError` e oricum subclasa de
        `HTTPException`, deci vechea pereche acoperea o singura clasa reala.
        `InteractionResponded` intra si el: e un `ClientException`, adica un semn
        ca am raspuns deja de doua ori — un defect al nostru, nu o pana.

        Si mai important, nu mai tace: cand confirmarea eșua, utilizatorul vedea
        "Gogu didn't respond in time" si in loguri nu exista absolut nimic.
        """
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            log.warning("Interactiunea era deja confirmata: %s",
                        (interaction.data or {}).get('custom_id'))
        except DISCORD_ERRORS as e:
            log.warning("Nu am putut confirma interactiunea %s: %s",
                        (interaction.data or {}).get('custom_id'), e)

    @discord.ui.button(label="Inapoi", style=discord.ButtonStyle.secondary, custom_id="prev", row=0)
    async def back_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        if len(state.history) >= 2:
            # Ambele intrari se SCOT, si asta e deliberat: history e stiva pe care
            # merge butonul, deci daca `prev` ar rămâne in ea a doua apasare s-ar
            # intoarce la aceeasi piesa la infinit.
            #
            # Consecinta care era un defect — calea de cache nu mai gasea metadata
            # piesei, fiindca o cauta in exact lista de aici — nu mai exista:
            # metadatele stau acum in fisierul insoțitor de langa audio
            # (utils.write_track_meta), deci un hit de cache nu depinde de history.
            state.history.pop()
            prev = state.history.pop()
            if not prev.get('url'):
                # O intrare fara URL ar deveni o cerere goala in coada, respinsa
                # apoi ca "nu ai scris nimic" — dupa ce history a fost deja golit.
                log.warning("Intrarea de history nu are URL; nu pot merge inapoi")
                await self._safe_defer(interaction)
                return
            state.queue.insert(0, {'query': prev['url'], 'title': prev['title']})
            await self._safe_defer(interaction)
            # `_advance`, nu doar `is_playing()`: butonul verifica DOAR redarea
            # activa, deci apasat in PAUZA punea piesa anterioara in coada si nu
            # pornea nimic — singurul buton al panoului care ignora pauza.
            await self._advance(state)
        else:
            try:
                await interaction.response.send_message("Nu exista o piesa anterioara.", ephemeral=True, delete_after=3)
            except DISCORD_ERRORS as e:
                log.warning(f"Nu am putut raspunde la Inapoi: {e}")

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="playpause", row=0)
    async def pause_resume_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        vc = self.ctx.voice_client
        if vc:
            if vc.is_playing():
                vc.pause()
                mark_paused(state, time.time())
            elif vc.is_paused():
                vc.resume()
                mark_resumed(state, time.time())
        await self._safe_defer(interaction)
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, custom_id="skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        await self._safe_defer(interaction)
        # Cand nimic nu cânta, versiunea de dinainte doar confirma interactiunea si
        # ieșea: butonul se aprindea si coada rămânea pe loc.
        await self._advance(state)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button):
        await self._safe_defer(interaction)
        state = get_state(self.ctx.guild.id)
        state.queue.clear()
        set_autoplay(state, False, by_user=True)
        state.loop_mode = 0
        state.is_loading = False
        state.always_on = False
        import music.player as _p
        _p.bump_play_generation(state)
        _p.cancel_timeout(self.ctx)
        # Butonul de stop lasa fisierul curent pe disc, spre deosebire de
        # !stop: fiecare oprire din panou pierdea un fisier audio pana la
        # repornirea containerului.
        # Fisierul rămâne in cache pentru urmatoarea redare; doar il eliberam
        # din sesiune si lasam evacuarea pe marime sa decida.
        state.current_file = None
        _p.trim_cache()
        if self.ctx.voice_client:
            await self.ctx.voice_client.disconnect()
        from music.ui import forget_panel
        await forget_panel(state)

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.secondary, custom_id="autoplay", row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button):
        # Ack-ul primul: Discord da drumul la doar 3 secunde, iar prefill-ul de
        # mai jos face cereri de retea care pot depasi usor acest buget.
        await self._safe_defer(interaction)
        state = get_state(self.ctx.guild.id)
        set_autoplay(state, not state.autoplay, by_user=True)
        if state.autoplay:
            state.loop_mode = 0
            state.show_queue = True
            # `is_loading` conteaza: prefill-ul trage un Mix de pana la 50 de
            # intrari cu cookies, iar butonul nu se uita la nimic. Apasat in
            # timpul unei incarcari, dubla cererile pe un IP deja limitat. Cand
            # e ocupat, refill-ul normal din _play_next_async o face oricum.
            if not state.queue and state.last_url and not state.is_loading:
                try:
                    with loading(state):
                        await prefill_autoplay_queue(state, self.ctx.bot.loop)
                except Exception as e:
                    log.warning(f"Prefill esuat: {e}", exc_info=True)
                # Prefill-ul a tinut `is_loading`, dar nu porneste nicio redare.
                # Un `!play` intrat in fereastra aceea a fost pus in coada crezand
                # ca incarcarea in curs o va scurge — deci trebuie sa o scurgem noi.
                import music.player as _p
                log.info(f"Autoplay dupa prefill: {_p.resume_if_idle(self.ctx)}")
        else:
            state.show_queue = False
        from music.ui import update_player_ui
        await update_player_ui(self.ctx)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, custom_id="loop", row=1)
    async def loop_btn(self, interaction: discord.Interaction, button):
        state = get_state(self.ctx.guild.id)
        state.loop_mode = (state.loop_mode + 1) % 3
        if state.loop_mode > 0: set_autoplay(state, False, by_user=True)
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
