"""Reconciliation: prove the books are consistent, or say exactly what is not.

Nothing here EVER rewrites a balance to "make the numbers match". Problems are
recorded as integrity events (evidence preserved), ECON is alerted, and - for
serious ones - the emergency lock halts financial changes until staff review.
"""
from __future__ import annotations

import datetime as dt

from . import ledger as L
from . import money as M
from .config import cfg_int
from .util import ISO_FMT, jdump, now_iso, seconds_since, sha256_text, utcnow

AUTO_RESOLVE = ("STALE_SYNC", "STALE_PRICES", "BANK_UNAVAILABLE")


def _f(sev, kind, msg, *, nation_id=None, details=None, key=None):
    return {"severity": sev, "kind": kind, "message": msg, "nation_id": nation_id,
            "details": details or {}, "key": key or kind.lower()}


# --------------------------------------------------------------- checks
def check_chains(conn):
    out = []
    for table, kind in (("ledger_entries", "LEDGER_CHAIN_BROKEN"), ("audit_log", "AUDIT_CHAIN_BROKEN")):
        res = L.verify_chain(conn, table)
        if not res["ok"]:
            out.append(_f("CRITICAL", kind,
                          f"{table}: history was altered or deleted near row {res.get('bad_id')} "
                          f"({res['reason']}).", details=res))
    return out


def ledger_sums(conn):
    return {(r["nation_id"], r["bucket"], r["resource"]): r["s"]
            for r in conn.execute("SELECT nation_id, bucket, resource, SUM(delta) AS s "
                                  "FROM ledger_entries GROUP BY nation_id, bucket, resource")}


def check_balances(conn):
    out = []
    sums = {k: v for k, v in ledger_sums(conn).items() if v}
    cache = {(r["nation_id"], r["bucket"], r["resource"]): r["amount"]
             for r in conn.execute("SELECT * FROM balances") if r["amount"]}
    bad = []
    for k in set(sums) | set(cache):
        if sums.get(k, 0) != cache.get(k, 0):
            bad.append({"nation_id": k[0], "bucket": k[1], "resource": k[2],
                        "ledger": sums.get(k, 0), "balance_table": cache.get(k, 0)})
    if bad:
        out.append(_f("CRITICAL", "BALANCE_MISMATCH",
                      f"{len(bad)} balance(s) do not equal the sum of their ledger history "
                      "(someone or something changed a balance without a ledger entry).",
                      details={"examples": bad[:15], "count": len(bad)}))
    # A negative AVAILABLE balance is a legitimate debt to the alliance (restored from a spreadsheet or deducted by
    # an approved adjustment) and is part of the member's net accounting balance. A negative LOCKED balance is
    # still impossible: locks can only hold what was reserved.
    neg = [k for k, v in sums.items() if v < 0 and k[1] == "LOCKED"]
    if neg:
        out.append(_f("CRITICAL", "NEGATIVE_BALANCE", f"{len(neg)} impossible negative LOCKED balance(s).",
                      details={"examples": [list(k) for k in neg[:15]]}))
    return out


