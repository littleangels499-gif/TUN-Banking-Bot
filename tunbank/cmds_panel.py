"""The permanent TUN Bank panel: one message with buttons, so members don't need to remember commands.

The buttons contain NO banking logic. Each one hands over to the same service the slash commands use
(withdrawals, deposits from the member's own key, offshore, limits, valuation, ledger, audit), registered in svc.actions.
Every press is checked again on the server; a button can never do more than its command could.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import conversion as CV
from . import fmt
from . import icons
from . import ledger as L
from . import money as M
from .buttons import _safe, open_form
from .config import cfg_bool, cfg_get, cfg_set
from .ui import Services, need, reply, thinking

log = logging.getLogger("tunbank.panel")

NOT_LINKED = "You have not linked your nation yet. Use `/nation link` first (your nation id, link or name)."
IDS = {"withdraw": "tunbank:panel:withdraw", "send": "tunbank:panel:send", "account": "tunbank:panel:account",
       "deposit": "tunbank:panel:deposit", "excess": "tunbank:panel:excess", "offshore": "tunbank:panel:offshore"}


def panel_card() -> A.Card:
    c = A.Card("🏦 TUN Bank",
               "Manage your TUN Bank account, deposits and withdrawals from here.\n"
               "**Balance  •  Deposits  •  Withdrawals**", A.BLUE)
    c.add("💸 Withdraw to Me", "Take your available funds to your own nation.", True)
    c.add("📤 Send Funds", "Send some of your available funds to another nation.", True)
    c.add("📥 Deposit Funds", "Deposit from your nation straight from Discord.", True)
    c.add("♻️ Deposit Excess", "Deposit everything above the alliance's holding limits.", True)
    c.add("🏝️ Offshore Funds", "Move alliance funds from the main bank to the offshore bank.", True)
    c.add("🏦 My Account", "Your balance, locked funds and recent activity.", True)
    c.footer = "Every action asks you to confirm first · Nothing is credited until PnW shows the real transaction · TUN Bank"
    return c


# ------------------------------------------------------------------ resource + details flow
class PickView(discord.ui.View):
    """Private screen: choose a resource, then press the button to fill in the details."""

    def __init__(self, user_id: int, *, title: str, options: list, fields: list, on_submit, button_label="Enter details"):
        super().__init__(timeout=600)
        self.user_id, self.title, self.fields, self.on_submit = user_id, title, fields, on_submit
        self.res: str | None = None
        self.select = discord.ui.Select(placeholder="1 · Choose a resource", min_values=1, max_values=1,
                                        options=[discord.SelectOption(label=label[:100], value=value) for value, label in options][:25])
        self.select.callback = self._picked
        self.go = discord.ui.Button(label=f"2 · {button_label}", emoji="✏️", style=discord.ButtonStyle.primary)
        self.go.callback = self._open
        self.add_item(self.select)
        self.add_item(self.go)

    def card(self) -> A.Card:
        c = A.Card(self.title, "Choose a resource, then press the button to enter the details. Nothing happens until you confirm.", A.BLUE)
        c.add("Resource", M.LABELS[self.res] if self.res else "— not chosen yet —")
        return c

    async def _mine(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction):
        if await self._mine(interaction):
            self.res = self.select.values[0]
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)

    async def _open(self, interaction):
        if not await self._mine(interaction):
            return
        if not self.res:
            return await interaction.response.send_message("Choose a resource first.", ephemeral=True)
        res = self.res

        async def submitted(i2, *values):
            await thinking(i2)
            await self.on_submit(i2, res, *values)
        await open_form(interaction, f"{self.title} · {M.LABELS[res]}", self.fields, submitted)


def _parse(res: str, text: str, available: int | None = None) -> dict:
    """'5m' / '1,000' / 'all' -> {res: units}. Raises LedgerError with a message fit for the member."""
    units = CV.parse_amount(text, res, available or 0)
    if units <= 0:
        raise L.LedgerError("The amount must be more than zero.")
    return {res: units}


def register(bankset: app_commands.Group, svc: Services):
    acts = svc.actions

    def member(interaction):
        with svc.db.read() as conn:
            return L.member_by_discord(conn, interaction.user.id)

    def spendable(nation_id):
        with svc.db.read() as conn:
            return {r: v for r, v in L.spendable(conn, nation_id).items() if v > 0}

    # ---------------------------------------------------------------- Withdraw to Me / Send Funds
    async def b_withdraw(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        free = spendable(m["nation_id"])
        if not free:
            return await interaction.response.send_message(
                "You have no available funds to withdraw. (Locked funds can't be withdrawn; ask ECON.)", ephemeral=True)

        async def submit(i, res, amount, note):
            try:
                parsed = _parse(res, amount, spendable(m["nation_id"]).get(res, 0))
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_withdraw"](i, parsed, note)
        view = PickView(interaction.user.id, title="💸 Withdraw to me", button_label="Enter amount",
                        options=[(r, f"{M.LABELS[r]} · {M.fmt_units(r, v)} available") for r, v in free.items()],
                        fields=[dict(label="Amount", placeholder="e.g. 5m, 1,000,000 or all", max=30),
                                dict(label="Note (optional)", required=False, max=100)], on_submit=submit)
        await interaction.response.send_message(embed=A.to_embed(view.card()), view=view, ephemeral=True)

    async def b_send(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        with svc.db.read() as conn:
            if not cfg_bool(conn, "member_send_enabled"):
                return await interaction.response.send_message("Sending funds to other nations is switched off by ECON right now.", ephemeral=True)
        free = spendable(m["nation_id"])
        if not free:
            return await interaction.response.send_message("You have no available funds to send. (Locked funds can't be sent.)", ephemeral=True)

        async def submit(i, res, recipient, amount, note):
            try:
                parsed = _parse(res, amount, spendable(m["nation_id"]).get(res, 0))
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_send"](i, recipient, parsed, note)
        view = PickView(interaction.user.id, title="📤 Send funds", button_label="Recipient & amount",
                        options=[(r, f"{M.LABELS[r]} · {M.fmt_units(r, v)} available") for r, v in free.items()],
                        fields=[dict(label="Recipient nation", placeholder="nation id, link or exact name", max=100),
                                dict(label="Amount", placeholder="e.g. 5m, 1,000,000 or all", max=30),
                                dict(label="Note (optional)", required=False, max=100)], on_submit=submit)
        await interaction.response.send_message(embed=A.to_embed(view.card()), view=view, ephemeral=True)

    # ---------------------------------------------------------------- Deposit Funds
    async def b_deposit(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        with svc.db.read() as conn:
            usable = bool(svc.deposits and svc.deposits.usable_for(conn, m["nation_id"], m["discord_id"]))
        if not usable:                                     # clear message about exactly what to set up
            await thinking(interaction)
            return await acts["member_api_help"](interaction)

        async def submit(i, res, amount):
            try:
                parsed = _parse(res, amount)
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_deposit"](i, parsed)
        view = PickView(interaction.user.id, title="📥 Deposit funds", button_label="Enter amount",
                        options=[(r, M.LABELS[r]) for r in M.RESOURCES],
                        fields=[dict(label="Amount to deposit", placeholder="e.g. 5m or 250,000", max=30)], on_submit=submit)
        await interaction.response.send_message(embed=A.to_embed(view.card()), view=view, ephemeral=True)

    # ---------------------------------------------------------------- the persistent panel
    class PanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)               # never expires; re-registered when the bot starts
            S = discord.ButtonStyle
            for key, label, emoji, style, row, handler in (
                    ("withdraw", "Withdraw to Me", "💸", S.primary, 0, b_withdraw),
                    ("send", "Send Funds", "📤", S.secondary, 0, b_send),
                    ("account", "My Account", "🏦", S.secondary, 0, self_dashboard),
                    ("deposit", "Deposit Funds", "📥", S.success, 1, b_deposit),
                    ("excess", "Deposit Excess", "♻️", S.success, 1, self_excess),
                    ("offshore", "Offshore Funds", "🏝️", S.secondary, 2, self_offshore)):
                b = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=IDS[key], row=row)
                b.callback = self._wrap(label, handler)
                self.add_item(b)

        @staticmethod
        def _wrap(label, handler):
            async def cb(interaction: discord.Interaction):
                from . import configaudit as CA
                CA.ACTION.set(f"panel button: {label}")
                await _safe(interaction, handler(interaction))
            return cb

    async def self_dashboard(interaction):
        await acts["member_dashboard"](interaction)

    async def self_excess(interaction):
        if not member(interaction):
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        await acts["member_excess"](interaction)

    async def self_offshore(interaction):
        await acts["offshore_move"](interaction)

    svc.panel_view = PanelView                           # the bot registers one instance so the buttons survive restarts

    # ---------------------------------------------------------------- admin commands
    @bankset.command(name="panel", description="Admin: post (or re-post) the TUN Bank panel in the banking channel")
    @app_commands.describe(channel="Where to post it (default: the saved banking-panel channel, else this channel)")
    async def panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            saved = cfg_get(conn, "panel_channel_id")
            old_msg = cfg_get(conn, "panel_message_id")
        target = channel
        if target is None and saved.strip().isdigit():
            target = interaction.client.get_channel(int(saved)) if getattr(interaction, "client", None) else None
        target = target or interaction.channel
        old = None
        if old_msg.strip().isdigit() and saved.strip().isdigit():          # re-post: remove the previous panel
            try:
                old_ch = interaction.client.get_channel(int(saved)) or await interaction.client.fetch_channel(int(saved))
                old = await old_ch.fetch_message(int(old_msg))
            except (discord.HTTPException, AttributeError):
                old = None
        try:
            msg = await target.send(embed=A.to_embed(panel_card()), view=PanelView())
        except (discord.HTTPException, AttributeError):
            return await reply(interaction, "I couldn't post there. Check that I can send messages and embeds in that channel.")
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

        def save():
            with svc.db.tx() as conn:
                cfg_set(conn, "panel_channel_id", str(target.id), str(interaction.user.id))
                cfg_set(conn, "panel_message_id", str(msg.id), str(interaction.user.id))
        await asyncio.to_thread(save)
        await reply(interaction, f"The TUN Bank panel is posted in {getattr(target, 'mention', 'this channel')}.")

    @bankset.command(name="excess", description="Admin: set (or view) the most a nation should keep; anything above is 'excess'")
    @app_commands.describe(limits="e.g. money=50m food=250k coal=10k. Leave empty to view. Type 'none' to clear.")
    async def excess(interaction: discord.Interaction, limits: str = ""):
        if not await need(svc, interaction, "MINISTER" if not limits.strip() else "ADMIN"):
            return
        await thinking(interaction)
        if not limits.strip():
            with svc.db.read() as conn:
                cur = (cfg_get(conn, "excess_holdings") or "").strip()
            if not cur:
                return await reply(interaction, "No excess-holdings limits are set yet. Use `/bankset excess limits:money=50m food=250k coal=10k`.")
            parsed = M.parse_amounts(cur)
            return await reply(interaction, "Members keep at most: " + fmt.amount_lines(parsed).replace("\n", " · ")
                                            + "\nAnything above is offered for deposit by the **Deposit Excess** button.")
        text = "" if limits.strip().lower() in ("none", "clear", "off") else limits.strip()
        if text:
            try:
                M.parse_amounts(text)
            except M.AmountError as exc:
                return await reply(interaction, f"I couldn't read those limits: {exc}")

        def save():
            with svc.db.tx() as conn:
                cfg_set(conn, "excess_holdings", text, str(interaction.user.id))
        await asyncio.to_thread(save)
        await reply(interaction, "Excess-holdings limits saved." if text else "Excess-holdings limits cleared.")
