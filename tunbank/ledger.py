"""The ledger: the ONLY place member balances are allowed to change.

Rules enforced here (and again by database triggers, see 001_initial.sql):
  * balance = opening balance + real PnW deposits - real PnW withdrawals
              +/- locks/releases + documented, approved adjustments
  * nothing is credited without a real PnW record or a committed import
  * nothing is marked completed until PnW confirmed the transfer
  * every change is written to a tamper-evident hash chain
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Iterable

from . import money as M
from .config import cfg_bool, cfg_int
from .util import ISO_FMT, jdump, now_iso, parse_iso, sha256_text, utcnow

GENESIS = "GENESIS"
IN_FLIGHT = ("CONFIRMED", "RECONCILIATION_REQUIRED")


class LedgerError(Exception):
    """Something was asked that the accounting rules do not allow."""


class FinancialBlocked(LedgerError):
    """The bank is locked / paused / needs reconciliation."""


class InsufficientFunds(LedgerError):
    pass


# ----------------------------------------------------------------- state
def get_state(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now_iso()),
    )


# ----------------------------------------------------------------- audit
def _last_hash(conn, table: str) -> str:
    row = conn.execute(f"SELECT entry_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return row["entry_hash"] if row else GENESIS


def audit(conn, actor: str, action: str, target: str | None = None, details: dict | None = None):
    actor = str(actor)  # hash and stored value must be identical (ints would hash differently)
    prev = _last_hash(conn, "audit_log")
    ts = now_iso()
    det = jdump(details or {})
    h = sha256_text(jdump([prev, ts, actor, action, target, det]))
    conn.execute(
        "INSERT INTO audit_log(ts,actor,action,target,details_json,prev_hash,entry_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts, actor, action, target, det, prev, h),
    )


def _ledger_hash(prev: str, r: dict) -> str:
    return sha256_text(
        jdump([
            prev, r["ts"], r["group_id"], r["nation_id"], r["bucket"], r["resource"],
            r["delta"], r["entry_type"], r.get("pnw_record_id"), r.get("tx_id"),
            r.get("lock_id"), r.get("batch_id"), r.get("adjustment_id"), r["actor"],
            r.get("note"),
        ])
    )


def verify_chain(conn, table: str) -> dict:
    """Re-compute the whole hash chain. Detects edited or deleted history."""
    prev = GENESIS
    count = 0
    head_id = None
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
        d = dict(row)
        if d["prev_hash"] != prev:
            return {"ok": False, "bad_id": d["id"], "count": count, "reason": "prev_hash mismatch"}
        if table == "ledger_entries":
            expect = _ledger_hash(prev, d)
        else:
            expect = sha256_text(
                jdump([prev, d["ts"], d["actor"], d["action"], d["target"], d["details_json"]])
            )
        if expect != d["entry_hash"]:
            return {"ok": False, "bad_id": d["id"], "count": count, "reason": "content hash mismatch"}
        prev = d["entry_hash"]
        head_id = d["id"]
        count += 1
    return {"ok": True, "count": count, "head_id": head_id, "head_hash": prev}


# ------------------------------------------------------- integrity events
def raise_event(conn, severity: str, kind: str, *, nation_id=None, ref_type=None,
                ref_id=None, details=None, dedupe_key=None):
    """Record an anomaly. Returns (event_id, is_new). CRITICAL can trigger the lock."""
    if dedupe_key:
        row = conn.execute(
            "SELECT id FROM integrity_events WHERE dedupe_key=? AND status='OPEN'",
            (dedupe_key,),
        ).fetchone()
        if row:
            return row["id"], False
    cur = conn.execute(
        "INSERT INTO integrity_events(ts,severity,kind,nation_id,ref_type,ref_id,"
        "details_json,dedupe_key) VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), severity, kind, nation_id, ref_type,
         None if ref_id is None else str(ref_id), jdump(details or {}), dedupe_key),
    )
    eid = cur.lastrowid
    audit(conn, "system", "INTEGRITY_EVENT", f"event:{eid}",
          {"severity": severity, "kind": kind, "nation_id": nation_id, "ref": ref_id})
    if severity == "CRITICAL" and cfg_bool(conn, "auto_emergency_lock"):
        set_emergency_lock(conn, True, f"Automatic lock: {kind} (event #{eid})", "system")
    return eid, True


def resolve_event(conn, event_id: int, actor: str, note: str) -> bool:
    if not note or not note.strip():
        raise LedgerError("A resolution note is required.")
    cur = conn.execute(
        "UPDATE integrity_events SET status='RESOLVED', resolved_by=?, resolved_at=?, "
        "resolution_note=? WHERE id=? AND status='OPEN'",
        (str(actor), now_iso(), note.strip(), event_id),
    )
    if cur.rowcount:
        audit(conn, actor, "INTEGRITY_EVENT_RESOLVED", f"event:{event_id}", {"note": note})
        from . import configaudit as CA
        CA.record(conn, actor=actor, setting=f"integrity_event:{event_id}", previous="OPEN", new="RESOLVED", target=note, category="SECURITY")
    return bool(cur.rowcount)


def set_emergency_lock(conn, on: bool, reason: str, actor: str):
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting="emergency_financial_lock", previous="ON" if get_state(conn, "emergency_lock") == "1" else "OFF",
              new="ON" if on else "OFF", target=reason or None, category="SECURITY")
    set_state(conn, "emergency_lock", "1" if on else "0")
    set_state(conn, "emergency_reason", reason if on else "")
    audit(conn, actor, "EMERGENCY_LOCK_ON" if on else "EMERGENCY_LOCK_OFF", None, {"reason": reason})


def open_events(conn, limit: int = 50):
    return conn.execute(
        "SELECT * FROM integrity_events WHERE status='OPEN' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def integrity_state(conn) -> dict:
    locked = get_state(conn, "emergency_lock") == "1"
    sev = [r["severity"] for r in conn.execute(
        "SELECT severity FROM integrity_events WHERE status='OPEN'")]
    if locked:
        state = "EMERGENCY_LOCK"
    elif "RECON" in sev or "CRITICAL" in sev:
        state = "RECONCILIATION_REQUIRED"
    elif "WARNING" in sev:
        state = "WARNING"
    else:
        state = "NORMAL"
    return {
        "state": state,
        "emergency_lock": locked,
        "emergency_reason": get_state(conn, "emergency_reason", ""),
        "bank_paused": get_state(conn, "bank_paused") == "1",
        "open_events": len(sev),
    }


def assert_can_mutate(conn, nation_id: int | None, op: str) -> None:
    """Raise FinancialBlocked if this kind of money movement must not happen now.

    op is one of: withdraw, convert, lock, release, adjust, import.
    """
    st = integrity_state(conn)
    if st["emergency_lock"]:
        raise FinancialBlocked(
            "EMERGENCY LOCK is active - all financial changes are halted until "
            f"authorized staff review. Reason: {st['emergency_reason'] or 'not given'}"
        )
    if op in ("withdraw", "convert") and st["bank_paused"]:
        raise FinancialBlocked("Withdrawals and conversions are paused by ECON (/bank unlock to resume)." if op == "convert"
                               else "Withdrawals are paused by ECON (/bank unlock to resume).")
    if op in ("withdraw", "convert", "lock", "import"):
        rows = conn.execute(
            "SELECT id, nation_id, kind FROM integrity_events WHERE status='OPEN' "
            "AND severity IN ('RECON','CRITICAL')"
        ).fetchall()
        for r in rows:
            if r["nation_id"] is None or (nation_id is not None and r["nation_id"] == nation_id):
                raise FinancialBlocked(
                    f"RECONCILIATION REQUIRED: integrity event #{r['id']} ({r['kind']}) "
                    "is open and must be reviewed by ECON before this can proceed."
                )


# --------------------------------------------------------------- members
def ensure_member(conn, nation_id: int, name: str | None = None) -> None:
    conn.execute(
        "INSERT INTO members(nation_id, nation_name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(nation_id) DO UPDATE SET nation_name=COALESCE(excluded.nation_name, nation_name)",
        (nation_id, name, now_iso()),
    )


def get_member(conn, nation_id: int):
    return conn.execute("SELECT * FROM members WHERE nation_id=?", (nation_id,)).fetchone()


def member_by_discord(conn, discord_id) -> object | None:
    return conn.execute("SELECT * FROM members WHERE discord_id=?", (str(discord_id),)).fetchone()


# -------------------------------------------------------------- balances
def get_balances(conn, nation_id: int, bucket: str) -> dict:
    rows = conn.execute(
        "SELECT resource, amount FROM balances WHERE nation_id=? AND bucket=? AND amount!=0",
        (nation_id, bucket),
    ).fetchall()
    return {r["resource"]: r["amount"] for r in rows}


def holds(conn, member_nation_id: int | None, funding_source: str, lock_id: int | None = None) -> dict:
    """Amounts promised to in-flight (not yet finished) withdrawals."""
    sql = (
        "SELECT i.resource, SUM(i.amount) AS amt FROM transactions t "
        "JOIN tx_items i ON i.tx_id=t.id "
        "WHERE t.status IN ('CONFIRMED','RECONCILIATION_REQUIRED') AND t.funding_source=? "
    )
    args: list = [funding_source]
    if member_nation_id is not None:
        sql += "AND t.member_nation_id=? "
        args.append(member_nation_id)
    if lock_id is not None:
        sql += "AND t.lock_id=? "
        args.append(lock_id)
    sql += "GROUP BY i.resource"
    return {r["resource"]: r["amt"] for r in conn.execute(sql, args)}


def spendable(conn, nation_id: int) -> dict:
    return M.sub(get_balances(conn, nation_id, "AVAILABLE"),
                 holds(conn, nation_id, "MEMBER_AVAILABLE"))


def lock_remaining(conn, lock_id: int) -> dict:
    rows = conn.execute(
        "SELECT resource, SUM(delta) AS amt FROM ledger_entries "
        "WHERE lock_id=? AND bucket='LOCKED' GROUP BY resource", (lock_id,)
    ).fetchall()
    return {r["resource"]: r["amt"] for r in rows if r["amt"]}


def lock_spendable(conn, lock_id: int, nation_id: int) -> dict:
    return M.sub(lock_remaining(conn, lock_id), holds(conn, nation_id, "MEMBER_LOCKED", lock_id))


def totals_held(conn) -> dict:
    """Total member-owned funds: {'AVAILABLE': {...}, 'LOCKED': {...}}."""
    out = {"AVAILABLE": {}, "LOCKED": {}}
    for r in conn.execute("SELECT bucket, resource, SUM(amount) AS amt FROM balances GROUP BY bucket, resource"):
        if r["amt"]:
            out[r["bucket"]][r["resource"]] = r["amt"]
    return out


def positive_claims(conn) -> dict:
    """Member-owned funds that physically sit in the alliance bank: the sum of every POSITIVE balance.

    A negative balance is money a member OWES the alliance. It does not make another member's funds smaller, so it
    must never be netted against them when working out what the bank has to hold (see reconcile.bank_position).
    Returns {'AVAILABLE': {...}, 'LOCKED': {...}, 'OWED': {...}}."""
    out = {"AVAILABLE": {}, "LOCKED": {}, "OWED": {}}
    for r in conn.execute("SELECT bucket, resource, amount FROM balances WHERE amount != 0"):
        if r["amount"] > 0:
            out[r["bucket"]][r["resource"]] = out[r["bucket"]].get(r["resource"], 0) + r["amount"]
        else:
            out["OWED"][r["resource"]] = out["OWED"].get(r["resource"], 0) - r["amount"]
    return out


# ------------------------------------------------------------ posting
def new_group(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def post_entries(conn, rows: Iterable[dict]) -> int:
    """Append rows to the ledger inside the CALLER's transaction."""
    if not conn.in_transaction:
        raise LedgerError("post_entries must run inside a database transaction")
    prev = _last_hash(conn, "ledger_entries")
    n = 0
    for r in rows:
        r = dict(r)
        r["ts"] = now_iso()
        r["actor"] = str(r["actor"])
        if r.get("note") is not None:
            r["note"] = str(r["note"])
        h = _ledger_hash(prev, r)
        conn.execute(
            "INSERT INTO ledger_entries(ts,group_id,nation_id,bucket,resource,delta,entry_type,"
            "pnw_record_id,tx_id,lock_id,batch_id,adjustment_id,actor,note,price_snapshot_id,"
            "prev_hash,entry_hash,reset_id,conversion_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["ts"], r["group_id"], r["nation_id"], r["bucket"], r["resource"], r["delta"],
             r["entry_type"], r.get("pnw_record_id"), r.get("tx_id"), r.get("lock_id"),
             r.get("batch_id"), r.get("adjustment_id"), r["actor"], r.get("note"),
             r.get("price_snapshot_id"), prev, h, r.get("reset_id"), r.get("conversion_id")),
        )
        prev = h
        n += 1
    return n