def _count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def check_justification(conn):
    """Every ledger row must be explained by evidence. Re-checked independently of triggers."""
    out = []
    probs = {
        "deposits without a matching PnW deposit record": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='DEPOSIT' AND NOT EXISTS ("
            "SELECT 1 FROM pnw_records p WHERE p.id=e.pnw_record_id AND p.direction='IN' "
            "AND p.classification='MEMBER_DEPOSIT' AND p.credited_nation_id=e.nation_id "
            "AND json_extract(p.amounts_json,'$.'||e.resource)=e.delta) "
            "AND NOT EXISTS (SELECT 1 FROM pnw_records p JOIN loan_events v ON v.pnw_record_id=p.id "
            "WHERE p.id=e.pnw_record_id AND p.classification='LOAN_REPAYMENT' AND v.kind='REPAYMENT' "
            "AND v.nation_id=e.nation_id AND e.resource='money' AND v.excess_cents=e.delta)"),
        "loan deductions without a recorded loan deduction": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='LOAN_DEDUCTION' AND NOT EXISTS ("
            "SELECT 1 FROM loan_events v WHERE v.id=e.loan_event_id AND v.kind='DEDUCTION' AND v.nation_id=e.nation_id "
            "AND v.interest_cents+v.principal_cents=-e.delta)"),
        "loan deductions whose ledger entry is missing": (
            "SELECT COUNT(*) FROM loan_events v WHERE v.kind='DEDUCTION' AND NOT EXISTS ("
            "SELECT 1 FROM ledger_entries e WHERE e.entry_type='LOAN_DEDUCTION' AND e.loan_event_id=v.id)"),
        "loans whose balances do not match their history": (
            "SELECT COUNT(*) FROM loans l WHERE l.interest_paid_cents != COALESCE((SELECT SUM(interest_cents) FROM loan_events v "
            "WHERE v.loan_id=l.id AND v.kind IN ('REPAYMENT','DEDUCTION','WRITE_OFF')),0) "
            "OR l.principal_paid_cents != COALESCE((SELECT SUM(principal_cents) FROM loan_events v "
            "WHERE v.loan_id=l.id AND v.kind IN ('REPAYMENT','DEDUCTION','WRITE_OFF')),0)"),
        "withdrawals without a completed PnW-confirmed transaction": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='WITHDRAWAL' AND NOT EXISTS ("
            "SELECT 1 FROM transactions t JOIN tx_items i ON i.tx_id=t.id "
            "WHERE t.id=e.tx_id AND t.status='COMPLETED' AND t.pnw_record_id=e.pnw_record_id "
            "AND i.resource=e.resource AND i.amount=-e.delta)"),
        "opening balances without an import batch record": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='OPENING' AND NOT EXISTS ("
            "SELECT 1 FROM opening_balance_rows r WHERE r.batch_id=e.batch_id "
            "AND r.nation_id=e.nation_id AND r.resource=e.resource AND r.amount=e.delta)"),
        "restores without a committed restore import batch": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='RESTORE' AND NOT EXISTS ("
            "SELECT 1 FROM opening_balance_rows r JOIN import_batches b ON b.id=r.batch_id "
            "AND b.kind='RESTORE' AND b.reset_id IS NOT NULL WHERE r.batch_id=e.batch_id "
            "AND r.nation_id=e.nation_id AND r.resource=e.resource AND r.amount=e.delta)"),
        "resets without a matching reset record": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='RESET' AND NOT EXISTS ("
            "SELECT 1 FROM deposit_reset_items i WHERE i.reset_id=e.reset_id AND i.nation_id=e.nation_id "
            "AND i.bucket=e.bucket AND i.resource=e.resource AND i.lock_id IS e.lock_id AND i.amount=-e.delta)"),
        "reset records whose ledger entries are missing": (
            "SELECT COUNT(*) FROM deposit_reset_items i WHERE NOT EXISTS ("
            "SELECT 1 FROM ledger_entries e WHERE e.entry_type='RESET' AND e.reset_id=i.reset_id "
            "AND e.nation_id=i.nation_id AND e.bucket=i.bucket AND e.resource=i.resource "
            "AND e.lock_id IS i.lock_id AND e.delta=-i.amount)"),
        "conversions without a conversion record": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='CONVERSION' AND NOT EXISTS ("
            "SELECT 1 FROM conversions c WHERE c.id=e.conversion_id AND c.nation_id=e.nation_id AND "
            "((e.resource=c.from_resource AND e.delta=-c.from_units) OR (e.resource=c.to_resource AND e.delta=c.to_units)))"),
        "conversion records whose ledger entries are incomplete": (
            "SELECT COUNT(*) FROM conversions c WHERE (SELECT COUNT(*) FROM ledger_entries e WHERE "
            "e.entry_type='CONVERSION' AND e.conversion_id=c.id)!=2"),
        "adjustments without documented reason/evidence": (
            "SELECT COUNT(*) FROM ledger_entries e WHERE entry_type='ADJUSTMENT' AND NOT EXISTS ("
            "SELECT 1 FROM adjustments j JOIN adjustment_items a ON a.adjustment_id=j.id "
            "WHERE j.id=e.adjustment_id AND a.resource=e.resource AND a.delta=e.delta "
            "AND length(trim(j.reason))>0 AND length(trim(j.evidence))>0)"),
        "lock/release groups that do not net to zero": (
            "SELECT COUNT(*) FROM (SELECT group_id FROM ledger_entries "
            "WHERE entry_type IN ('LOCK','RELEASE') GROUP BY group_id, resource HAVING SUM(delta)!=0)"),
        "locked funds not attached to a lock": (
            "SELECT COUNT(*) FROM ledger_entries WHERE bucket='LOCKED' AND lock_id IS NULL"),
        "locks holding a negative amount": (
            "SELECT COUNT(*) FROM (SELECT lock_id FROM ledger_entries WHERE bucket='LOCKED' "
            "GROUP BY lock_id, resource HAVING SUM(delta)<0)"),
        "completed transactions without a PnW record id": (
            "SELECT COUNT(*) FROM transactions WHERE status='COMPLETED' AND pnw_record_id IS NULL"),
        "completed member withdrawals missing from the ledger": (
            "SELECT COUNT(*) FROM transactions t WHERE t.status='COMPLETED' "
            "AND t.funding_source!='ALLIANCE' AND NOT EXISTS ("
            "SELECT 1 FROM ledger_entries e WHERE e.tx_id=t.id AND e.entry_type='WITHDRAWAL')"),
    }
    for label, sql in probs.items():
        n = _count(conn, sql)
        if n:
            out.append(_f("CRITICAL", "UNJUSTIFIED_ENTRIES", f"{n} {label}.",
                          details={"check": label, "count": n}, key="unjustified:" + label[:30]))
    missing = _count(
        conn,
        "SELECT COUNT(*) FROM pnw_records p WHERE p.status='CREDITED' AND p.classification="
        "'MEMBER_DEPOSIT' AND (SELECT COUNT(*) FROM json_each(p.amounts_json)) != "
        "(SELECT COUNT(*) FROM ledger_entries e WHERE e.pnw_record_id=p.id AND e.entry_type='DEPOSIT')")
    if missing:
        out.append(_f("RECON", "CREDIT_MISMATCH",
                      f"{missing} PnW deposit record(s) marked credited but the ledger lines "
                      "do not match.", details={"count": missing}))
    dup = _count(conn, "SELECT COUNT(*) FROM (SELECT pnw_record_id FROM ledger_entries "
                       "WHERE entry_type='DEPOSIT' GROUP BY pnw_record_id, resource HAVING COUNT(*)>1)")
    if dup:
        out.append(_f("CRITICAL", "DUPLICATE_CREDIT", f"{dup} PnW record(s) credited more than once."))
    return out


