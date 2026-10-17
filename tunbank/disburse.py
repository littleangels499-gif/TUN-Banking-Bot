"""Disbursements: /transfer raws and /grant warchest|city|infrastructure|land.

This module adds NO new way to move money. Every recipient is paid through the existing safe pipeline
(WithdrawalService.request: validate -> hold -> call PnW once -> verify -> book the ledger in one transaction), so every
guarantee of normal withdrawals applies: a transfer reaches PnW at most once, an uncertain result is never guessed, a failed
transfer never changes a balance, limits and the emergency lock apply. What this module adds is the CALCULATION, the
SOURCE VALIDATION (all-or-nothing, before anything is sent), and a durable record of the inputs and outcome of every recipient.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from . import ledger as L
from . import limits as LIM
from . import money as M
from .config import cfg_bool, cfg_get, cfg_int
from .pnw import PnWRejected, PnWUncertain
from .util import jdump, now_iso
from .valuation import value_amounts

log = logging.getLogger("tunbank.disburse")

KINDS = ("RAWS", "WARCHEST_GRANT", "CITY_GRANT", "INFRA_GRANT", "LAND_GRANT")
LABEL = {"RAWS": "Raws (operating upkeep)", "WARCHEST_GRANT": "Warchest grant", "CITY_GRANT": "City grant",
         "INFRA_GRANT": "Infrastructure grant", "LAND_GRANT": "Land grant"}
SOURCES = ("ALLIANCE", "MEMBER_AVAILABLE", "MEMBER_LOCKED")
SYSTEMIC = ("EMERGENCY LOCK", "paused", "RECONCILIATION REQUIRED", "Could not read the live PnW bank")
_RUNNING: set = set()
DUPLICATE_WINDOW_MINUTES = 10


class DisburseError(L.LedgerError):
    pass


@dataclass
class Source:
    kind: str                       # ALLIANCE | MEMBER_AVAILABLE | MEMBER_LOCKED
    member_id: int | None = None    # the funder for member sources; None = each recipient pays from their OWN account
    lock_id: int | None = None
    description: str = ""           # shown on the confirmation screen and stored with the batch

    def funder_for(self, nation_id: int) -> int | None:
        if self.kind == "ALLIANCE":
            return None
        return self.member_id or nation_id


# ------------------------------------------------------------------------------------------------ switches
def live_kinds(conn) -> set:
    return {k.strip().upper() for k in (cfg_get(conn, "disburse_live_kinds") or "").split(",") if k.strip()}


def is_live(conn, kind: str) -> bool:
    return kind in live_kinds(conn)


def set_live(conn, kind: str, on: bool, actor: str) -> None:
    from .config import cfg_set
    cur = live_kinds(conn)
    (cur.add if on else cur.discard)(kind)
    cfg_set(conn, "disburse_live_kinds", ",".join(sorted(k for k in KINDS if k in cur)), actor)


# ------------------------------------------------------------------------------------------------ calculation
def totals_of(calcs) -> dict:
    out: dict = {}
    for c in calcs:
        if c.sendable:
            out = M.add(out, c.amounts)
    return out


def signature(calcs) -> str:
    """Identity of what would be sent. If it differs between the preview and the confirmation, nothing is sent."""
    return hashlib.sha256(json.dumps(sorted((c.nation_id, sorted(c.amounts.items()), bool(c.sendable)) for c in calcs)).encode()).hexdigest()


def apply_caps(conn, kind: str, calcs) -> None:
    """ECON-configured per-nation caps: a bigger calculation is flagged for review, never silently reduced and never sent."""
    key = {"CITY_GRANT": "grant_city_max_usd", "INFRA_GRANT": "grant_infra_max_usd", "LAND_GRANT": "grant_land_max_usd"}.get(kind)
    cap = cfg_int(conn, key) if key else 0
    if not cap:
        return
    for c in calcs:
        if c.sendable and c.amounts.get("money", 0) > cap * M.SCALE:
            c.review.append(f"the calculated ${c.amounts['money'] // M.SCALE:,} is over the ${cap:,} cap for this grant type ({key})")


def describe_source(svc, conn, source: Source, label_of) -> str:
    """The exact words for 'whose funds are being spent'."""
    if source.kind == "ALLIANCE":
        from . import offshore_ledger as OL
        bank = svc.settings.payout.name
        if svc.settings.offshore is not None and svc.settings.payout is svc.settings.offshore and OL.shared_enabled(conn):
            return f"ALLIANCE-OWNED funds · paid from the {bank} bank, this alliance's ownership share (the share of the other alliances is never touched)"
        return f"ALLIANCE-OWNED funds · paid from the {bank} bank"
    bucket = "AVAILABLE deposit" if source.kind == "MEMBER_AVAILABLE" else f"LOCKED funds (lock #{source.lock_id})"
    who = label_of(source.member_id) if source.member_id else "each recipient's OWN"
    return f"MEMBER funds · {who} {bucket} (this is the member's money, not alliance money)"


async def check_source(svc, source: Source, calcs, *, actor: str, role_ids, reveal: bool, alliance_funds_ok: bool = True):
    """Validate the funding source for the WHOLE operation before anything is sent. Returns (errors, snap, valuation).

    All-or-nothing: if any source is short or unusable the operation is refused as a whole, so a failed or insufficient source can
    never cause a partial transfer or create funds.
    """
    sendable = [c for c in calcs if c.sendable and c.amounts]
    totals = totals_of(sendable)
    errors: list = []
    if not sendable:
        return ["There is nothing to send."], None, None
    try:
        snap, val, alliance_free = await svc.wd.prepare(funding_source=source.kind, amounts=totals)
    except (PnWRejected, PnWUncertain) as exc:
        return [f"I can't read the live PnW bank, so nothing can be sent: {exc}"], None, None
    physical = None
    if source.kind == "ALLIANCE":
        try:
            physical = await svc.pnw.fetch_bank_holdings(svc.settings.payout)
        except (PnWRejected, PnWUncertain) as exc:
            return [f"I can't read the {svc.settings.payout.name} bank, so nothing can be sent: {exc}"], snap, val
    with svc.db.read() as conn:
        # global safeguards: emergency lock, ECON pause, reconciliation gate, per funder
        funders = {source.funder_for(c.nation_id) for c in sendable}
        for f in funders:
            try:
                L.assert_can_mutate(conn, f, "withdraw")
            except L.LedgerError as exc:
                errors.append(str(exc))
                break
        if source.kind == "ALLIANCE":
            free = M.sub(alliance_free or {}, L.holds(conn, None, "ALLIANCE"))
            for r, need_units in totals.items():
                if free.get(r, 0) < need_units:
                    errors.append(f"Alliance-owned {M.LABELS[r]} is short: {M.fmt_units(r, max(free.get(r, 0), 0))} available, "
                                  f"{M.fmt_units(r, need_units)} needed." if reveal else
                                  f"Alliance-owned {M.LABELS[r]} is not sufficient for this operation.")
            for r, need_units in totals.items():
                if (physical or {}).get(r, 0) < need_units:
                    errors.append(f"The {svc.settings.payout.name} bank does not physically hold enough {M.LABELS[r]} right now."
                                  + (" ECON can move funds there with /bank offshore." if svc.settings.offshore else ""))
        elif source.kind == "MEMBER_AVAILABLE":
            need_by: dict = {}
            for c in sendable:
                f = source.funder_for(c.nation_id)
                need_by[f] = M.add(need_by.get(f, {}), c.amounts)
            for f, need_units in need_by.items():
                if not L.get_member(conn, f):
                    errors.append(f"Nation #{f} has no TUN Bank account, so there is no balance to spend.")
                    continue
                have = L.spendable(conn, f)
                for r, n in need_units.items():
                    if have.get(r, 0) < n:
                        errors.append(f"Nation #{f}: available {M.LABELS[r]} is {M.fmt_units(r, max(have.get(r, 0), 0))}, needs {M.fmt_units(r, n)}.")
        elif source.kind == "MEMBER_LOCKED":
            if len(sendable) != 1 or not source.lock_id:
                errors.append("Locked funds can only pay ONE recipient, from one named lock (give lock_id).")
            else:
                c = sendable[0]
                f = source.funder_for(c.nation_id)
                lock = conn.execute("SELECT nation_id FROM locks WHERE id=?", (source.lock_id,)).fetchone()
                if not lock or lock["nation_id"] != f:
                    errors.append(f"Lock #{source.lock_id} does not belong to nation #{f}.")
                else:
                    have = L.lock_spendable(conn, source.lock_id, f)
                    for r, n in c.amounts.items():
                        if have.get(r, 0) < n:
                            errors.append(f"Lock #{source.lock_id}: {M.LABELS[r]} left is {M.fmt_units(r, max(have.get(r, 0), 0))}, needs {M.fmt_units(r, n)}.")
        # the operator's own limits, per recipient and for the whole operation
        for c in sendable:
            try:
                LIM.check(conn, is_self=False, nation_id=source.funder_for(c.nation_id), actor_id=actor, actor_role_ids=role_ids,
                          amounts=c.amounts, valuation=value_amounts(c.amounts, snap))
            except LIM.LimitExceeded as exc:
                errors.append(f"Nation #{c.nation_id}: {exc}")
        try:
            LIM.check(conn, is_self=False, nation_id=None, actor_id=actor, actor_role_ids=role_ids, amounts=totals, valuation=val,
                      skip_per_tx=True)
        except LIM.LimitExceeded as exc:
            errors.append(f"Whole operation: {exc}")
    return errors, snap, val


# ------------------------------------------------------------------------------------------------ persistence
def content_hash(kind: str, source: Source, note: str, calcs) -> str:
    return hashlib.sha256(json.dumps([kind, source.kind, source.member_id, source.lock_id, note.strip(),
                                      sorted((c.nation_id, sorted(c.amounts.items())) for c in calcs if c.sendable)]).encode()).hexdigest()


def recent_duplicate(conn, sha: str):
    return conn.execute("SELECT id FROM disbursement_batches WHERE content_sha256=? AND status IN ('RUNNING','COMPLETED','PARTIAL','HALTED') "
                        "AND created_at >= ?", (sha, _minutes_ago(DUPLICATE_WINDOW_MINUTES))).fetchone()


def _minutes_ago(minutes: int) -> str:
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_batch(conn, *, kind: str, calcs, source: Source, actor: str, note: str, params: dict, rules_version: str, paying_bank: str,
                 val, snap, approval_id, request_key: str):
    """Write the batch and one item per recipient (sendable ones PENDING, flagged ones REVIEW) in one transaction.

    Returns the batch id, or None if this exact request already created a batch.
    """
    if conn.execute("SELECT 1 FROM disbursement_batches WHERE request_key=?", (request_key,)).fetchone():
        return None
    sha = content_hash(kind, source, note, calcs)
    if recent_duplicate(conn, sha):
        raise DisburseError(f"An identical {LABEL[kind].lower()} was already created in the last {DUPLICATE_WINDOW_MINUTES} minutes "
                            f"(batch #{recent_duplicate(conn, sha)['id']}). Nothing was repeated; change the note if you really mean to send it again.")
    totals = totals_of(calcs)
    cur = conn.execute(
        "INSERT INTO disbursement_batches(created_at,kind,actor,funding_source,source_member_id,source_description,paying_bank,note,"
        "parameters_json,rules_version,totals_json,value_cents,price_snapshot_id,approval_id,content_sha256,request_key,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RUNNING')",
        (now_iso(), kind, actor, source.kind, source.member_id, source.description, paying_bank, note.strip(), jdump(params),
         rules_version, jdump(totals), getattr(val, "total_cents", None), getattr(snap, "id", None), approval_id, sha, request_key))
    bid = cur.lastrowid
    for seq, c in enumerate(calcs, start=1):
        sendable = c.sendable and bool(c.amounts)
        conn.execute(
            "INSERT INTO disbursement_items(batch_id,seq,nation_id,kind,amounts_json,requirement_json,stock_json,calculation_json,"
            "source_member_id,lock_id,status,message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, seq, c.nation_id, kind, jdump(c.amounts if c.sendable else {}), jdump(c.requirement), jdump(c.stock) if c.stock else None,
             jdump({"lines": c.lines, "inputs": c.inputs, "assumptions": c.assumptions, "estimate": c.estimate, "review": c.review}),
             source.funder_for(c.nation_id), source.lock_id if source.kind == "MEMBER_LOCKED" else None,
             "PENDING" if sendable else "REVIEW",
             None if sendable else ("; ".join(c.review) if c.review else "nothing to send")))
    if approval_id:
        L.finish_approval(conn, approval_id, "EXECUTED", {"disbursement": bid})
    L.audit(conn, actor, "DISBURSEMENT_CREATED", f"disbursement:{bid}", {
        "kind": kind, "source": source.kind, "source_member": source.member_id, "source_description": source.description,
        "recipients": [c.nation_id for c in calcs if c.sendable and c.amounts], "left_for_review": [c.nation_id for c in calcs if not c.sendable],
        "totals": totals, "note": note, "params": params, "rules": rules_version, "approval_id": approval_id})
    return bid


def load(conn, batch_id: int):
    b = conn.execute("SELECT * FROM disbursement_batches WHERE id=?", (batch_id,)).fetchone()
    items = conn.execute("SELECT * FROM disbursement_items WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
    return b, items


def refresh_items(conn, batch_id: int) -> None:
    """Bring item states in line with their real transactions (an uncertain one that later settled)."""
    for it in conn.execute("SELECT * FROM disbursement_items WHERE batch_id=? AND tx_id IS NOT NULL AND status IN ('PENDING','UNCERTAIN')",
                           (batch_id,)).fetchall():
        tx = conn.execute("SELECT status, failure_reason, pnw_record_id FROM transactions WHERE id=?", (it["tx_id"],)).fetchone()
        new = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "CANCELLED": "FAILED", "RECONCILIATION_REQUIRED": "UNCERTAIN"}.get(tx["status"])
        if new and new != it["status"]:
            conn.execute("UPDATE disbursement_items SET status=?, message=?, pnw_record_id=? WHERE id=?",
                         (new, (tx["failure_reason"] or "")[:300] or None, tx["pnw_record_id"], it["id"]))


def summarize(conn, batch_id: int) -> dict:
    counts = {"PENDING": 0, "COMPLETED": 0, "FAILED": 0, "UNCERTAIN": 0, "REVIEW": 0}
    sent: dict = {}
    for it in conn.execute("SELECT status, amounts_json FROM disbursement_items WHERE batch_id=?", (batch_id,)):
        counts[it["status"]] += 1
        if it["status"] == "COMPLETED":
            sent = M.add(sent, json.loads(it["amounts_json"]))
    return {"counts": counts, "sent": sent}


def finish(conn, batch_id: int) -> str:
    s = summarize(conn, batch_id)["counts"]
    if s["PENDING"]:
        status = "HALTED"
    elif s["FAILED"] or s["UNCERTAIN"] or s["REVIEW"]:
        status = "FAILED" if not s["COMPLETED"] and not s["UNCERTAIN"] else "PARTIAL"      # never looks fully successful when anything is left out
    else:
        status = "COMPLETED"
    conn.execute("UPDATE disbursement_batches SET status=?, finished_at=? WHERE id=?", (status, now_iso() if status != "HALTED" else None, batch_id))
    L.audit(conn, "system", "DISBURSEMENT_" + status, f"disbursement:{batch_id}", s)
    return status


def pnw_note(conn, note: str, source: Source) -> str:
    """Human note for PnW (the structured classification lives in the database, not in this text). Alliance-funded transfers carry
    the donation note so they are never treated as a member's deposit or withdrawal; member-funded ones never do."""
    base = (note or "TUN Bank").strip()[:70]
    if source.kind == "ALLIANCE" and cfg_bool(conn, "disburse_ignore_tag"):
        tag = (cfg_get(conn, "tag_ignore") or "").strip()
        if tag and tag.lower() not in base.lower():
            base = f"{base} {tag}"
    return base