def snapshot_accounts(conn, nation_id: int) -> dict:
    return {
        "available": get_balances(conn, nation_id, "AVAILABLE"),
        "locked": get_balances(conn, nation_id, "LOCKED"),
    }


# --------------------------------------------------------------- deposits
def credit_deposit(conn, *, pnw_record_id: int, nation_id: int, amounts: dict,
                   actor: str, snapshot_id=None, note=None) -> dict:
    """Credit AVAILABLE for a real PnW deposit record. Returns before/after."""
    before = snapshot_accounts(conn, nation_id)
    ensure_member(conn, nation_id)
    g = new_group("DEP")
    post_entries(conn, [
        dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=res, delta=amt,
             entry_type="DEPOSIT", pnw_record_id=pnw_record_id, actor=actor, note=note,
             price_snapshot_id=snapshot_id)
        for res, amt in amounts.items()
    ])
    audit(conn, actor, "DEPOSIT_CREDITED", f"pnw:{pnw_record_id}",
          {"nation_id": nation_id, "amounts": amounts})
    return {"before": before, "after": snapshot_accounts(conn, nation_id)}


# ------------------------------------------------------------------ locks
def create_lock(conn, *, nation_id: int, amounts: dict, lock_type: str, reason: str,
                actor: str, snapshot_id=None) -> dict:
    amounts = M.clean(amounts)
    if not M.is_positive(amounts):
        raise LedgerError("Nothing to reserve.")
    if not reason.strip():
        raise LedgerError("A reason is required to reserve funds.")
    assert_can_mutate(conn, nation_id, "lock")
    avail = spendable(conn, nation_id)
    for res, amt in amounts.items():
        if avail.get(res, 0) < amt:
            raise InsufficientFunds(
                f"{M.LABELS[res]}: member has {M.fmt_units(res, avail.get(res, 0))} "
                f"spendable, tried to reserve {M.fmt_units(res, amt)}."
            )
    before = snapshot_accounts(conn, nation_id)
    cur = conn.execute(
        "INSERT INTO locks(nation_id,lock_type,reason,created_by,created_at,price_snapshot_id) "
        "VALUES(?,?,?,?,?,?)", (nation_id, lock_type, reason.strip(), str(actor), now_iso(), snapshot_id))
    lock_id = cur.lastrowid
    g = new_group("LOCK")
    rows = []
    for res, amt in amounts.items():
        rows.append(dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=res,
                         delta=-amt, entry_type="LOCK", lock_id=lock_id, actor=actor,
                         note=reason, price_snapshot_id=snapshot_id))
        rows.append(dict(group_id=g, nation_id=nation_id, bucket="LOCKED", resource=res,
                         delta=amt, entry_type="LOCK", lock_id=lock_id, actor=actor,
                         note=reason, price_snapshot_id=snapshot_id))
    post_entries(conn, rows)
    audit(conn, actor, "FUNDS_RESERVED", f"lock:{lock_id}",
          {"nation_id": nation_id, "amounts": amounts, "type": lock_type, "reason": reason})
    return {"lock_id": lock_id, "before": before, "after": snapshot_accounts(conn, nation_id)}


