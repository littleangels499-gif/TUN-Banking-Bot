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
from .buttons import _safe, open_form
from .config import cfg_bool
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, confirm, need, reply, thinking

log = logging.getLogger("tunbank.convert")

NOT_LINKED = "You have not linked your nation yet. Use `/nation link` first (your nation id, link or name)."
PANEL_ID = "tunbank:convert:open"


def _price_text(res: str, price) -> str:
    return "$1.00" if res == "money" else f"${price:,.2f} each"


def quotes_card(qs: list) -> A.Card:
    c = A.Card("💱 RESOURCE CONVERSION", "Check everything. Nothing changes until you press Confirm. It happens all together or not at all.", A.ORANGE)
    c.add("You are converting", "\n".join(f"{icons.resource(q.from_res)} **{M.fmt_units(q.from_res, q.from_units)}** {M.LABELS[q.from_res]}"
                                          f"  →  {M.fmt_units(q.to_res, q.to_units)}" for q in qs)[:1000])
    to = qs[0].to_res
    c.add("You receive", f"{icons.resource(to)} **{M.fmt_units(to, sum(q.to_units for q in qs))}** {M.LABELS[to]}")
    c.add("Market value", fmt.dollars(sum(q.value_cents for q in qs)), True)
    c.add("Price snapshot", f"{qs[0].as_of or 'current'}", True)
    c.add("Prices used", "\n".join(f"{M.LABELS[q.from_res]}: {_price_text(q.from_res, q.from_price)}" for q in qs)
          + f"\n{M.LABELS[to]}: {_price_text(to, qs[0].to_price)}")
    c.add("Good to know", "Each amount is rounded down to the nearest 0.01. This only changes your TUN Bank balance, nothing happens in-game.")
    return c


async def run_conversion(svc: Services, interaction: discord.Interaction, nation_id: int, to_res: str, amounts_text: str):
    """Quote -> confirm -> re-price -> execute, for ANY number of resources converted into one. All or nothing."""
    await thinking(interaction)
    try:
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            free = CV.convertible(conn, nation_id)
        parsed = CV.parse_multi(amounts_text, free)
        if to_res in parsed:
            return await reply(interaction, f"You can't convert {M.LABELS[to_res]} into itself. Remove it from the list.")
        short = [f"{M.LABELS[r]} (you have {M.fmt_units(r, free.get(r, 0))})" for r, u in parsed.items() if u > free.get(r, 0)]
        if short:
            return await reply(interaction, "Nothing was converted. You don't have enough available: " + ", ".join(short))
        qs = [CV.make_quote(snap, r, to_res, u) for r, u in parsed.items()]
    except L.LedgerError as exc:
        return await reply(interaction, str(exc))
    if not await confirm(svc, interaction, quotes_card(qs)):
        return await reply(interaction, "Conversion cancelled. Nothing was changed.")

    # Prices are fetched again at the moment of confirmation: if they moved, nothing is converted.
    try:
        snap2 = await svc.prices.get()
        qs2 = [CV.make_quote(snap2, q.from_res, to_res, q.from_units) for q in qs]
    except L.LedgerError as exc:
        return await reply(interaction, f"Nothing was converted. {exc}")
    if [(q.to_units, q.snapshot_id) for q in qs2] != [(q.to_units, q.snapshot_id) for q in qs]:
        return await reply(interaction, card=quotes_card(qs2), content="Prices changed while you were confirming, so nothing was "
                           "converted. Here is the new quote; press **Convert resources** again to use it.")
    try:
        holdings, _ = await live_holdings(svc.pnw, svc.settings, svc.db)
    except (PnWRejected, PnWUncertain):
        holdings = None

    def do():
        with svc.db.tx() as conn:                      # one transaction: any refusal undoes every part
            return [CV.execute(conn, nation_id=nation_id, quote=q, actor=str(interaction.user.id),
                               idempotency_key=f"conv-{interaction.id}-{q.from_res}", holdings=holdings) for q in qs2]
    try:
        results = await asyncio.to_thread(do)
    except CV.AllianceStockShort as exc:
        await svc.alerts.econ(A.Card("⚠️ Conversion refused", f"{actor_label(interaction)}: {exc.internal}", A.ORANGE))
        return await reply(interaction, str(exc))
    except L.LedgerError as exc:
        return await reply(interaction, f"Nothing was converted. {exc}")

    replay = all(r["replay"] for r in results)
    ids = ", ".join(f"#{r['conversion_id']}" for r in results)
    out = A.Card("✅ Conversion complete" if not replay else "Conversion already completed", f"Conversion {ids}", A.GREEN, kind="CONVERSION")
    out.add("Converted", "\n".join(f"{M.fmt_units(q.from_res, q.from_units)} {M.LABELS[q.from_res]}" for q in qs2), True)
    out.add("Received", f"{M.fmt_units(to_res, sum(r['to_units'] for r in results))} {M.LABELS[to_res]}", True)
    out.add("Market value", fmt.dollars(sum(r["value_cents"] for r in results)), True)
    if results[-1].get("after"):
        out.add("Your available balance now", fmt.amount_lines(results[-1]["after"]["available"]))
    await reply(interaction, card=out)
    if not replay:
        log_card = A.Card("💱 Resource conversion", f"{actor_label(interaction)} · nation #{nation_id}", A.BLUE, kind="CONVERSION", nation_id=nation_id)
        log_card.add("Converted → received", " + ".join(f"{M.fmt_units(q.from_res, q.from_units)} {M.LABELS[q.from_res]}" for q in qs2)
                     + f" → {M.fmt_units(to_res, sum(r['to_units'] for r in results))} {M.LABELS[to_res]}")
        log_card.add("Market value", fmt.dollars(sum(r["value_cents"] for r in results)), True)
        log_card.add("Price snapshot", f"{qs2[0].as_of} (#{qs2[0].snapshot_id})", True)
        await svc.alerts.econ(log_card)


