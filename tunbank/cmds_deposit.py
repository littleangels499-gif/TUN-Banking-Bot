"""/deposit reset and /deposit restore (Admin only): clear member balances formally, then load the verified ones."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import deposit_reset as DR
from . import fmt
from . import importer
from . import ledger as L
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, confirm, need, reply, thinking

log = logging.getLogger("tunbank.deposit")


def import_preview_card(p: importer.Preview, title: str, *, current_nonzero: int = 0) -> A.Card:
    """The preview shown BEFORE anything is written (shared by /deposit restore and /bankset importopening)."""
    c = A.Card(title, "Nothing has been changed yet.", A.RED if p.blocked else A.ORANGE)
    c.add("File", f"{p.filename}\nSHA-256 `{p.file_sha256[:16]}…`", True)
    c.add("Nations", f"{len(p.nations)} ({p.used_ids} by id, {p.used_names} by name only) · {len(p.rows)} amounts", True)
    c.add("Total positive deposits", fmt.amounts_with_value(p.positive_totals, p.valuation_positive) if p.positive_totals else "none")
    if p.negative_totals:
        c.add("Total NEGATIVE resource balances (owed to the alliance)",
              fmt.amounts_with_value(p.negative_totals, p.valuation_negative))
    if p.totals:
        c.add("Net market value (positives minus negatives)", fmt.value_line(p.valuation))
    c.add("Total outstanding loans", f"${p.loan_total_cents / 100:,.2f} across {len(p.loans)} nation(s) · kept as loans, NOT deposits"
          if p.loans else "none")
    if current_nonzero:
        c.add("⚠️ Balances exist right now",
              f"{current_nonzero} balance line(s) are non-zero (deposits credited since the reset). The restore ADDS to "
              "them. Make sure the spreadsheet does not already include those deposits.")
    if p.missing_members:
        c.add("Alliance members missing from file", ", ".join(map(str, p.missing_members[:20])) + (" …" if len(p.missing_members) > 20 else ""))
    if p.warnings:
        c.add("Warnings", "\n".join(p.warnings[:8]))
    if p.errors:
        c.add(f"ERRORS ({len(p.errors)}): import BLOCKED", "\n".join(p.errors[:12]))
        c.add("Fix the file and upload it again", "Nothing was imported.")
    return c


def register(deposit: app_commands.Group, svc: Services):
    uid = lambda i: str(i.user.id)  # noqa: E731

    @deposit.command(name="reset", description="Admin: clear ALL member deposit balances (permanent audit record, history kept)")
    @app_commands.describe(reason="Why the reset is being done (recorded permanently)",
                           confirm_phrase=f"Type {DR.RESET_PHRASE} to confirm you understand")
    async def reset(interaction: discord.Interaction, reason: str, confirm_phrase: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        if confirm_phrase.strip() != DR.RESET_PHRASE:
            return await reply(interaction, f"Nothing was changed. To reset, type exactly `{DR.RESET_PHRASE}` in the confirm_phrase box.")
        snap = await svc.prices.get()

        def look():
            with svc.db.read() as conn:
                return DR.preview(conn, snap)
        pv = await asyncio.to_thread(look)
        if pv["waiting_restore"]:
            return await reply(interaction, f"Deposit reset #{pv['waiting_restore']['id']} is still waiting for its restore "
                                            "import. Run `/deposit restore` first.")
        if not pv["lines"]:
            return await reply(interaction, "There are no member balances to reset.")
        c = A.Card("⚠️ Confirm DEPOSIT RESET", "Every member balance will be brought to zero by a formal ledger event. "
                   "No history is deleted. This cannot be undone except by a restore import.", A.RED)
        c.add("Accounts / balance lines", f"{pv['nations']} / {len(pv['lines'])}", True)
        c.add("Value before reset", fmt.value_line(pv["valuation"]), True)
        c.add("Totals being cleared", fmt.amounts_with_value(pv["totals"], pv["valuation"]))
        c.add("Reason", reason[:300])
        c.add("Withdrawals", "paused ✅" if pv["paused"] else "NOT paused ❌ - run `/bank lock` first", True)
        if pv["in_flight"]:
            c.add("In-flight withdrawals", f"{pv['in_flight']} - must finish first", True)
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Reset cancelled. Nothing was changed.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return DR.execute(conn, actor=uid(interaction), reason=reason, snapshot=snap)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Reset refused, nothing was changed: {exc}")
        out = A.Card("🧨 Deposit reset complete", f"Reset #{res['reset_id']} by {actor_label(interaction)}", A.GREEN, kind="RESET")
        out.add("Accounts cleared", f"{res['nations']} ({res['lines']} balance lines)", True)
        out.add("Value before reset", fmt.value_line(res["valuation"]), True)
        out.add("Reason", reason[:300])
        out.add("Next step", "Load the verified balances with `/deposit restore`, then `/bank unlock`.")
        await reply(interaction, card=out)
        await svc.alerts.econ(out)

    @deposit.command(name="restore", description="Admin: load verified balances (+ loans) from a spreadsheet after a reset")
    @app_commands.describe(file=".xlsx or .csv with nation_id and/or nation_name, resource columns, optional loan column",
                           note="Where this data came from")
    async def restore(interaction: discord.Interaction, file: discord.Attachment, note: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        if file.size > importer.MAX_BYTES:
            return await reply(interaction, "File is too large (max 5 MB).")
        with svc.db.read() as conn:
            rs = DR.open_reset(conn)
            nonzero = conn.execute("SELECT COUNT(*) FROM balances WHERE amount != 0").fetchone()[0]
        if not rs:
            return await reply(interaction, "There is no deposit reset waiting for a restore. Run `/deposit reset` first.")
        data = await file.read()
        snap = await svc.prices.get()
        try:
            members = await svc.pnw.fetch_alliance_members()
        except (PnWRejected, PnWUncertain):
            members = None
        p = await asyncio.to_thread(importer.preview, file.filename, data, members=members, snapshot=snap, mode="RESTORE")
        card = import_preview_card(p, f"Restore PREVIEW (after reset #{rs['id']})", current_nonzero=nonzero)
        if p.errors:
            return await reply(interaction, card=card)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Restore cancelled. Nothing was changed.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return importer.commit(conn, p, admin_id=uid(interaction), note=note, kind="RESTORE",
                                           reset_id=rs["id"], snapshot_id=snap.id if snap else None)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Restore refused, nothing was changed: {exc}")
        out = A.Card("♻️ Balances restored", f"Batch #{res['batch_id']} (reset #{rs['id']}) by {actor_label(interaction)}", A.GREEN, kind="IMPORT")
        out.add("Nations / amounts", f"{res['nations']} / {res['rows']}", True)
        out.add("Net value", fmt.value_line(p.valuation), True)
        if res["loans"]:
            out.add("Outstanding loans stored", f"{res['loans']} nation(s) · ${res['loan_total_cents'] / 100:,.2f} (not deposits)")
        out.add("Next step", "Run `/ledger reconcile`, then `/bank unlock` to resume withdrawals.")
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