def release_lock(conn, *, lock_id: int, amounts: dict | None, reason: str, actor: str,
                 snapshot_id=None) -> dict:
    if not reason.strip():
        raise LedgerError("A reason is required to release funds.")
    lock = conn.execute("SELECT * FROM locks WHERE id=?", (lock_id,)).fetchone()
    if not lock:
        raise LedgerError(f"Lock #{lock_id} does not exist.")
    nation_id = lock["nation_id"]
    assert_can_mutate(conn, nation_id, "release")
    free = lock_spendable(conn, lock_id, nation_id)
    if amounts is None:
        amounts = dict(free)
    amounts = M.clean(amounts)
    if not M.is_positive(amounts):
        raise LedgerError("Nothing left to release in that lock.")
    for res, amt in amounts.items():
        if free.get(res, 0) < amt:
            raise InsufficientFunds(
                f"Lock #{lock_id} only has {M.fmt_units(res, free.get(res, 0))} "
                f"{M.LABELS[res]} releasable (asked {M.fmt_units(res, amt)})."
            )
    before = snapshot_accounts(conn, nation_id)
    g = new_group("REL")
    rows = []
    for res, amt in amounts.items():
        rows.append(dict(group_id=g, nation_id=nation_id, bucket="LOCKED", resource=res,
                         delta=-amt, entry_type="RELEASE", lock_id=lock_id, actor=actor,
                         note=reason, price_snapshot_id=snapshot_id))
        rows.append(dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=res,
                         delta=amt, entry_type="RELEASE", lock_id=lock_id, actor=actor,
                         note=reason, price_snapshot_id=snapshot_id))
    post_entries(conn, rows)
    audit(conn, actor, "FUNDS_RELEASED", f"lock:{lock_id}",
          {"nation_id": nation_id, "amounts": amounts, "reason": reason})
    return {"nation_id": nation_id, "before": before, "after": snapshot_accounts(conn, nation_id)}


