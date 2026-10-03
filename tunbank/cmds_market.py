"""/prices - the live market prices every balance is valued with (anyone can look)."""
from __future__ import annotations

import discord

from . import alerts as A
from . import icons
from . import money as M
from .buttons import ActionView
from .ui import Services, reply, thinking


def register(tree, svc: Services):
    async def build(force: bool = False) -> A.Card:
        snap = await svc.prices.get(force=force)
        c = A.Card(f"{icons.status('value')} Market prices", "The prices used for every **Current Market Value** in the bank.", A.BLUE)
        if snap is None:
            c.color = A.RED
            c.description = f"{icons.status('bad')} No price data is available yet, so only cash can be valued."
        else:
            lines = [f"{icons.resource('money')} **Cash** — $1.00 each"]
            for r in M.NON_CASH:
                price = snap.prices.get(r)
                lines.append(f"{icons.resource(r)} **{M.LABELS[r]}** — " +
                             (f"${price:,.0f}" if price is not None else "_no price_"))
            half = (len(lines) + 1) // 2
            c.add("Per unit", "\n".join(lines[:half]), True)
            c.add("\u200b", "\n".join(lines[half:]), True)
            c.footer = f"Prices as of {snap.fetched_at} · TUN Bank"
            if snap.stale:
                c.color = A.ORANGE
                c.add(f"{icons.status('warn')} Stale", "These prices are older than expected; values are approximate.")
        if svc.prices.last_error:
            c.color = A.ORANGE if snap else A.RED
            c.add(f"{icons.status('warn')} Last refresh failed", svc.prices.last_error[:500] +
                  "\nShowing the last saved prices. ECON: run `python scripts/check_pnw.py` to test the connection.")
        return c

    @tree.command(name="prices", description="Current market prices used to value balances")
    async def prices(interaction: discord.Interaction):
        await thinking(interaction)
        await reply(interaction, card=await build(), view=ActionView(interaction.user.id, [], refresh=lambda: build(True)))

    svc.actions["prices"] = prices.callback
