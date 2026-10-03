"""Buttons and pop-up forms used across the bot.

* ActionView  - a row of action buttons under a card (+ optional 🔄 Refresh)
* ItemPager   - one record per page with ◀ ▶ and action buttons for the record on screen
* open_form   - a pop-up form (Discord "modal") that asks for text, then runs a function

Every button re-checks permissions when pressed (inside the command it runs), only the person
who opened the screen can use it, and money-moving buttons go through the normal confirmation screen.
"""
from __future__ import annotations

import logging

import discord

from . import alerts as A

log = logging.getLogger("tunbank.buttons")

STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
          "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
FRIENDLY_ERROR = "Sorry, that button didn't work. Nothing was changed. Please try the command instead."


async def _safe(interaction: discord.Interaction, coro):
    try:
        await coro
    except Exception:  # noqa: BLE001
        log.exception("button action failed")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(FRIENDLY_ERROR, ephemeral=True)
            else:
                await interaction.response.send_message(FRIENDLY_ERROR, ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- forms
class ModalForm(discord.ui.Modal):
    def __init__(self, title: str, fields: list, callback):
        super().__init__(title=title[:45])
        self._callback = callback
        for f in fields:
            self.add_item(discord.ui.TextInput(
                label=f["label"][:45], placeholder=(f.get("placeholder") or "")[:100],
                required=f.get("required", True), max_length=f.get("max", 400),
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short))

    async def on_submit(self, interaction: discord.Interaction):
        values = [str(c.value or "").strip() for c in self.children]
        await _safe(interaction, self._callback(interaction, *values))


async def open_form(interaction: discord.Interaction, title: str, fields: list, callback):
    """Show a pop-up form. `callback(interaction, *values)` runs when it is submitted."""
    await interaction.response.send_modal(ModalForm(title, fields, callback))


# -------------------------------------------------------------- buttons
class ActionView(discord.ui.View):
    """actions: [(label, emoji, style, async callback(interaction))]"""

    def __init__(self, user_id: int, actions: list, refresh=None, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.refresh_builder = refresh
        self.handlers: dict = {}
        for label, emoji, style, cb in actions:
            self._add(label, emoji, style, cb)
        if refresh is not None:
            self._add("Refresh", "🔄", "secondary", self._refresh)

    def _add(self, label, emoji, style, cb):
        button = discord.ui.Button(label=label, emoji=emoji, style=STYLES.get(style, STYLES["secondary"]))

        async def handler(interaction: discord.Interaction, cb=cb):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            await _safe(interaction, cb(interaction))
        button.callback = handler
        self.handlers[label] = handler
        self.add_item(button)

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        card = await self.refresh_builder()
        if card is not None:
            await interaction.edit_original_response(embed=A.to_embed(card), view=self)


class ItemPager(discord.ui.View):
    """Shows ONE record at a time. item_actions: [(label, emoji, style, async cb(interaction, item))]"""

    def __init__(self, user_id: int, items: list, render, item_actions: list = (), timeout: float = 900):
        super().__init__(timeout=timeout)
        self.user_id, self.items, self.render = user_id, items, render
        self.index = 0
        self.handlers: dict = {}
        self._nav = []
        if len(items) > 1:
            self._nav.append(self._add("Previous", "◀️", "secondary", self._prev, nav=True))
            self._nav.append(self._add("Next", "▶️", "secondary", self._next, nav=True))
        for label, emoji, style, cb in item_actions:
            self._add(label, emoji, style, cb)
        self._sync()

    def card(self) -> A.Card:
        c = self.render(self.items[self.index], self.index, len(self.items))
        c.footer = f"{self.index + 1} of {len(self.items)} · TUN Bank"
        return c

    def _add(self, label, emoji, style, cb, nav=False):
        button = discord.ui.Button(label=label, emoji=emoji, style=STYLES.get(style, STYLES["secondary"]))

        async def handler(interaction: discord.Interaction, cb=cb):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            if nav:
                await _safe(interaction, cb(interaction))
            else:
                await _safe(interaction, cb(interaction, self.items[self.index]))
        button.callback = handler
        self.handlers[label] = handler
        self.add_item(button)
        return button

    def _sync(self):
        if len(self._nav) == 2:
            self._nav[0].disabled = self.index <= 0
            self._nav[1].disabled = self.index >= len(self.items) - 1

    async def _step(self, interaction, delta):
        self.index = max(0, min(len(self.items) - 1, self.index + delta))
        self._sync()
        await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)

    async def _prev(self, interaction):
        await self._step(interaction, -1)

    async def _next(self, interaction):
        await self._step(interaction, +1)