# ------------------------------------------------------------ withdrawals
def get_tx(conn, tx_id: int):
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return None, {}
    items = {r["resource"]: r["amount"] for r in conn.execute(
        "SELECT resource, amount FROM tx_items WHERE tx_id=?", (tx_id,))}
    return tx, items


def begin_withdrawal(conn, *, tx_type: str, funding_source: str, member_nation_id: int | None,
                     lock_id: int | None, dest_nation_id: int, amounts: dict, actor: str,
                     note: str, reason: str, idempotency_key: str, snapshot_id=None,
                     value_cents=None, alliance_free: dict | None = None,
                     approver: str | None = None) -> tuple[int, bool]:
    """Validate and reserve (hold) a withdrawal. Nothing is sent to PnW here.

    Returns (tx_id, created). If the idempotency_key was used before, returns the
    existing transaction and created=False, so a repeated click/retry can never
    create a second withdrawal.
    """
    row = conn.execute("SELECT id FROM transactions WHERE idempotency_key=?",
                       (idempotency_key,)).fetchone()
    if row:
        return row["id"], False
    amounts = M.clean(amounts)
    if not M.is_positive(amounts):
        raise LedgerError("Nothing to withdraw.")
    if funding_source not in ("ALLIANCE", "MEMBER_AVAILABLE", "MEMBER_LOCKED"):
        raise LedgerError(f"Funding source '{funding_source}' is not allowed.")

    before = after = None
    if funding_source == "ALLIANCE":
        member_nation_id = None
        assert_can_mutate(conn, None, "withdraw")
        if alliance_free is None:
            raise LedgerError("Alliance funds can only be spent after checking the live PnW bank.")
        free = M.sub(alliance_free, holds(conn, None, "ALLIANCE"))
        for res, amt in amounts.items():
            if free.get(res, 0) < amt:
                raise InsufficientFunds(
                    f"Alliance-owned funds are short on {M.LABELS[res]}: "
                    f"{M.fmt_units(res, max(free.get(res, 0), 0))} available, "
                    f"{M.fmt_units(res, amt)} requested.")
        before, after = {"alliance_free": free}, {"alliance_free": M.sub(free, amounts)}
    else:
        if member_nation_id is None:
            raise LedgerError("A member account is required for this funding source.")
        assert_can_mutate(conn, member_nation_id, "withdraw")
        member = get_member(conn, member_nation_id)
        if member is None:
            raise LedgerError("That nation has no TUN Bank account.")
        if member["frozen"] and tx_type == "WITHDRAW_SELF":
            raise FinancialBlocked(
                "Your account is frozen by ECON. Please contact ECON staff."
                + (f" Reason: {member['frozen_reason']}" if member["frozen_reason"] else ""))
        if funding_source == "MEMBER_AVAILABLE":
            free = spendable(conn, member_nation_id)
            lock_id = None
        else:
            if lock_id is None:
                raise LedgerError("Withdrawing from LOCKED funds requires a lock id.")
            lk = conn.execute("SELECT nation_id FROM locks WHERE id=?", (lock_id,)).fetchone()
            if not lk or lk["nation_id"] != member_nation_id:
                raise LedgerError("That lock does not belong to this member.")
            free = lock_spendable(conn, lock_id, member_nation_id)
        for res, amt in amounts.items():
            if free.get(res, 0) < amt:
                raise InsufficientFunds(
                    f"Not enough {M.LABELS[res]}: {M.fmt_units(res, max(free.get(res, 0), 0))} "
                    f"available, {M.fmt_units(res, amt)} requested.")
        before = snapshot_accounts(conn, member_nation_id)
        after = json.loads(json.dumps(before))
        key = "available" if funding_source == "MEMBER_AVAILABLE" else "locked"
        after[key] = M.sub(before[key], amounts)

    now = now_iso()
    cur = conn.execute(
        "INSERT INTO transactions(created_at,updated_at,tx_type,status,funding_source,"
        "member_nation_id,lock_id,dest_nation_id,actor_discord_id,approver_discord_id,note,"
        "reason,idempotency_key,price_snapshot_id,value_cents,balance_before_json,"
        "balance_after_json) VALUES(?,?,?,'CONFIRMED',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, tx_type, funding_source, member_nation_id, lock_id, dest_nation_id,
         str(actor), approver, note, reason, idempotency_key, snapshot_id, value_cents,
         jdump(before), jdump(after)))
    tx_id = cur.lastrowid
    for res, amt in amounts.items():
        conn.execute("INSERT INTO tx_items(tx_id,resource,amount) VALUES(?,?,?)", (tx_id, res, amt))
    audit(conn, actor, "WITHDRAWAL_CONFIRMED", f"tx:{tx_id}", {
        "funding_source": funding_source, "member": member_nation_id, "dest": dest_nation_id,
        "amounts": amounts, "note": note, "reason": reason})
    return tx_id, True


