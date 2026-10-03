"""/bulk commands: pay many nations from alliance-owned funds, safely."""
from __future__ import annotations

import asyncio
import io
import json
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import bulk as B
from . import fmt
from . import icons
from . import importer
from . import ledger as L
from . import limits as LIM
from . import money as M
from .buttons import ActionView
from .config import cfg_int
from .pnw import PnWRejected, PnWUncertain
from .resolve import ResolveError, resolve
from .ui import Services, chunk_cards, confirm, need, paginate, reply, role_ids, thinking
from .valuation import value_amounts

log = logging.getLogger("tunbank.bulk")

ICON = {"COMPLETED": "✅", "FAILED": "⛔", "UNCERTAIN": "⚠️", "PENDING": "⏳"}
BATCH_ICON = {"COMPLETED": "✅", "PARTIAL": "⚠️", "HALTED": "⏸️", "RUNNING": "⏳"}


def register(bulk: app_commands.Group, svc: Services):
    # ---------------------------------------------------------------- views
    def summary_card(batch_id: int) -> A.Card:
        with svc.db.read() as conn:
            b, items = B.load_batch(conn, batch_id)
            s = B.summarize(conn, batch_id)
        if not b:
            return A.Card("Bulk transfer", f"Batch #{batch_id} does not exist.", A.RED)
        c = A.Card(f"{BATCH_ICON.get(b['status'], '')} Bulk transfer #{b['id']} · {b['status'].title()}",
                   f"{b['reason']}\n-# by <@{b['actor']}> · {b['created_at'][:16].replace('T', ' ')} · file `{b['source_filename']}`",
                   {"COMPLETED": A.GREEN, "PARTIAL": A.ORANGE, "HALTED": A.ORANGE}.get(b["status"], A.BLUE), kind="BULK")
        n = s["counts"]
        c.add("Results", f"{ICON['COMPLETED']} {n['COMPLETED']} sent · {ICON['FAILED']} {n['FAILED']} failed · "
                         f"{ICON['UNCERTAIN']} {n['UNCERTAIN']} unconfirmed · {ICON['PENDING']} {n['PENDING']} waiting")
        c.add("Planned total", fmt.amount_lines(json.loads(b["totals_json"])) + f"\n{icons.status('value')} **Current Market Value:** {fmt.dollars(b['value_cents'])}")
        c.add("Actually sent", fmt.amount_lines(s["sent"]))
        if n["UNCERTAIN"]:
            c.add(f"{icons.status('warn')} Unconfirmed", "Their funds stay on hold. Settle each with `/bank resolvetx`; nothing is sent twice.")
        if n["PENDING"]:
            c.add(f"{icons.status('wait')} Waiting", f"`/bulk resume batch_id:{b['id']}` sends the rest (already-sent rows are never repeated).")
        return c

    def item_pages(batch_id: int):
        with svc.db.read() as conn:
            b, items = B.load_batch(conn, batch_id)
            names = {r["nation_id"]: r["nation_name"] for r in conn.execute("SELECT nation_id, nation_name FROM members")}
        lines = []
        for it in items:
            nm = names.get(it["nation_id"]) or "Nation"
            tail = f" · tx #{it['tx_id']}" if it["tx_id"] else ""
            msg = f"\n-# {it['message']}" if it["status"] in ("FAILED", "UNCERTAIN") and it["message"] else ""
            lines.append(f"{ICON[it['status']]} **Row {it['row_no']}** · {nm} [#{it['nation_id']}]\n"
                         f"{fmt.short_amounts(json.loads(it['amounts_json']))}{tail}{msg}")
        return chunk_cards(f"{icons.status('withdraw')} Bulk #{batch_id} · item results", lines, per_page=8)

    async def show_result(interaction, batch_id: int, halted_why: str | None = None):
        card = summary_card(batch_id)
        if halted_why:
            card.add(f"{icons.status('warn')} Stopped early", halted_why)

        async def b_items(i):
            await thinking(i)
            await paginate(i, item_pages(batch_id))

        async def b_resume(i):
            await resume.callback(i, batch_id)
        actions = [("Item results", "📋", "primary", b_items)]
        with svc.db.read() as conn:
            st = conn.execute("SELECT status FROM bulk_batches WHERE id=?", (batch_id,)).fetchone()
        if st and st["status"] in ("HALTED", "RUNNING"):
            actions.append(("Resume", "▶️", "success", b_resume))
        await reply(interaction, card=card, view=ActionView(interaction.user.id, actions))
        await svc.alerts.econ(card)
        await svc.alerts.flush_events()

    # ------------------------------------------------------------- commands
    @bulk.command(name="template", description="ECON: download an example bulk transfer file")
    async def template(interaction: discord.Interaction):
        if not await need(svc, interaction, "BANKER"):
            return
        await reply(interaction, "Fill this in (one row per nation). `nation` can be an id, name or link. "
                                 "Then run `/bulk send`. Bulk uses **alliance-owned** funds only.",
                    file=discord.File(io.BytesIO(B.TEMPLATE.encode()), filename="bulk_template.csv"))

    @bulk.command(name="send", description="ECON: pay many nations from alliance funds using a CSV/Excel list")
    @app_commands.describe(file="CSV or Excel: nation + one column per resource (+ optional note)", reason="Why this payment")
    async def send(interaction: discord.Interaction, file: discord.Attachment, reason: str):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        if file.size > importer.MAX_BYTES:
            return await reply(interaction, "That file is too large (max 5 MB).")
        data = await file.read()
        parsed = await asyncio.to_thread(B.parse, file.filename, data)
        errors = list(parsed.errors)

        roster = dict(svc.scanner.roster or {})
        if not roster:
            try:
                roster = await svc.pnw.fetch_alliance_members()
                svc.scanner.roster = dict(roster)
            except (PnWRejected, PnWUncertain):
                roster = {}
        if parsed.rows and not roster:
            errors.append("I could not read the alliance member list from PnW, so destinations can't be validated. Try again in a minute.")
        seen: dict = {}
        for row in parsed.rows:
            try:
                row.nation_id = await resolve(svc, row.nation_text)
            except ResolveError as exc:
                errors.append(f"Row {row.row_no}: {exc}")
                continue
            if roster and row.nation_id not in roster:
                errors.append(f"Row {row.row_no}: nation #{row.nation_id} is not in the alliance.")
            if row.nation_id in seen:
                errors.append(f"Row {row.row_no}: nation #{row.nation_id} is already on row {seen[row.nation_id]}; combine them into one row.")
            seen.setdefault(row.nation_id, row.row_no)
            row.nation_label = f"{roster.get(row.nation_id) or row.nation_text} [#{row.nation_id}]"

        def fail_card():
            c = A.Card(f"{icons.status('bad')} Bulk transfer blocked", "Nothing was sent. Fix the file and upload it again.", A.RED)
            c.add(f"Problems ({len(errors)})", "\n".join(errors[:12]) + (f"\n…and {len(errors) - 12} more" if len(errors) > 12 else ""))
            return c
        if errors:
            return await reply(interaction, card=fail_card())

        totals = B.totals_of(parsed.rows)
        try:
            snap, val, alliance_free = await svc.wd.prepare(funding_source="ALLIANCE", amounts=totals)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I can't read the live PnW bank, so nothing can be sent: {exc}")
        actor = str(interaction.user.id)
        with svc.db.read() as conn:
            held = L.holds(conn, None, "ALLIANCE")
            free = M.sub(alliance_free, held)
            for r, need_units in totals.items():
                if free.get(r, 0) < need_units:
                    errors.append(f"Alliance-owned {M.LABELS[r]} is short: {M.fmt_units(r, max(free.get(r, 0), 0))} available, "
                                  f"{M.fmt_units(r, need_units)} needed.")
            for row in parsed.rows:
                try:
                    LIM.check(conn, is_self=False, nation_id=None, actor_id=actor, actor_role_ids=role_ids(interaction),
                              amounts=row.amounts, valuation=value_amounts(row.amounts, snap))
                except LIM.LimitExceeded as exc:
                    errors.append(f"Row {row.row_no}: {exc}")
            try:
                LIM.check(conn, is_self=False, nation_id=None, actor_id=actor, actor_role_ids=role_ids(interaction),
                          amounts=totals, valuation=val, skip_per_tx=True)
            except LIM.LimitExceeded as exc:
                errors.append(f"Whole batch: {exc}")
            dup = conn.execute("SELECT id FROM bulk_batches WHERE source_sha256=? AND created_at>=datetime('now','-1 day')",
                               (parsed.sha256,)).fetchone()
            if dup:
                errors.append(f"This exact file was already sent as batch #{dup['id']} in the last 24 hours. "
                              "Change the file if you really mean to pay again.")
            threshold = cfg_int(conn, "approval_threshold_value") * 100
        if errors:
            return await reply(interaction, card=fail_card())

        approval_id = approver = None
        if threshold and (val.total_cents is None or val.total_cents > threshold):
            payload = {"bulk_sha": parsed.sha256, "totals": totals, "rows": len(parsed.rows)}

            def ask():
                with svc.db.tx() as conn:
                    return L.find_or_request_approval(conn, "ECON_WITHDRAW", actor, payload, f"bulk: {reason}")
            approval_id, approver = await asyncio.to_thread(ask)
            if not approver:
                c = A.Card(f"{icons.status('wait')} Bulk transfer needs a second approver",
                           f"Worth {fmt.dollars(val.total_cents)} across {len(parsed.rows)} nations.", A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by <@{actor}>")
                c.add("Total", fmt.amounts_with_value(totals, val))
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} was posted. After a different Minister "
                                                "approves it, run this same command with the same file.")

        card = A.Card(f"{icons.status('withdraw')} Confirm BULK transfer", "Nothing is sent until you press Confirm. "
                      "Paid from **ALLIANCE-OWNED** funds.", A.ORANGE)
        card.add("Payments", f"{len(parsed.rows)} nations · file `{file.filename}`", True)
        card.add("Reason", reason, True)
        card.add("Total", fmt.amounts_with_value(totals, val))
        card.add("Alliance-owned left afterwards", fmt.amount_lines(M.sub(free, totals)))
        preview = parsed.rows[:8]
        card.add("First rows", "\n".join(f"**{r.nation_label}** — {fmt.short_amounts(r.amounts)}" for r in preview)
                 + (f"\n…and {len(parsed.rows) - 8} more" if len(parsed.rows) > 8 else ""))
        if approver:
            card.add("Second approver", f"<@{approver}>", True)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")

        def create():
            with svc.db.tx() as conn:
                again = conn.execute("SELECT id FROM bulk_batches WHERE source_sha256=? AND created_at>=datetime('now','-1 day')",
                                     (parsed.sha256,)).fetchone()
                if again:
                    return None
                cur = conn.execute(
                    "INSERT INTO bulk_batches(created_at,actor,source_filename,source_sha256,reason,row_count,totals_json,"
                    "value_cents,price_snapshot_id,status,approval_id) VALUES(?,?,?,?,?,?,?,?,?,'RUNNING',?)",
                    (B.now_iso(), actor, file.filename, parsed.sha256, reason, len(parsed.rows), B.jdump(totals),
                     val.total_cents, snap.id if snap else None, approval_id))
                bid = cur.lastrowid
                for r in parsed.rows:
                    conn.execute("INSERT INTO bulk_items(batch_id,row_no,nation_id,amounts_json,note) VALUES(?,?,?,?,?)",
                                 (bid, r.row_no, r.nation_id, B.jdump(r.amounts), r.note))
                if approval_id:
                    L.finish_approval(conn, approval_id, "EXECUTED", {"batch": bid})
                L.audit(conn, actor, "BULK_BATCH_CREATED", f"bulk:{bid}", {"rows": len(parsed.rows), "totals": totals, "reason": reason})
                return bid
        batch_id = await asyncio.to_thread(create)
        if batch_id is None:
            return await reply(interaction, "This file was just sent by someone else. Nothing was repeated.")
        result = await B.run_batch(svc, batch_id, actor, role_ids(interaction))
        await show_result(interaction, batch_id, result.get("halted_why") or result.get("error"))

    @bulk.command(name="status", description="ECON: results of a bulk transfer (leave empty to list recent ones)")
    async def status(interaction: discord.Interaction, batch_id: int = 0):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        if batch_id:
            def sync():
                with svc.db.tx() as conn:
                    B.refresh_items(conn, batch_id)
            await asyncio.to_thread(sync)
            return await show_result(interaction, batch_id)
        with svc.db.read() as conn:
            rows = conn.execute("SELECT * FROM bulk_batches ORDER BY id DESC LIMIT 20").fetchall()
        lines = [f"{BATCH_ICON.get(r['status'], '')} **#{r['id']}** · {r['status'].title()} · {r['row_count']} nations · "
                 f"{fmt.dollars(r['value_cents'])}\n-# {r['reason'][:60]} · by <@{r['actor']}> · {r['created_at'][:16].replace('T', ' ')}"
                 for r in rows]
        await paginate(interaction, chunk_cards(f"{icons.status('withdraw')} Bulk transfers", lines,
                                                empty="No bulk transfers yet.", per_page=8))

    @bulk.command(name="resume", description="ECON: continue a stopped bulk transfer (rows already sent are never repeated)")
    async def resume(interaction: discord.Interaction, batch_id: int):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            b = conn.execute("SELECT status FROM bulk_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            return await reply(interaction, f"Batch #{batch_id} does not exist.")
        if b["status"] not in ("HALTED", "RUNNING"):
            return await reply(interaction, f"Batch #{batch_id} is {b['status'].title()}; nothing is waiting to be sent.")

        def sync():
            with svc.db.tx() as conn:
                B.refresh_items(conn, batch_id)
        await asyncio.to_thread(sync)
        result = await B.run_batch(svc, batch_id, str(interaction.user.id), role_ids(interaction))
        await show_result(interaction, batch_id, result.get("halted_why") or result.get("error"))
