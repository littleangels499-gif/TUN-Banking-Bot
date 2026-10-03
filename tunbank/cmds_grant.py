"""/grant - alliance-approved expenditures. A grant is NOT a deposit and NOT a loan: it is alliance money spent
for an alliance purpose, sent through the normal confirmed, audited PnW transfer."""
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
from . import resolve as RS
from .buttons import ActionView
from .config import cfg_get, cfg_int
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, chunk_cards, confirm, has_flag, nation_arg, need, paginate, reply, role_ids, thinking
from .util import jdump, now_iso
from .valuation import value_amounts

log = logging.getLogger("tunbank.grant")
ICON = {"COMPLETED": "✅", "PENDING": "⏳", "UNCERTAIN": "⚠️", "FAILED": "⛔"}


def sync_grants(conn):
    """Bring grant status in line with the real transaction behind it."""
    for g in conn.execute("SELECT * FROM grants WHERE tx_id IS NOT NULL AND status IN ('PENDING','UNCERTAIN')").fetchall():
        tx = conn.execute("SELECT status, pnw_record_id, failure_reason FROM transactions WHERE id=?", (g["tx_id"],)).fetchone()
        new = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "CANCELLED": "FAILED", "RECONCILIATION_REQUIRED": "UNCERTAIN"}.get(tx["status"])
        if new and new != g["status"]:
            conn.execute("UPDATE grants SET status=?, pnw_record_id=?, message=?, completed_at=? WHERE id=?",
                         (new, tx["pnw_record_id"], (tx["failure_reason"] or None), now_iso() if new == "COMPLETED" else None, g["id"]))