def batch_fingerprint(rows) -> str:
    return sha256_text(jdump(sorted([[r["nation_id"], r["resource"], r["amount"]] for r in rows])))


def check_opening(conn):
    out = []
    for b in conn.execute("SELECT * FROM import_batches"):
        if b["kind"] not in ("OPENING", "RESTORE"):
            continue
        rows = conn.execute("SELECT nation_id, resource, amount FROM opening_balance_rows "
                            "WHERE batch_id=?", (b["id"],)).fetchall()
        if batch_fingerprint(rows) != b["rows_sha256"]:
            out.append(_f("CRITICAL", "OPENING_CHANGED",
                          f"Opening-balance import #{b['id']} no longer matches its original "
                          "fingerprint.", details={"batch": b["id"]}, key=f"opening:{b['id']}"))
        led = {(r["nation_id"], r["resource"]): r["s"] for r in conn.execute(
            "SELECT nation_id, resource, SUM(delta) s FROM ledger_entries "
            "WHERE entry_type=? AND batch_id=? GROUP BY nation_id, resource", (b["kind"], b["id"]))}
        src = {(r["nation_id"], r["resource"]): r["amount"] for r in rows}
        if led != src:
            out.append(_f("CRITICAL", "OPENING_CHANGED",
                          f"Opening-balance import #{b['id']}: ledger differs from the import record.",
                          details={"batch": b["id"]}, key=f"opening-ledger:{b['id']}"))
    return out