def mark_attempt(conn, tx_id: int) -> bool:
    """The at-most-once gate. Only ONE caller can ever get True for a transaction;
    only that caller may send the transfer to PnW."""
    cur = conn.execute(
        "UPDATE transactions SET attempted_at=?, updated_at=? "
        "WHERE id=? AND status='CONFIRMED' AND attempted_at IS NULL AND pnw_record_id IS NULL",
        (now_iso(), now_iso(), tx_id))
    return cur.rowcount == 1


def attach_pnw_record(conn, tx_id: int, pnw_record_id: int) -> None:
    tx, _ = get_tx(conn, tx_id)
    if tx is None:
        raise LedgerError(f"Transaction {tx_id} not found")
    if tx["pnw_record_id"] not in (None, pnw_record_id):
        raise LedgerError(
            f"Transaction {tx_id} is already linked to PnW record {tx['pnw_record_id']}")
    other = conn.execute(
        "SELECT id FROM transactions WHERE pnw_record_id=? AND id!=?", (pnw_record_id, tx_id)
    ).fetchone()
    if other:
        raise LedgerError(f"PnW record {pnw_record_id} already belongs to transaction {other['id']}")
    conn.execute("UPDATE transactions SET pnw_record_id=?, updated_at=? WHERE id=?",
                 (pnw_record_id, now_iso(), tx_id))


