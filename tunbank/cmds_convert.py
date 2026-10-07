"""Resource Conversion Panel: a button in a channel -> pick what to convert and what to receive -> amount -> confirm.

Conversion only changes the member's TUN Bank balance (see conversion.py). Nothing is sent in-game.
Everything the member presses is re-checked on the server; the panel itself is only a button.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import conversion as CV
from . import fmt
from . import icons
from . import ledger as L
from . import money as M
from .banks import live_holdings
from .buttons import _safe
from .config import cfg_bool
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, confirm, need, reply, thinking

log = logging.getLogger("tunbank.convert")

NOT_LINKED = "You have not linked your nation yet. Use `/nation link` first (your nation id, link or name)."
PANEL_ID = "tunbank:convert:open"


def _price_text(res: str, price) -> str:
    return "$1.00" if res == "money" else f"${price:,.2f} each"


def quote_card(q: CV.Quote) -> A.Card:
    c = A.Card("💱 RESOURCE CONVERSION", "Check everything. Nothing changes until you press Confirm.", A.ORANGE)
    c.add("You are converting", f"{icons.resource(q.from_res)} **{M.fmt_units(q.from_res, q.from_units)}** {M.LABELS[q.from_res]}")
    c.add("Into", f"{icons.resource(q.to_res)} **{M.fmt_units(q.to_res, q.to_units)}** {M.LABELS[q.to_res]}")
    c.add("Market value", fmt.dollars(q.value_cents), True)
    c.add("Price snapshot", f"{q.as_of or 'current'}", True)
    c.add("Prices used", f"{M.LABELS[q.from_res]}: {_price_text(q.from_res, q.from_price)}\n"
                         f"{M.LABELS[q.to_res]}: {_price_text(q.to_res, q.to_price)}")
    c.add("Good to know", "Rounded down to the nearest 0.01. This only changes your TUN Bank balance, nothing happens in-game.")
    return c


async def run_conversion(svc: Services, interaction: discord.Interaction, nation_id: int, from_res: str, to_res: str,
                         amount_text: str):
    """Quote -> confirm -> re-price -> execute. Called from the amount pop-up."""
    await thinking(interaction)
    try:
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            free = CV.convertible(conn, nation_id)
        units = CV.parse_amount(amount_text, from_res, free.get(from_res, 0))
        if units > free.get(from_res, 0):
            return await reply(interaction, f"You only have {M.fmt_units(from_res, free.get(from_res, 0))} "
                                            f"{M.LABELS[from_res]} available to convert.")
        q = CV.make_quote(snap, from_res, to_res, units)
    except L.LedgerError as exc:
        return await reply(interaction, str(exc))
    if not await confirm(svc, interaction, quote_card(q)):
        return await reply(interaction, "Conversion cancelled. Nothing was changed.")

    # Prices are fetched again at the moment of confirmation: if they moved, nothing is converted.
    try:
        snap2 = await svc.prices.get()
        q2 = CV.make_quote(snap2, from_res, to_res, units)
    except L.LedgerError as exc:
        return await reply(interaction, f"Nothing was converted. {exc}")
    if (q2.to_units, q2.snapshot_id) != (q.to_units, q.snapshot_id):
        return await reply(interaction, card=_moved_card(q2), content="Prices changed while you were confirming, so "
                           "nothing was converted. Here is the new quote; press **Convert resources** again to use it.")
    try:
        holdings, _ = await live_holdings(svc.pnw, svc.settings)
    except (PnWRejected, PnWUncertain):
        holdings = None

    def do():
        with svc.db.tx() as conn:
            return CV.execute(conn, nation_id=nation_id, quote=q2, actor=str(interaction.user.id),
                              idempotency_key=f"conv-{interaction.id}", holdings=holdings)
    try:
        res = await asyncio.to_thread(do)
    except CV.AllianceStockShort as exc:
        await svc.alerts.econ(A.Card("⚠️ Conversion refused", f"{actor_label(interaction)}: {exc.internal}", A.ORANGE))
        return await reply(interaction, str(exc))
    except L.LedgerError as exc:
        return await reply(interaction, f"Nothing was converted. {exc}")

    out = A.Card("✅ Conversion complete" if not res["replay"] else "Conversion already completed", f"Conversion #{res['conversion_id']}", A.GREEN, kind="CONVERSION")
    out.add("Converted", f"{M.fmt_units(from_res, res['from_units'])} {M.LABELS[from_res]}", True)
    out.add("Received", f"{M.fmt_units(to_res, res['to_units'])} {M.LABELS[to_res]}", True)
    out.add("Market value", fmt.dollars(res["value_cents"]), True)
    if res.get("after"):
        out.add("Your available balance now", fmt.amount_lines(res["after"]["available"]))
    await reply(interaction, card=out)
    if not res["replay"]:
        log_card = A.Card("💱 Resource conversion", f"{actor_label(interaction)} · nation #{nation_id}", A.BLUE, kind="CONVERSION", nation_id=nation_id)
        log_card.add("Converted → received", f"{M.fmt_units(from_res, res['from_units'])} {M.LABELS[from_res]} → "
                                             f"{M.fmt_units(to_res, res['to_units'])} {M.LABELS[to_res]}")
        log_card.add("Market value", fmt.dollars(res["value_cents"]), True)
        log_card.add("Price snapshot", f"{q2.as_of} (#{q2.snapshot_id})", True)
        await svc.alerts.econ(log_card)


def _moved_card(q: CV.Quote) -> A.Card:
    c = A.Card("💱 New quote", "", A.ORANGE)
    c.add("You would receive", f"{M.fmt_units(q.to_res, q.to_units)} {M.LABELS[q.to_res]}", True)
    c.add("Market value", fmt.dollars(q.value_cents), True)
    return c


class AmountModal(discord.ui.Modal):
    def __init__(self, svc: Services, nation_id: int, from_res: str, to_res: str):
        super().__init__(title=f"Convert {M.LABELS[from_res]}"[:45])
        self.svc, self.nation_id, self.from_res, self.to_res = svc, nation_id, from_res, to_res
        self.amount = discord.ui.TextInput(label=f"How much {M.LABELS[from_res]}?"[:45],
                                           placeholder="e.g. 90m, 1,000,000 or all", required=True, max_length=30)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        from . import configaudit as CA
        CA.ACTION.set("form: Resource conversion")
        await _safe(interaction, run_conversion(self.svc, interaction, self.nation_id, self.from_res, self.to_res,
                                                str(self.amount.value or "").strip()))


class ConverterView(discord.ui.View):
    """Private (ephemeral) screen: two drop-downs and an amount button. Only the person who opened it can use it."""

    def __init__(self, svc: Services, user_id: int, nation_id: int, free: dict):
        super().__init__(timeout=600)
        self.svc, self.user_id, self.nation_id = svc, user_id, nation_id
        self.from_res: str | None = None
        self.to_res: str | None = None
        self.from_select = discord.ui.Select(
            placeholder="1 · Resource to convert", min_values=1, max_values=1,
            options=[discord.SelectOption(label=f"{M.LABELS[r]} · {M.fmt_units(r, free[r])} available"[:100], value=r)
                     for r in M.RESOURCES if free.get(r)][:25])
        self.to_select = discord.ui.Select(
            placeholder="2 · Resource to receive", min_values=1, max_values=1,
            options=[discord.SelectOption(label=M.LABELS[r][:100], value=r) for r in M.RESOURCES])
        self.from_select.callback = self._picked_from
        self.to_select.callback = self._picked_to
        self.amount_button = discord.ui.Button(label="3 · Enter amount", emoji="✏️", style=discord.ButtonStyle.primary)
        self.amount_button.callback = self._amount
        for item in (self.from_select, self.to_select, self.amount_button):
            self.add_item(item)

    def card(self) -> A.Card:
        c = A.Card("💱 Resource conversion", "Pick what to convert, what to receive, then enter the amount.", A.BLUE)
        c.add("Convert", M.LABELS[self.from_res] if self.from_res else "— not chosen yet —", True)
        c.add("Receive", M.LABELS[self.to_res] if self.to_res else "— not chosen yet —", True)
        return c

    async def _mine(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _picked_from(self, interaction):
        if await self._mine(interaction):
            self.from_res = self.from_select.values[0]
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)

    async def _picked_to(self, interaction):
        if await self._mine(interaction):
            self.to_res = self.to_select.values[0]
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)

    async def _amount(self, interaction):
        if not await self._mine(interaction):
            return
        if not self.from_res or not self.to_res:
            return await interaction.response.send_message("Choose both resources first.", ephemeral=True)
        if self.from_res == self.to_res:
            return await interaction.response.send_message("Choose two different resources.", ephemeral=True)
        await interaction.response.send_modal(AmountModal(self.svc, self.nation_id, self.from_res, self.to_res))


async def open_converter(svc: Services, interaction: discord.Interaction):
    with svc.db.read() as conn:
        m = L.member_by_discord(conn, interaction.user.id)
        enabled = cfg_bool(conn, "conversion_enabled")
        free = CV.convertible(conn, m["nation_id"]) if m else {}
    if not m:
        return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
    if not enabled:
        return await interaction.response.send_message("Resource conversion is switched off by ECON right now.", ephemeral=True)
    if not free:
        return await interaction.response.send_message(
            "You have nothing available to convert. (Locked funds and negative balances can't be converted.)", ephemeral=True)
    view = ConverterView(svc, interaction.user.id, m["nation_id"], free)
    await interaction.response.send_message(embed=A.to_embed(view.card()), view=view, ephemeral=True)


class ConversionPanelView(discord.ui.View):
    """The public panel: one button that never expires (re-registered when the bot starts)."""

    def __init__(self, svc: Services):
        super().__init__(timeout=None)
        self.svc = svc
        b = discord.ui.Button(label="Convert resources", emoji="💱", style=discord.ButtonStyle.primary, custom_id=PANEL_ID)
        b.callback = self._open
        self.add_item(b)

    async def _open(self, interaction: discord.Interaction):
        from . import configaudit as CA
        CA.ACTION.set("button: Convert resources")
        await _safe(interaction, open_converter(self.svc, interaction))


def panel_card() -> A.Card:
    c = A.Card("💱 Resource Conversion",
               "Swap one resource for another inside your TUN Bank balance, at current market prices.\n\n"
               "**1.** Press the button  **2.** Choose what to convert and what to receive  **3.** Enter the amount  "
               "**4.** Check the quote and confirm.", A.BLUE)
    c.add("Good to know", "• This only changes your bank balance. Nothing is sent in-game.\n"
                          "• Locked (reserved) funds and negative balances can't be converted.\n"
                          "• You can only receive resources the alliance currently has available.")
    return c


def register(bankset: app_commands.Group, svc: Services):
    @bankset.command(name="conversionpanel", description="Admin: post the Resource Conversion panel in a channel")
    @app_commands.describe(channel="Where to post the panel (default: this channel)")
    async def conversionpanel(interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        target = channel or interaction.channel
        try:
            await target.send(embed=A.to_embed(panel_card()), view=ConversionPanelView(svc))
        except (discord.HTTPException, AttributeError):
            return await reply(interaction, "I couldn't post there. Check that I can send messages and embeds in that channel.")

        def log():
            with svc.db.tx() as conn:
                L.audit(conn, str(interaction.user.id), "CONVERSION_PANEL_POSTED", f"channel:{getattr(target, 'id', '?')}", {})
        await asyncio.to_thread(log)
        await reply(interaction, f"Conversion panel posted in {getattr(target, 'mention', 'this channel')}.")
