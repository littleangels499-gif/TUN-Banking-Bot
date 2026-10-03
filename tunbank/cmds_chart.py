"""/chart commands: pictures drawn from the bot's real data."""
from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import charts as C
from . import icons
from . import ledger as L
from . import resolve as RS
from .ui import Services, need, nation_arg, reply, thinking

log = logging.getLogger("tunbank.chart")


def register(chart: app_commands.Group, svc: Services):
    async def send(interaction, build, name: str, title: str, text: str):
        """Draw in a background thread (charts are CPU work) and post one clean card + image."""
        try:
            png = await asyncio.to_thread(build)
        except C.ChartError as exc:
            return await reply(interaction, f"{icons.status('info')} {exc}")
        except Exception:  # noqa: BLE001
            log.exception("chart failed")
            return await reply(interaction, "Sorry, I couldn't draw that chart right now. Nothing was changed.")
        card = A.Card(f"{icons.status('chart')} {title}", text, A.BLUE)
        card.image = f"attachment://{name}"
        await reply(interaction, card=card, file=discord.File(io.BytesIO(png), filename=name))

    def me(interaction):
        with svc.db.read() as conn:
            return L.member_by_discord(conn, interaction.user.id)

    def label(nation_id):
        with svc.db.read() as conn:
            return RS.label(conn, nation_id)

    # ---------------------------------------------------- members: own account
    @chart.command(name="mybalance", description="Your holdings as a chart (share of value per resource)")
    async def mybalance(interaction: discord.Interaction):
        await thinking(interaction)
        m = me(interaction)
        if not m:
            return await reply(interaction, "Link your nation first with `/nation link`.")
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.composition(conn, snap, m["nation_id"], label(m["nation_id"]))
        await send(interaction, build, "composition.png", "Your resource mix", "Share of your deposit's value per resource.")

    @chart.command(name="mytrend", description="Your account value over time")
    async def mytrend(interaction: discord.Interaction):
        await thinking(interaction)
        m = me(interaction)
        if not m:
            return await reply(interaction, "Link your nation first with `/nation link`.")
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.wealth(conn, snap, m["nation_id"], label(m["nation_id"]))
        await send(interaction, build, "trend.png", "Your account over time", "Valued at current market prices.")

    # ------------------------------------------------------------- staff
    KINDS = [app_commands.Choice(name="Resource mix", value="mix"), app_commands.Choice(name="Value over time", value="trend"),
             app_commands.Choice(name="Deposits per day", value="deposits")]

    @chart.command(name="nation", description="ECON: charts for one member's account")
    @app_commands.describe(nation="Member: id, name, link or @user", kind="Which chart", days="Days to show (deposits chart)")
    @app_commands.choices(kind=KINDS)
    async def nation(interaction: discord.Interaction, nation: str, kind: app_commands.Choice[str], days: int = 30):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        days = max(7, min(90, days))
        snap = await svc.prices.get()
        lab = label(nid)

        def build():
            with svc.db.read() as conn:
                if kind.value == "mix":
                    return C.composition(conn, snap, nid, lab)
                if kind.value == "trend":
                    return C.wealth(conn, snap, nid, lab)
                return C.deposits(conn, snap, days, nid, lab)
        await send(interaction, build, f"{kind.value}.png", f"{lab}: {kind.name}", "Drawn from the ledger.")

    @chart.command(name="vault", description="Confidential: alliance-owned vs member-held funds per resource")
    async def vault(interaction: discord.Interaction):
        if not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
            return
        await thinking(interaction)
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.vault(conn, snap)
        await send(interaction, build, "vault.png", "Alliance vs member-held", "From the latest reconciliation.")

    @chart.command(name="members", description="Confidential: all member-held funds by resource")
    async def members(interaction: discord.Interaction):
        if not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
            return
        await thinking(interaction)
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.composition(conn, snap, None, "All member-held funds")
        await send(interaction, build, "members.png", "Member-held funds", "Share of value per resource.")

    @chart.command(name="deposits", description="ECON: deposits per day across all members")
    @app_commands.describe(days="Days to show (7-90)")
    async def deposits(interaction: discord.Interaction, days: int = 30):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        days = max(7, min(90, days))
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.deposits(conn, snap, days)
        await send(interaction, build, "deposits.png", "Deposits per day", f"Last {days} days.")

    @chart.command(name="tax", description="Confidential: tax collected per day (alliance-owned)")
    @app_commands.describe(days="Days to show (7-90)")
    async def tax(interaction: discord.Interaction, days: int = 30):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        days = max(7, min(90, days))
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.tax(conn, snap, days)
        await send(interaction, build, "tax.png", "Tax collected per day", f"Last {days} days.")

    svc.actions.update(chart_vault=vault.callback, chart_nation=nation.callback, chart_tax=tax.callback)
    RS.attach(svc, nation, "nation")