def complete_withdrawal(conn, tx_id: int) -> dict:
    """Book a withdrawal into the ledger AFTER PnW confirmed it."""
    tx, items = get_tx(conn, tx_id)
    if tx is None:
        raise LedgerError(f"Transaction {tx_id} not found")
    if tx["status"] == "COMPLETED":
        return {"already": True}
    if tx["status"] not in IN_FLIGHT:
        raise LedgerError(f"Transaction {tx_id} is {tx['status']}, it cannot be completed.")
    if tx["pnw_record_id"] is None:
        raise LedgerError("Cannot complete a withdrawal without a PnW record id.")
    if tx["funding_source"] != "ALLIANCE":
        bucket = "AVAILABLE" if tx["funding_source"] == "MEMBER_AVAILABLE" else "LOCKED"
        g = new_group("WD")
        post_entries(conn, [
            dict(group_id=g, nation_id=tx["member_nation_id"], bucket=bucket, resource=res,
                 delta=-amt, entry_type="WITHDRAWAL", pnw_record_id=tx["pnw_record_id"],
                 tx_id=tx_id, lock_id=tx["lock_id"], actor=tx["actor_discord_id"],
                 note=tx["note"], price_snapshot_id=tx["price_snapshot_id"])
            for res, amt in items.items()
        ])
    now = now_iso()
    conn.execute("UPDATE transactions SET status='COMPLETED', completed_at=?, updated_at=? WHERE id=?",
                 (now, now, tx_id))
    conn.execute("UPDATE pnw_records SET status='LINKED_TX', tx_id=?, classification='OUTGOING_TX' "
                 "WHERE id=?", (tx_id, tx["pnw_record_id"]))
    conn.execute(
        "UPDATE integrity_events SET status='RESOLVED', resolved_by='system', resolved_at=?, "
        "resolution_note=? WHERE status='OPEN' AND dedupe_key=?",
        (now, f"Confirmed by PnW record #{tx['pnw_record_id']}", f"tx-uncertain:{tx_id}"))
    audit(conn, "system", "WITHDRAWAL_COMPLETED", f"tx:{tx_id}",
          {"pnw_record_id": tx["pnw_record_id"], "amounts": items})
    return {"already": False}


