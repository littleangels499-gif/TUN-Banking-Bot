"""The permanent TUN Bank panel: one message with buttons, so members don't need to remember commands.

The buttons contain NO banking logic. Each one hands over to the same service the slash commands use
(withdrawals, deposits from the member's own key, offshore, limits, valuation, ledger, audit), registered in svc.actions.
Every press is checked again on the server; a button can never do more than its command could.
"""
from __future__ import annotations

import asyncio
import logging
import re
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


# ------------------------------------------------------------------ amounts: several resources in ONE request
EXAMPLE = CV.EXAMPLE
parse_multi = CV.parse_multi


def register(bankset: app_commands.Group, svc: Services):
    acts = svc.actions

    def member(interaction):
        with svc.db.read() as conn:
            return L.member_by_discord(conn, interaction.user.id)

    def spendable(nation_id):
        with svc.db.read() as conn:
            return {r: v for r, v in L.spendable(conn, nation_id).items() if v > 0}

    # ---------------------------------------------------------------- Withdraw to Me / Send Funds / Deposit Funds
    AMOUNTS = dict(label="What (resource=amount, any number)", placeholder=EXAMPLE + "   (food=all works too)", max=400, long=True)

    async def b_withdraw(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        if not spendable(m["nation_id"]):
            return await interaction.response.send_message(
                "You have no available funds to withdraw. (Locked funds can't be withdrawn; ask ECON.)", ephemeral=True)

        async def submit(i, amounts, note):
            try:
                parsed = parse_multi(amounts, spendable(m["nation_id"]))
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_withdraw"](i, parsed, note)
        await open_form(interaction, "Withdraw to me", [AMOUNTS, dict(label="Note (optional)", required=False, max=100)], submit_after_thinking(submit))

    async def b_send(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        with svc.db.read() as conn:
            if not cfg_bool(conn, "member_send_enabled"):
                return await interaction.response.send_message("Sending funds to other nations is switched off by ECON right now.", ephemeral=True)
        if not spendable(m["nation_id"]):
            return await interaction.response.send_message("You have no available funds to send. (Locked funds can't be sent.)", ephemeral=True)

        async def submit(i, recipient, amounts, note):
            try:
                parsed = parse_multi(amounts, spendable(m["nation_id"]))
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_send"](i, recipient, parsed, note)
        await open_form(interaction, "Send funds", [dict(label="Recipient nation", placeholder="nation id, link or exact name", max=100),
                                                   AMOUNTS, dict(label="Note (optional)", required=False, max=100)], submit_after_thinking(submit))

    async def b_deposit(interaction):
        m = member(interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        with svc.db.read() as conn:
            usable = bool(svc.deposits and svc.deposits.usable_for(conn, m["nation_id"], m["discord_id"]))
        if not usable:                                     # clear message about exactly what to set up
            await thinking(interaction)
            return await acts["member_api_help"](interaction)

        async def submit(i, amounts):
            try:
                parsed = parse_multi(amounts)
            except L.LedgerError as exc:
                return await reply(i, str(exc))
            await acts["member_deposit"](i, parsed)
        await open_form(interaction, "Deposit funds", [dict(AMOUNTS, label="Deposit what (resource=amount, any number)")], submit_after_thinking(submit))

    def submit_after_thinking(fn):
        async def wrapper(interaction, *values):
            await thinking(interaction)
            await fn(interaction, *values)
        return wrapper

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