def register(grant: app_commands.Group, svc: Services):
    def card_for(g) -> A.Card:
        amounts = json.loads(g["amounts_json"])
        c = A.Card(f"{ICON.get(g['status'], '')} Grant #{g['id']} · {g['status'].title()}", g["purpose"],
                   {"COMPLETED": A.GREEN, "FAILED": A.RED}.get(g["status"], A.ORANGE), kind="GRANT")
        with svc.db.read() as conn:
            who = RS.label(conn, g["recipient_nation_id"])
        c.add("Recipient", who, True)
        c.add("Date", g["created_at"][:16].replace("T", " "), True)
        c.add("Project", g["project"] or "—", True)
        c.add("Amount", fmt.amount_lines(amounts) + f"\n{icons.status('value')} **Current Market Value at the time:** {fmt.dollars(g['value_cents'])}")
        c.add("Requested by", f"<@{g['requested_by']}>", True)
        c.add("Approved by", f"<@{g['approver']}>" if g["approver"] else "— (below the second-approver threshold)", True)
        c.add("PnW transaction", f"tx #{g['tx_id']}" + (f" · PnW record #{g['pnw_record_id']}" if g["pnw_record_id"] else ""), True)
        if g["message"] and g["status"] in ("FAILED", "UNCERTAIN"):
            c.add("Note", g["message"])
        return c

    @grant.command(name="send", description="ECON: give an alliance-approved grant from alliance-owned funds")
    @app_commands.describe(nation="Recipient: id, name, link or @user", amounts="e.g. money=50m steel=2000",
                           purpose="Why the alliance is paying for this (required)", project="Optional: the project, e.g. Iron Dome")
    async def send(interaction: discord.Interaction, nation: str, amounts: str, purpose: str, project: str = ""):
        with svc.db.read() as conn:
            level = (cfg_get(conn, "grant_min_level") or "MINISTER").strip().upper()
        if not await need(svc, interaction, level if level in ("BANKER", "MINISTER", "ADMIN") else "MINISTER"):
            return
        await thinking(interaction)
        if not purpose.strip():
            return await reply(interaction, "A grant needs a purpose: say why the alliance is paying for this.")
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        try:
            parsed = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        try:
            snap, val, _ = await svc.wd.prepare(funding_source="ALLIANCE", amounts=parsed)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I can't read the live PnW banks, so nothing was sent: {exc}")
        actor = str(interaction.user.id)
        approval_id = approver = None
        with svc.db.read() as conn:
            threshold = cfg_int(conn, "approval_threshold_value") * 100
            label = RS.label(conn, nid)
        if threshold and (val.total_cents is None or val.total_cents > threshold):
            payload = {"grant_to": nid, "amounts": parsed, "purpose": purpose}

            def ask():
                with svc.db.tx() as conn:
                    return L.find_or_request_approval(conn, "ECON_WITHDRAW", actor, payload, f"grant: {purpose}")
            approval_id, approver = await asyncio.to_thread(ask)
            if not approver:
                c = A.Card(f"{icons.status('wait')} Grant needs a second approving officer",
                           f"{fmt.dollars(val.total_cents)} to {label}.", A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by <@{actor}>")
                c.add("Purpose", purpose)
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} was posted. After a different Minister approves it, run this same command again.")
        card = A.Card("🎁 Confirm GRANT", "An alliance expenditure for an alliance purpose. It is **not** a deposit and **not** a loan. "
                      "Nothing is sent until you press Confirm.", A.ORANGE)
        card.add("Recipient", label, True)
        card.add("Funding", "ALLIANCE-OWNED funds", True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        card.add("Purpose", purpose + (f"\n-# Project: {project}" if project else ""))
        if approver:
            card.add("Second approving officer", f"<@{approver}>", True)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")

        def create():
            with svc.db.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO grants(created_at,recipient_nation_id,amounts_json,purpose,project,requested_by,approver,"
                    "value_cents,price_snapshot_id,status) VALUES(?,?,?,?,?,?,?,?,?,'PENDING')",
                    (now_iso(), nid, jdump(parsed), purpose.strip(), project.strip() or None, actor, approver,
                     val.total_cents, snap.id if snap else None))
                if approval_id:
                    L.finish_approval(conn, approval_id, "EXECUTED", {"grant": cur.lastrowid})
                L.audit(conn, actor, "GRANT_CREATED", f"grant:{cur.lastrowid}", {"to": nid, "amounts": parsed, "purpose": purpose, "approver": approver})
                return cur.lastrowid
        gid = await asyncio.to_thread(create)
        res = await svc.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None, dest_nation_id=nid,
            amounts=parsed, actor=actor, note=f"Grant: {purpose.strip()[:60]}", reason=f"grant #{gid}: {purpose.strip()}",
            idempotency_key=f"grant-{gid}", actor_role_ids=role_ids(interaction), approver=approver,
            reveal_treasury=has_flag(svc, interaction, "bank_view_alliance_holdings"))
        status = {"COMPLETED": "COMPLETED", "UNCERTAIN": "UNCERTAIN"}.get(res.status, "FAILED")

        def save():
            with svc.db.tx() as conn:
                conn.execute("UPDATE grants SET status=?, tx_id=?, pnw_record_id=?, message=?, completed_at=? WHERE id=?",
                             (status, res.tx_id, res.pnw_record_id, (res.message or "")[:300],
                              now_iso() if status == "COMPLETED" else None, gid))
                row = conn.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
                return dict(row)
        g = await asyncio.to_thread(save)
        out = card_for(g)

        async def b_all(i):
            await thinking(i)
            await listing(i)
        await reply(interaction, card=out, view=ActionView(interaction.user.id, [("All grants", "📋", "secondary", b_all)]))
        await svc.alerts.econ(out)
        await svc.alerts.flush_events()

    async def listing(interaction):
        def load():
            with svc.db.tx() as conn:
                sync_grants(conn)
                return [dict(r) for r in conn.execute("SELECT * FROM grants ORDER BY id DESC LIMIT 40")]
        rows = await asyncio.to_thread(load)
        lines = [f"{ICON.get(g['status'], '')} **#{g['id']}** · {g['status'].title()} · {fmt.dollars(g['value_cents'])}\n"
                 f"-# to [#{g['recipient_nation_id']}] · {g['purpose'][:70]} · {g['created_at'][:10]}" for g in rows]
        await paginate(interaction, chunk_cards("🎁 Grants", lines, empty="No grants yet.", per_page=8))

    @grant.command(name="list", description="ECON: all grants, newest first")
    async def list_(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        await listing(interaction)

    @grant.command(name="view", description="ECON: the full record of one grant")
    async def view(interaction: discord.Interaction, grant_id: int):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)

        def load():
            with svc.db.tx() as conn:
                sync_grants(conn)
                r = conn.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
                return dict(r) if r else None
        g = await asyncio.to_thread(load)
        if not g:
            return await reply(interaction, f"Grant #{grant_id} does not exist.")
        await reply(interaction, card=card_for(g))