def in_flight_out_of_bank(conn) -> dict:
    """Member-funded transfers that may already have left the PnW bank but are not yet
    debited in the ledger (timing gap)."""
    rows = conn.execute(
        "SELECT i.resource, SUM(i.amount) a FROM transactions t JOIN tx_items i ON i.tx_id=t.id "
        "WHERE t.status IN ('CONFIRMED','RECONCILIATION_REQUIRED') AND t.funding_source!='ALLIANCE' "
        "AND t.attempted_at IS NOT NULL GROUP BY i.resource").fetchall()
    return {r["resource"]: r["a"] for r in rows}


def bank_position(conn, holdings: dict | None) -> dict:
    """The transparent alliance-owned calculation shown on the vault dashboard."""
    held = L.positive_claims(conn)      # debts (negative balances) never reduce other members' claims
    member_total = M.add(held["AVAILABLE"], held["LOCKED"])
    in_flight = in_flight_out_of_bank(conn)
    effective_member = M.sub(member_total, in_flight)
    alliance = None
    if holdings is not None:
        alliance = {r: holdings.get(r, 0) - effective_member.get(r, 0) for r in M.RESOURCES
                    if holdings.get(r, 0) or effective_member.get(r, 0)}
    return {"bank": holdings, "available": held["AVAILABLE"], "locked": held["LOCKED"], "owed_to_alliance": held["OWED"],
            "member_total": member_total, "in_flight_out": in_flight,
            "alliance_owned": alliance}


def check_bank(conn, holdings):
    if holdings is None:
        return [_f("WARNING", "BANK_UNAVAILABLE",
                   "Could not read the real PnW bank, so the ledger could not be compared to it.")]
    pos = bank_position(conn, holdings)
    short = {r: -v for r, v in (pos["alliance_owned"] or {}).items() if v < 0}
    if short:
        return [_f("CRITICAL", "LEDGER_EXCEEDS_BANK",
                   "Member-held balances are LARGER than what the PnW bank actually holds.",
                   details={"shortfall": short, "bank": holdings,
                            "member_total": pos["member_total"]}, key="ledger-exceeds-bank")]
    return []


def check_stuck(conn):
    out = []
    minutes = cfg_int(conn, "stuck_tx_minutes")
    for t in conn.execute("SELECT id, created_at, status FROM transactions WHERE status IN "
                          "('CONFIRMED','RECONCILIATION_REQUIRED')"):
        age = seconds_since(t["created_at"]) or 0
        if age > minutes * 60:
            out.append(_f("RECON", "TX_STUCK", f"Transaction #{t['id']} has been {t['status']} "
                          f"for {int(age // 60)} minutes.", details={"tx": t["id"]},
                          key=f"tx-stuck:{t['id']}"))
    return out


