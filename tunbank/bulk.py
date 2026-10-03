"""Bulk transfers: many alliance-funded payments from one reviewed list.

Rules (spec section 15): validate EVERY destination and amount first, show a full preview with the
total Current Market Value, require confirmation, make every payment idempotent, record each PnW
transaction id, report each success/failure separately, and apply the transaction/daily/role limits.

Bulk uses ALLIANCE-OWNED funds only. To spend one member's own deposit, use /bank withdraw.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from . import importer
from . import ledger as L
from . import money as M
from .util import jdump, now_iso

log = logging.getLogger("tunbank.bulk")

MAX_ROWS = 100
_RUNNING: set = set()      # batches being executed right now (never run one twice at once)
SYSTEMIC = ("EMERGENCY LOCK", "paused", "RECONCILIATION REQUIRED", "Could not read the live PnW bank")

TEMPLATE = (
    "nation,money,coal,oil,aluminum,note\n"
    "123456,1000000,0,0,0,Weekly pay\n"
    "Some Nation Name,0,5000,0,0,Coal for power\n"
)


@dataclass
class BulkRow:
    row_no: int
    nation_text: str
    amounts: dict
    note: str = ""
    nation_id: int | None = None
    nation_label: str = ""


@dataclass
class BulkParse:
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    sha256: str = ""
    filename: str = ""


def parse(filename: str, content: bytes) -> BulkParse:
    from .util import sha256_bytes

    out = BulkParse(sha256=sha256_bytes(content), filename=filename)
    if len(content) > importer.MAX_BYTES:
        out.errors.append("File is larger than 5 MB.")
        return out
    try:
        table = importer._read_table(filename, content)
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"Could not read the file: {exc}")
        return out
    table = [r for r in table if any(c not in (None, "") for c in r)]
    if len(table) < 2:
        out.errors.append("The file has no data rows.")
        return out
    if len(table) - 1 > MAX_ROWS:
        out.errors.append(f"Too many rows ({len(table) - 1}); the maximum is {MAX_ROWS} per bulk transfer.")
        return out
    header = [str(h).strip().lower().replace(" ", "_") if h is not None else "" for h in table[0]]
    cols: dict = {}
    for i, h in enumerate(header):
        if h in ("nation", "nation_id", "id", "nation_name", "name", "link", "url", "destination", "to"):
            cols.setdefault("nation", i)
        elif h in ("note", "memo", "reason"):
            cols["note"] = i
        else:
            try:
                res = M.resolve_resource(h)
            except M.AmountError:
                if h:
                    out.warnings.append(f"Column '{table[0][i]}' is not a known resource and was ignored.")
                continue
            if res in cols:
                out.errors.append(f"Resource '{res}' appears in two columns.")
            cols[res] = i
    if "nation" not in cols:
        out.errors.append("There is no 'nation' column (use a nation id, name or link).")
        return out
    res_cols = [r for r in M.RESOURCES if r in cols]
    if not res_cols:
        out.errors.append("No resource columns found (money, coal, oil, ...).")
        return out
    seen_exact = set()
    for lineno, row in enumerate(table[1:], start=2):
        row = list(row) + [None] * (len(header) - len(row))
        key = tuple("" if c is None else str(c).strip() for c in row)
        if key in seen_exact:
            out.errors.append(f"Row {lineno}: exact duplicate of an earlier row.")
            continue
        seen_exact.add(key)
        nation_text = "" if row[cols["nation"]] is None else str(row[cols["nation"]]).strip()
        if nation_text.endswith(".0") and nation_text[:-2].isdigit():
            nation_text = nation_text[:-2]        # spreadsheets turn ids into 123456.0
        if not nation_text:
            out.errors.append(f"Row {lineno}: no nation given.")
            continue
        amounts, bad = {}, False
        for res in res_cols:
            u = importer._cell_units(row[cols[res]], f"Row {lineno} {res}", out.errors)
            if u is None:
                bad = True
            elif u:
                amounts[res] = u
        if bad:
            continue
        if not amounts:
            out.errors.append(f"Row {lineno}: no amounts to send.")
            continue
        note = "" if "note" not in cols or row[cols["note"]] is None else str(row[cols["note"]]).strip()[:100]
        out.rows.append(BulkRow(lineno, nation_text, amounts, note))
    return out


def totals_of(rows) -> dict:
    t: dict = {}
    for r in rows:
        t = M.add(t, r.amounts)
    return t


# --------------------------------------------------------------- running
def load_batch(conn, batch_id: int):
    b = conn.execute("SELECT * FROM bulk_batches WHERE id=?", (batch_id,)).fetchone()
    items = conn.execute("SELECT * FROM bulk_items WHERE batch_id=? ORDER BY row_no", (batch_id,)).fetchall()
    return b, items


def refresh_items(conn, batch_id: int) -> None:
    """Bring item states in line with their real transactions (e.g. an uncertain one that later settled)."""
    for it in conn.execute("SELECT * FROM bulk_items WHERE batch_id=? AND tx_id IS NOT NULL "
                           "AND status IN ('PENDING','UNCERTAIN')", (batch_id,)).fetchall():
        tx = conn.execute("SELECT status, failure_reason FROM transactions WHERE id=?", (it["tx_id"],)).fetchone()
        new = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "CANCELLED": "FAILED"}.get(tx["status"])
        if tx["status"] == "RECONCILIATION_REQUIRED":
            new = "UNCERTAIN"
        if new and new != it["status"]:
            conn.execute("UPDATE bulk_items SET status=?, message=? WHERE batch_id=? AND row_no=?",
                         (new, (tx["failure_reason"] or "")[:300] or None, batch_id, it["row_no"]))


def summarize(conn, batch_id: int) -> dict:
    counts = {"PENDING": 0, "COMPLETED": 0, "FAILED": 0, "UNCERTAIN": 0}
    sent: dict = {}
    for it in conn.execute("SELECT status, amounts_json FROM bulk_items WHERE batch_id=?", (batch_id,)):
        counts[it["status"]] += 1
        if it["status"] == "COMPLETED":
            sent = M.add(sent, json.loads(it["amounts_json"]))
    return {"counts": counts, "sent": sent}


def finish(conn, batch_id: int) -> str:
    s = summarize(conn, batch_id)["counts"]
    if s["PENDING"]:
        status = "HALTED"
    elif s["FAILED"] or s["UNCERTAIN"]:
        status = "PARTIAL"
    else:
        status = "COMPLETED"
    conn.execute("UPDATE bulk_batches SET status=?, finished_at=? WHERE id=?",
                 (status, now_iso() if status != "HALTED" else None, batch_id))
    L.audit(conn, "system", "BULK_BATCH_" + status, f"bulk:{batch_id}", s)
    return status


async def run_batch(svc, batch_id: int, actor: str, role_ids) -> dict:
    """Send every PENDING item, one at a time, through the normal safe withdrawal pipeline."""
    if batch_id in _RUNNING:
        return {"error": "This batch is already running."}
    _RUNNING.add(batch_id)
    try:
        def load():
            with svc.db.read() as conn:
                b, items = load_batch(conn, batch_id)
                approver = None
                if b["approval_id"]:
                    ap = L.get_approval(conn, b["approval_id"])
                    approver = ap["approved_by"] if ap else None
                return dict(b), [dict(i) for i in items if i["status"] == "PENDING"], approver
        batch, items, approver = await asyncio.to_thread(load)
        bad_streak = 0
        halted_why = None
        for it in items:
            amounts = json.loads(it["amounts_json"])
            res = await svc.wd.request(
                tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None,
                dest_nation_id=it["nation_id"], amounts=amounts, actor=str(actor),
                note=it["note"] or f"TUN Bank bulk #{batch_id}", reason=f"bulk #{batch_id}: {batch['reason']}",
                idempotency_key=f"bulk-{batch_id}-{it['row_no']}", actor_role_ids=role_ids, approver=approver)
            status = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "UNCERTAIN": "UNCERTAIN"}.get(res.status)
            if res.status == "BLOCKED" and any(k in res.message for k in SYSTEMIC):
                halted_why = res.message           # leave the row PENDING so /bulk resume can continue
                break
            if status is None:
                status = "FAILED" if res.status == "BLOCKED" else "UNCERTAIN"

            def save(it=it, res=res, status=status):
                with svc.db.tx() as conn:
                    conn.execute("UPDATE bulk_items SET status=?, tx_id=?, message=? WHERE batch_id=? AND row_no=?",
                                 (status, res.tx_id, (res.message or "")[:300], batch_id, it["row_no"]))
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