async def run_batch(svc, batch_id: int, actor: str, role_ids, reveal: bool = False) -> dict:
    """Send every PENDING item, one at a time, through the normal safe withdrawal pipeline. Safe to call again (resume)."""
    import asyncio
    if batch_id in _RUNNING:
        return {"error": "This batch is already running."}
    _RUNNING.add(batch_id)
    try:
        def load_all():
            with svc.db.read() as conn:
                b, items = load(conn, batch_id)
                approver = None
                if b["approval_id"]:
                    ap = L.get_approval(conn, b["approval_id"])
                    approver = ap["approved_by"] if ap else None
                src = Source(b["funding_source"], b["source_member_id"] if b["funding_source"] != "ALLIANCE" else None)
                src.description = b["source_description"]
                return dict(b), [dict(i) for i in items if i["status"] == "PENDING"], approver, src, pnw_note(conn, b["note"], src)
        batch, items, approver, src, note = await asyncio.to_thread(load_all)
        halted_why, bad_streak = None, 0
        for it in items:
            amounts = json.loads(it["amounts_json"])
            res = await svc.wd.request(
                tx_type="WITHDRAW_ECON", funding_source=batch["funding_source"], member_nation_id=it["source_member_id"],
                lock_id=it["lock_id"], dest_nation_id=it["nation_id"], amounts=amounts, actor=str(actor), note=note,
                reason=f"{batch['kind']} batch #{batch_id}: {batch['note']}", idempotency_key=f"disb-{batch_id}-{it['seq']}",
                actor_role_ids=role_ids, approver=approver, reveal_treasury=reveal)
            status = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "UNCERTAIN": "UNCERTAIN"}.get(res.status)
            if res.status == "BLOCKED" and any(k in res.message for k in SYSTEMIC):
                halted_why = res.message               # leave the item PENDING so it can be resumed once the cause is fixed
                break
            if status is None:
                status = "FAILED" if res.status == "BLOCKED" else "UNCERTAIN"

            def save(it=it, res=res, status=status):
                with svc.db.tx() as conn:
                    conn.execute("UPDATE disbursement_items SET status=?, tx_id=?, pnw_record_id=?, message=? WHERE id=?",
                                 (status, res.tx_id, res.pnw_record_id, (res.message or "")[:300], it["id"]))
            await asyncio.to_thread(save)
            if status == "UNCERTAIN":
                halted_why = "A transfer's result is not confirmed yet; stopping so nothing is sent twice."
                break
            bad_streak = bad_streak + 1 if status == "FAILED" else 0
            if bad_streak >= 3:
                halted_why = "Three transfers in a row failed; stopping so you can look into it."
                break

        def done():
            with svc.db.tx() as conn:
                refresh_items(conn, batch_id)
                st = finish(conn, batch_id)
                return st, summarize(conn, batch_id)
        status, summary = await asyncio.to_thread(done)
        return {"status": status, "summary": summary, "halted_why": halted_why}
    finally:
        _RUNNING.discard(batch_id)