def fail_tx(conn, tx_id: int, reason: str) -> bool:
    """Mark a transfer FAILED. Refused if PnW ever produced a record for it."""
    cur = conn.execute(
        "UPDATE transactions SET status='FAILED', failure_reason=?, updated_at=? "
        "WHERE id=? AND status IN ('CONFIRMED','RECONCILIATION_REQUIRED') AND pnw_record_id IS NULL",
        (reason[:500], now_iso(), tx_id))
    if cur.rowcount:
        audit(conn, "system", "WITHDRAWAL_FAILED", f"tx:{tx_id}", {"reason": reason})
        conn.execute(
            "UPDATE integrity_events SET status='RESOLVED', resolved_by='system', resolved_at=?, "
            "resolution_note='Transfer confirmed FAILED' WHERE status='OPEN' AND dedupe_key=?",
            (now_iso(), f"tx-uncertain:{tx_id}"))
    return bool(cur.rowcount)


def mark_uncertain(conn, tx_id: int, reason: str) -> None:
    """We cannot prove whether PnW sent the money. Keep the hold, alert ECON."""
    tx, _ = get_tx(conn, tx_id)
    if tx is None or tx["status"] not in IN_FLIGHT:
        return
    conn.execute("UPDATE transactions SET status='RECONCILIATION_REQUIRED', failure_reason=?, "
                 "updated_at=? WHERE id=?", (reason[:500], now_iso(), tx_id))
    raise_event(conn, "RECON", "TX_UNCERTAIN", nation_id=tx["member_nation_id"],
                ref_type="tx", ref_id=tx_id, details={"reason": reason},
                dedupe_key=f"tx-uncertain:{tx_id}")


# ------------------------------------------------------------- approvals
def create_approval(conn, kind: str, payload: dict, requested_by: str, reason: str) -> int:
    expiry = cfg_int(conn, "approval_expiry_minutes") or 60
    expires_at = (utcnow() + dt.timedelta(minutes=expiry)).strftime(ISO_FMT)
    cur = conn.execute(
        "INSERT INTO approval_requests(kind,payload_json,status,requested_by,requested_at,"
        "expires_at,reason) VALUES(?,?, 'PENDING',?,?,?,?)",
        (kind, jdump(payload), str(requested_by), now_iso(), expires_at, reason))
    audit(conn, requested_by, "APPROVAL_REQUESTED", f"approval:{cur.lastrowid}",
          {"kind": kind, "reason": reason})
    return cur.lastrowid


def get_approval(conn, approval_id: int):
    return conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()


def approve(conn, approval_id: int, approver: str):
    """Second person signs off. Must be a different person from the requester."""
    ap = get_approval(conn, approval_id)
    if not ap:
        raise LedgerError(f"Approval #{approval_id} does not exist.")
    if ap["status"] != "PENDING":
        raise LedgerError(f"Approval #{approval_id} is {ap['status']}.")
    if parse_iso(ap["expires_at"]) < utcnow():
        conn.execute("UPDATE approval_requests SET status='EXPIRED' WHERE id=?", (approval_id,))
        raise LedgerError(f"Approval #{approval_id} has expired.")
    if str(approver) == ap["requested_by"]:
        raise LedgerError("Two-person rule: a different staff member must approve this.")
    conn.execute("UPDATE approval_requests SET approved_by=?, approved_at=? WHERE id=?",
                 (str(approver), now_iso(), approval_id))
    audit(conn, approver, "APPROVAL_GRANTED", f"approval:{approval_id}", {})
    return get_approval(conn, approval_id)


def finish_approval(conn, approval_id: int, status: str, result: dict):
    conn.execute("UPDATE approval_requests SET status=?, result_json=? WHERE id=?",
                 (status, jdump(result), approval_id))


def revoke_approval(conn, approval_id: int, actor: str) -> bool:
    cur = conn.execute("UPDATE approval_requests SET status='REVOKED' WHERE id=? AND status='PENDING'",
                       (approval_id,))
    if cur.rowcount:
        audit(conn, actor, "APPROVAL_REVOKED", f"approval:{approval_id}", {})
    return bool(cur.rowcount)


# ------------------------------------------------------------ adjustments
def adjustment_debt(conn, nation_id: int, deltas: dict) -> dict:
    """Resources whose AVAILABLE balance an adjustment would take below zero -> {resource: resulting balance}.

    The balance is NOT clamped: 10,000 steel minus 30,000 steel is -20,000 steel. Refused while a pending withdrawal
    is holding that resource (the debt would silently uncover it)."""
    bal = get_balances(conn, nation_id, "AVAILABLE")
    held = holds(conn, nation_id, "MEMBER_AVAILABLE")
    out = {}
    for res, d in deltas.items():
        if d >= 0:
            continue
        after = bal.get(res, 0) + d
        if after < 0 and (bal.get(res, 0) - held.get(res, 0)) < -d:
            if held.get(res, 0):
                raise InsufficientFunds(f"A pending withdrawal is holding {M.LABELS[res]}; wait for it to finish "
                                        "before taking this balance below zero.")
            out[res] = after
    return out