def check_freshness(conn):
    out = []
    stale_min = cfg_int(conn, "stale_sync_minutes")
    last_ok = L.get_state(conn, "last_scan_ok")
    age = seconds_since(last_ok)
    if age is None or age > stale_min * 60:
        out.append(_f("WARNING", "STALE_SYNC",
                      "No successful PnW bank scan " + (f"for {int(age // 60)} min." if age else "yet."),
                      key="stale-sync"))
    row = conn.execute("SELECT fetched_at FROM price_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    page = seconds_since(row["fetched_at"]) if row else None
    if page is None or page > cfg_int(conn, "price_stale_seconds"):
        out.append(_f("WARNING", "STALE_PRICES", "Market prices are missing or stale.", key="stale-prices"))
    return out


def check_mass_changes(conn):
    out = []
    lim = cfg_int(conn, "mass_change_nations")
    if not lim:
        return out
    since = (utcnow() - dt.timedelta(hours=1)).strftime(ISO_FMT)
    n = _count(conn, "SELECT COUNT(DISTINCT nation_id) FROM ledger_entries WHERE ts>=? "
                     "AND entry_type IN ('ADJUSTMENT','OPENING')", (since,))
    if n > lim:
        out.append(_f("RECON", "MASS_BALANCE_CHANGE",
                      f"{n} accounts received adjustments/opening balances within the last hour.",
                      details={"accounts": n}, key="mass-change:" + since[:13]))
    return out


def check_previous_head(conn):
    prev = conn.execute("SELECT * FROM reconciliation_runs WHERE ledger_head_id IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if not prev:
        return []
    row = conn.execute("SELECT entry_hash FROM ledger_entries WHERE id=?", (prev["ledger_head_id"],)).fetchone()
    cnt = _count(conn, "SELECT COUNT(*) FROM ledger_entries")
    if row is None or row["entry_hash"] != prev["ledger_head_hash"] or cnt < prev["ledger_count"]:
        return [_f("CRITICAL", "LEDGER_HISTORY_LOST",
                   "Ledger rows that existed at the last reconciliation are missing or changed.",
                   details={"expected_head": prev["ledger_head_id"], "expected_count": prev["ledger_count"],
                            "now_count": cnt})]
    return []


# ----------------------------------------------------------------- main
def run_checks(conn, *, holdings: dict | None, snapshot_id: int | None, triggered_by: str, per_bank: dict | None = None) -> dict:
    """Run every check, record findings as integrity events, store the run."""
    started = now_iso()
    findings = []
    for fn in (check_chains, check_balances, check_justification, check_opening,
               check_stuck, check_freshness, check_mass_changes, check_previous_head):
        findings += fn(conn)
    findings += check_bank(conn, holdings)

    new_events = []
    for f in findings:
        eid, is_new = L.raise_event(
            conn, f["severity"], f["kind"], nation_id=f["nation_id"], details=
            {"message": f["message"], **f["details"]}, dedupe_key="recon:" + f["key"])
        f["event_id"], f["is_new"] = eid, is_new
        if is_new:
            new_events.append(f)
    present = {f["kind"] for f in findings}
    for kind in AUTO_RESOLVE:
        if kind not in present:
            conn.execute(
                "UPDATE integrity_events SET status='RESOLVED', resolved_by='system', resolved_at=?, "
                "resolution_note='Condition cleared' WHERE status='OPEN' AND kind=? "
                "AND dedupe_key LIKE 'recon:%'", (now_iso(), kind))

    chain = L.verify_chain(conn, "ledger_entries")
    sev = {f["severity"] for f in findings}
    result = "OK" if not findings else ("DISCREPANCY" if sev & {"RECON", "CRITICAL"} else "WARNING")
    pos = bank_position(conn, holdings)
    if per_bank:
        pos["banks"] = per_bank
    cur = conn.execute(
        "INSERT INTO reconciliation_runs(started_at,finished_at,triggered_by,result,findings_json,"
        "bank_json,ledger_head_id,ledger_head_hash,ledger_count,price_snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), triggered_by, result,
         jdump([{k: v for k, v in f.items() if k not in ("details",)} for f in findings]),
         jdump(pos), chain.get("head_id") if chain["ok"] else None,
         chain.get("head_hash") if chain["ok"] else None,
         chain.get("count") if chain["ok"] else None, snapshot_id))
    if result == "OK":
        L.set_state(conn, "last_reconcile_ok", now_iso())
    L.audit(conn, triggered_by, "RECONCILIATION_RUN", f"run:{cur.lastrowid}",
            {"result": result, "findings": [f["kind"] for f in findings]})
    return {"run_id": cur.lastrowid, "result": result, "findings": findings,
            "new_events": new_events, "position": pos, "chain": chain}
