"""/bank offshore - move funds between OUR OWN banks (main -> offshore). Member balances never change."""
from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import fmt
from . import icons
from . import ledger as L
from . import money as M
from .banks import live_holdings
from .buttons import ActionView, open_form
from .config import cfg_get
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, confirm, has_flag, need, reply, thinking
from .valuation import value_amounts

log = logging.getLogger("tunbank.offshore")
ICON = {"COMPLETED": "✅", "PLANNED": "📝", "PENDING": "⏳", "UNCERTAIN": "⚠️", "FAILED": "⛔", "CANCELLED": "🚫"}
LABEL = {"PLANNED": "Waiting for the real PnW transfer", "UNCERTAIN": "Unconfirmed (will complete when PnW shows it)"}

NOT_CONFIGURED = (f"{icons.status('info')} No offshore is configured. Add `OFFSHORE_ALLIANCE_ID`, `OFFSHORE_API_KEY` and "
                  "`OFFSHORE_BOT_KEY` to your `.env` / Railway variables (see docs/10_OFFSHORE.md).")


def register(bank: app_commands.Group, svc: Services):
    off = svc.offshore

    async def allowed(interaction) -> bool:
        with svc.db.read() as conn:
            who = (cfg_get(conn, "offshore_access") or "STAFF").strip().upper()
        if who == "MEMBERS":
            return True
        return await need(svc, interaction, "ADMIN" if who == "ADMIN" else "BANKER")

    def recent(limit=6):
        with svc.db.read() as conn:
            return conn.execute("SELECT * FROM offshore_transfers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    async def status_card() -> A.Card:
        c = A.Card("🏝️ Offshore", "Where our funds physically sit. Moving funds between our banks **never changes anyone's balance**.", A.BLUE)
        try:
            combined, per = await live_holdings(svc.pnw, svc.settings)
            snap = await svc.prices.get()
            for name, held in per.items():
                c.add(f"{'🏦' if name == 'main' else '🏝️'} {name.title()} bank", fmt.amounts_with_value(held, value_amounts(held, snap)), True)
        except (PnWRejected, PnWUncertain) as exc:
            c.add(f"{icons.status('warn')} Banks", f"Could not read the live banks: {exc}")
        mode = off.mode
        c.add("How transfers are sent",
              "**Automatic** - the bot sends them using a main-alliance nation's verified bot key." if mode == "AUTO" else
              "**Manual** - no main-alliance bot key is configured. The bot prepares the transfer and you send it in-game "
              "with the tag it gives you; the bot completes the record when it sees the real transfer.")
        with svc.db.read() as conn:
            who = cfg_get(conn, "offshore_access")
        c.add("Who may offshore", {"MEMBERS": "Staff and members", "ADMIN": "Admins only"}.get(who.upper(), "Staff (Banker and above)"), True)
        rows = recent()
        c.add("Recent transfers", "\n".join(
            f"{ICON.get(r['status'], '')} **#{r['id']}** {LABEL.get(r['status'], r['status'].title())} · {fmt.short_amounts(json.loads(r['amounts_json']))} · "
            f"{fmt.dollars(r['value_cents'])}\n-# {r['mode'].title()} · {r['created_at'][:16].replace('T', ' ')}"
            + (f" · PnW #{r['pnw_record_id']}" if r["pnw_record_id"] else "") for r in rows) or "_None yet_")
        return c

    @bank.command(name="offshore", description="ECON: move funds from the main bank to the offshore bank (ownership unchanged)")
    @app_commands.describe(amounts="e.g. money=5b coal=100k. Leave empty to see the offshore status", reason="Why (for the audit trail)")
    async def offshore(interaction: discord.Interaction, amounts: str = "", reason: str = ""):
        await thinking(interaction)
        if not off or not off.enabled:
            if not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
                return
            return await reply(interaction, NOT_CONFIGURED)
        if not amounts.strip():
            if not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
                return

            async def b_move(i):
                if not await allowed(i):
                    return

                async def done(i2, amt, why):
                    await offshore.callback(i2, amt, why)
                await open_form(i, "Move funds to the offshore", [
                    dict(label="What to move", placeholder="e.g. money=5b coal=100k", max=200),
                    dict(label="Why", required=False, max=150)], done)
            return await reply(interaction, card=await status_card(),
                               view=ActionView(interaction.user.id, [("Move funds…", "🏝️", "primary", b_move)], refresh=status_card))
        if not await allowed(interaction):
            return
        try:
            parsed = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        reason = reason.strip() or "offshore transfer"
        try:
            combined, per = await live_holdings(svc.pnw, svc.settings)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I can't read the live banks, so nothing was moved: {exc}")
        with svc.db.read() as conn:
            try:
                keep = M.parse_amounts(cfg_get(conn, "offshore_keep_in_main") or "") if (cfg_get(conn, "offshore_keep_in_main") or "").strip() else {}
            except M.AmountError:
                keep = {}
            try:
                L.assert_can_mutate(conn, None, "withdraw")
            except L.FinancialBlocked as exc:
                return await reply(interaction, f"{icons.status('lock')} {exc}")
        main_held = per["main"]
        reveal = has_flag(svc, interaction, "bank_view_alliance_holdings")
        short = []
        for r, a in parsed.items():
            eligible = max(0, main_held.get(r, 0) - keep.get(r, 0))
            if eligible < a:
                short.append(f"{M.LABELS[r]}: {M.fmt_units(r, eligible)} can move, {M.fmt_units(r, a)} asked"
                             + (" (the rest is kept in the main bank by policy)" if keep.get(r) else "") if reveal
                             else f"{M.LABELS[r]}")
        if short:
            if not reveal:
                return await reply(interaction, f"{icons.status('bad')} The main bank can't cover that transfer right now (" + ", ".join(short) + ").")
            return await reply(interaction, f"{icons.status('bad')} The **main bank** doesn't hold enough to move that:\n" + "\n".join(short))
        snap = await svc.prices.get()
        val = value_amounts(parsed, snap)
        auto = off.mode == "AUTO"
        card = A.Card("🏝️ Confirm: move funds to the OFFSHORE", "Only the physical location changes. **No member balance, lock, tax or "
                      "#ignore record is touched.**", A.ORANGE)
        card.add("From → To", f"🏦 Main bank → 🏝️ Offshore bank", True)
        card.add("Sent how", "Automatically by the bot" if auto else "You send it in-game (the bot gives you the exact note)", True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        if reveal:
            card.add("Main bank after", fmt.amount_lines(M.sub(main_held, parsed)), True)
            card.add("Offshore after", fmt.amount_lines(M.add(per["offshore"], parsed)), True)
        card.add("Reason", reason)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was moved.")
        actor = str(interaction.user.id)
        try:
            tid, created = await asyncio.to_thread(off.create, actor=actor, amounts=parsed, reason=reason,
                                                   key=f"off-{interaction.id}", value_cents=val.total_cents,
                                                   snapshot_id=snap.id if snap else None)
        except L.LedgerError as exc:
            return await reply(interaction, f"{icons.status('lock')} {exc}")
        if auto:
            res = await off.execute_auto(tid)
            ok = res["status"] == "COMPLETED"
            out = A.Card(f"{icons.status('ok') if ok else icons.status('warn' if res['status'] == 'UNCERTAIN' else 'bad')} "
                         f"Offshore transfer #{tid} · {res['status'].title()}", res["message"],
                         A.GREEN if ok else (A.ORANGE if res["status"] == "UNCERTAIN" else A.RED), kind="OFFSHORE")
            out.add("Moved", fmt.amounts_with_value(parsed, val))
            out.add("By", actor_label(interaction), True)
            if res.get("record_id"):
                out.add("PnW record", f"#{res['record_id']}", True)
            out.add("Member balances", "Unchanged.", True)
            await reply(interaction, card=out, view=ActionView(interaction.user.id, [
                ("Offshore status", "🏝️", "secondary", lambda i: offshore.callback(i, "", ""))]))
            await svc.alerts.econ(out)
            await svc.alerts.flush_events()
            return
        note = off.note_for(tid, reason)
        out = A.Card(f"📝 Offshore transfer #{tid} prepared", "Now send it in Politics & War. The bot completes this record "
                     "when it sees the real transfer.", A.BLUE, kind="OFFSHORE")
        out.add("Amount", fmt.amounts_with_value(parsed, val))
        out.add("In game", f"Alliance → Bank → **Withdraw** → send to **alliance #{svc.settings.offshore.alliance_id}**.\n"
                           f"Put exactly this in the note: `{note}`")
        out.add("Important", "Send exactly these amounts, once. A different amount or a missing tag is still recorded, but flagged for review.")

        async def b_cancel(i):
            ok = await asyncio.to_thread(off.cancel, tid, str(i.user.id))
            await reply(i, f"Plan #{tid} cancelled." if ok else f"Plan #{tid} can't be cancelled (it is no longer waiting).")
        await reply(interaction, card=out, view=ActionView(interaction.user.id, [("Cancel this plan", "🚫", "danger", b_cancel)]))
        await svc.alerts.econ(out)

    async def offshore_move(interaction):
        """The panel's 'Offshore Funds' button: same permission rule (offshore_access) and the same transfer code as /bank offshore."""
        if not off or not off.enabled:
            return await interaction.response.send_message("Offshore transfers are not set up for this bank.", ephemeral=True)
        if not await allowed(interaction):
            return                                              # allowed() already told them why

        async def done(i2, amt, why):
            await offshore.callback(i2, amt, why)
        await open_form(interaction, "Move funds to the offshore", [
            dict(label="What to move", placeholder="e.g. money=5b coal=100k", max=200),
            dict(label="Why", required=False, max=150)], done)

    svc.actions["offshore"] = offshore.callback
    svc.actions["offshore_move"] = offshore_move