class ConverterView(discord.ui.View):
    """Private (ephemeral) screen: pick what to RECEIVE, then type what to convert (any number of resources)."""

    def __init__(self, svc: Services, user_id: int, nation_id: int, free: dict):
        super().__init__(timeout=600)
        self.svc, self.user_id, self.nation_id, self.free = svc, user_id, nation_id, free
        self.to_res: str | None = None
        self.to_select = discord.ui.Select(
            placeholder="1 · Resource to receive", min_values=1, max_values=1,
            options=[discord.SelectOption(label=M.LABELS[r][:100], value=r) for r in M.RESOURCES])
        self.to_select.callback = self._picked_to
        self.amount_button = discord.ui.Button(label="2 · What to convert", emoji="✏️", style=discord.ButtonStyle.primary)
        self.amount_button.callback = self._amount
        for item in (self.to_select, self.amount_button):
            self.add_item(item)

    def card(self) -> A.Card:
        c = A.Card("💱 Resource conversion", "Pick what you want to receive, then list what to convert (any number of resources).", A.BLUE)
        c.add("Receive", M.LABELS[self.to_res] if self.to_res else "— not chosen yet —", True)
        c.add("You can convert", "\n".join(f"{icons.resource(r)} {M.fmt_units(r, v)} {M.LABELS[r]}" for r, v in self.free.items())[:1000], True)
        return c

    async def _mine(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _picked_to(self, interaction):
        if await self._mine(interaction):
            self.to_res = self.to_select.values[0]
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)

    async def _amount(self, interaction):
        if not await self._mine(interaction):
            return
        if not self.to_res:
            return await interaction.response.send_message("Choose what you want to receive first.", ephemeral=True)
        to_res = self.to_res

        async def submitted(i, text):
            await _safe(i, run_conversion(self.svc, i, self.nation_id, to_res, text))
        await open_form(interaction, f"Convert into {M.LABELS[to_res]}", [
            dict(label="What to convert (resource=amount)", placeholder="e.g. food=1m coal=500k   (food=all works too)", max=400, long=True)],
            submitted)


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
