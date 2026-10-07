"""Deposit reset: clear every member balance through a FORMAL, permanently recorded ledger event.

Nothing is deleted or rewritten. A reset:
  1. records exactly what every balance was (deposit_resets + deposit_reset_items, append-only),
  2. appends one RESET ledger entry per balance that brings it to zero (hash-chained like every other entry),
  3. writes the configuration-audit entry and the audit-log entry (who, when, why, previous balances, total value),
  4. leaves PnW records, transactions, audit history, config audit, migrations and reconciliation history untouched.

The balances are then loaded from the verified spreadsheet with a RESTORE import tied to this reset.
"""
from __future__ import annotations

from . import ledger as L
from . import money as M
from .util import jdump, now_iso
from .valuation import value_amounts

RESET_PHRASE = "RESET DEPOSITS"
MIN_REASON = 10


def current_lines(conn) -> list[dict]:
    """Every non-zero balance, one line per (nation, bucket, resource[, lock]), read from the LEDGER."""
    rows = conn.execute(
        "SELECT nation_id, bucket, resource, CASE WHEN bucket='LOCKED' THEN lock_id END AS lock_id, "
        "SUM(delta) AS amount FROM ledger_entries GROUP BY 1,2,3,4 HAVING SUM(delta) != 0 "
        "ORDER BY nation_id, bucket, resource, lock_id").fetchall()
    return [dict(r) for r in rows]


def net_totals(lines: list[dict]) -> dict:
    out: dict = {}
    for ln in lines:
        out[ln["resource"]] = out.get(ln["resource"], 0) + ln["amount"]
    return {r: v for r, v in out.items() if v}


def preview(conn, snapshot) -> dict:
    lines = current_lines(conn)
    totals = net_totals(lines)
    pos, neg = {}, {}
    for ln in lines:
        d = pos if ln["amount"] > 0 else neg
        d[ln["resource"]] = d.get(ln["resource"], 0) + abs(ln["amount"])
    return {
        "lines": lines, "nations": len({ln["nation_id"] for ln in lines}), "totals": totals,
        "valuation": value_amounts(totals, snapshot), "positive": pos, "negative": neg,
        "paused": L.get_state(conn, "bank_paused") == "1",
        "in_flight": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status IN ('CONFIRMED','RECONCILIATION_REQUIRED') "
            "AND funding_source != 'ALLIANCE'").fetchone()[0],
        "waiting_restore": conn.execute(
            "SELECT id FROM deposit_resets WHERE restore_batch_id IS NULL ORDER BY id DESC LIMIT 1").fetchone(),
    }


def open_reset(conn):
    """The latest reset that has not been restored yet (the one a restore import attaches to), or None."""
    return conn.execute("SELECT * FROM deposit_resets WHERE restore_batch_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def execute(conn, *, actor: str, reason: str, snapshot) -> dict:
    """Perform the reset. Must run inside db.tx(); any failure rolls everything back."""
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise L.LedgerError(f"A reason of at least {MIN_REASON} characters is required.")
    L.assert_can_mutate(conn, None, "import")
    if L.get_state(conn, "bank_paused") != "1":
        raise L.LedgerError("Pause withdrawals first with `/bank lock`, so nobody can withdraw between the reset and "
                            "the restore. Resume with `/bank unlock` afterwards.")
    n_flight = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE status IN ('CONFIRMED','RECONCILIATION_REQUIRED') "
        "AND funding_source != 'ALLIANCE'").fetchone()[0]
    if n_flight:
        raise L.LedgerError(f"{n_flight} member withdrawal(s) are still in flight. Let them finish (or resolve them "
                            "with `/bank resolvetx`) before resetting.")
    waiting = open_reset(conn)
    if waiting:
        raise L.LedgerError(f"Deposit reset #{waiting['id']} is still waiting for its restore import. "
                            "Run `/deposit restore` first.")
    lines = current_lines(conn)
    if not lines:
        raise L.LedgerError("There are no member balances to reset.")
    cache = {(r["nation_id"], r["bucket"], r["resource"]): r["amount"] for r in conn.execute(
        "SELECT * FROM balances WHERE amount != 0")}
    ledger_sum: dict = {}
    for ln in lines:
        k = (ln["nation_id"], ln["bucket"], ln["resource"])
        ledger_sum[k] = ledger_sum.get(k, 0) + ln["amount"]
    if cache != ledger_sum:
        raise L.LedgerError("The cached balances do not match the ledger. Run `/ledger reconcile` and resolve the "
                            "problem before resetting.")
    totals = net_totals(lines)
    val = value_amounts(totals, snapshot)
    if val.total_cents is None or not val.complete:
        raise L.LedgerError("Market prices are unavailable for: " + ", ".join(val.missing or ["?"])
                            + ". The reset must record the total value, so try again when prices load.")
    nations = sorted({ln["nation_id"] for ln in lines})
    before = {"lines": lines, "totals": totals}
    cur = conn.execute(
        "INSERT INTO deposit_resets(created_at,actor,reason,nations_affected,lines,totals_json,value_cents,"
        "price_snapshot_id,price_as_of,before_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), str(actor), reason, len(nations), len(lines), jdump(totals), val.total_cents,
         val.snapshot_id, val.as_of, jdump(before)))
    reset_id = cur.lastrowid
    for ln in lines:
        conn.execute("INSERT INTO deposit_reset_items(reset_id,nation_id,bucket,resource,lock_id,amount) "
                     "VALUES(?,?,?,?,?,?)",
                     (reset_id, ln["nation_id"], ln["bucket"], ln["resource"], ln["lock_id"], ln["amount"]))
    g = L.new_group("RESET")
    L.post_entries(conn, [
        dict(group_id=g, nation_id=ln["nation_id"], bucket=ln["bucket"], resource=ln["resource"],
             delta=-ln["amount"], entry_type="RESET", lock_id=ln["lock_id"], reset_id=reset_id, actor=actor,
             note=f"Deposit reset #{reset_id}: {reason}", price_snapshot_id=val.snapshot_id)
        for ln in lines])
    left = conn.execute("SELECT COUNT(*) FROM balances WHERE amount != 0").fetchone()[0]
    if left:
        raise L.LedgerError(f"Safety stop: {left} balance(s) were not cleared. Nothing was changed.")
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting="deposit_reset", previous=f"{len(nations)} accounts, value {val.total_cents / 100:,.2f}",
              new="all balances cleared", target=f"reset #{reset_id} · reason: {reason}", category="RESET",
              only_if_changed=False)
    L.audit(conn, actor, "DEPOSIT_RESET", f"reset:{reset_id}", {
        "reason": reason, "nations": len(nations), "lines": len(lines), "totals": totals,
        "value_cents": val.total_cents, "price_snapshot_id": val.snapshot_id, "price_as_of": val.as_of})
    return {"reset_id": reset_id, "nations": len(nations), "lines": len(lines), "totals": totals,
            "valuation": val}