def apply_adjustment(conn, *, nation_id: int, deltas: dict, reason: str, evidence: str,
                     actor: str, approval_id: int | None, snapshot_id=None) -> dict:
    """Documented correction of an accounting error. NOT a way to fund an account.

    Positive changes, and any change that takes a balance below zero, require a second staff member's approval."""
    deltas = {r: int(v) for r, v in deltas.items() if v}
    for r in deltas:
        if r not in M.RESOURCES:
            raise LedgerError(f"Unknown resource {r}")
    if not deltas:
        raise LedgerError("Nothing to adjust.")
    if not reason.strip() or not evidence.strip():
        raise LedgerError("A reason AND evidence/reference are mandatory for adjustments.")
    assert_can_mutate(conn, nation_id, "adjust")
    debt = adjustment_debt(conn, nation_id, deltas)          # {resource: resulting NEGATIVE balance}
    if any(v > 0 for v in deltas.values()) or debt:
        what = "Positive adjustments" if not debt else "Taking a balance below zero (a debt)"
        if approval_id is None:
            raise LedgerError(f"{what} require two-person approval.")
        ap = get_approval(conn, approval_id)
        if not ap or ap["kind"] != "ADJUSTMENT" or not ap["approved_by"] \
                or ap["approved_by"] == ap["requested_by"]:
            raise LedgerError("No valid second-person approval for this adjustment.")
    avail = spendable(conn, nation_id)
    for res, d in deltas.items():
        if d < 0 and res not in debt and avail.get(res, 0) < -d:
            raise InsufficientFunds(
                f"Cannot remove {M.fmt_units(res, -d)} {M.LABELS[res]}: only "
                f"{M.fmt_units(res, avail.get(res, 0))} is spendable.")
    ensure_member(conn, nation_id)
    before = snapshot_accounts(conn, nation_id)
    cur = conn.execute(
        "INSERT INTO adjustments(nation_id,bucket,reason,evidence,actor,approval_id,"
        "price_snapshot_id,created_at) VALUES(?, 'AVAILABLE', ?,?,?,?,?,?)",
        (nation_id, reason.strip(), evidence.strip(), str(actor), approval_id, snapshot_id, now_iso()))
    adj_id = cur.lastrowid
    for res, d in deltas.items():
        conn.execute("INSERT INTO adjustment_items(adjustment_id,resource,delta) VALUES(?,?,?)",
                     (adj_id, res, d))
    g = new_group("ADJ")
    post_entries(conn, [
        dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=res, delta=d,
             entry_type="ADJUSTMENT", adjustment_id=adj_id, actor=actor,
             note=f"{reason} | evidence: {evidence}", price_snapshot_id=snapshot_id)
        for res, d in deltas.items()
    ])
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting="balance_adjustment", previous=CA.amounts_text(before["available"]),
              new=CA.amounts_text(snapshot_accounts(conn, nation_id)["available"]),
              target=f"nation [#{nation_id}] · reason: {reason} · evidence: {evidence}", category="ACCOUNTING", only_if_changed=False)
    audit(conn, actor, "ADJUSTMENT_APPLIED", f"adjustment:{adj_id}", {
        "nation_id": nation_id, "deltas": deltas, "reason": reason, "evidence": evidence,
        "approval_id": approval_id})
    return {"adjustment_id": adj_id, "before": before, "after": snapshot_accounts(conn, nation_id)}


def find_or_request_approval(conn, kind: str, requester: str, payload: dict, reason: str):
    """Two-person rule helper. Returns (approval_id, approver_or_None).

    The requester asks once; a different staff member approves; the requester re-runs the same
    command, which finds the approved request (same payload) and proceeds."""
    for ap in conn.execute("SELECT * FROM approval_requests WHERE kind=? AND status='PENDING' "
                           "AND requested_by=? ORDER BY id DESC", (kind, str(requester))).fetchall():
        if ap["payload_json"] != jdump(payload):
            continue
        if parse_iso(ap["expires_at"]) < utcnow():
            conn.execute("UPDATE approval_requests SET status='EXPIRED' WHERE id=?", (ap["id"],))
            continue
        return ap["id"], ap["approved_by"]
    return create_approval(conn, kind, payload, requester, reason), None
