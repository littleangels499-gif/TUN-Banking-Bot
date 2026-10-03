"""Controlled opening-balance import (.xlsx / .csv).

Two steps, exactly as the specification requires:
  1. preview(): read + validate the file and show what WOULD happen. Writes nothing.
  2. commit(): after explicit confirmation, write the batch and the OPENING ledger
     entries in ONE all-or-nothing transaction.

Accepted layouts (header row required):
  WIDE:  nation_id | (nation_name) | money | coal | oil | ...   one row per nation
  LONG:  nation_id | resource | amount                          one row per amount
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import ledger as L
from . import money as M
from .config import cfg_bool
from .reconcile import batch_fingerprint
from .util import jdump, now_iso, sha256_bytes
from .valuation import value_amounts

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
# A single nation holding more than this in any one resource is treated as impossible.
MAX_PER_RESOURCE = {"money": 10**12 * 100}  # $1 trillion in cents
DEFAULT_MAX_RESOURCE_UNITS = 10**10 * 100    # 10 billion of a resource


@dataclass
class Preview:
    filename: str
    file_sha256: str
    rows: list = field(default_factory=list)          # [(nation_id, resource, units)]
    nations: dict = field(default_factory=dict)        # nation_id -> {resource: units}
    errors: list = field(default_factory=list)         # blocking
    warnings: list = field(default_factory=list)
    missing_members: list = field(default_factory=list)  # alliance members not in the file
    unknown_nations: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    valuation: object | None = None
    blocked: bool = False
    content: bytes = b""


def _read_table(filename: str, content: bytes) -> list[list]:
    name = filename.lower()
    if name.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        return [row for row in csv.reader(io.StringIO(text))]
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    raise ValueError("Only .xlsx and .csv files are supported.")


def _cell_units(value, where: str, errors: list) -> int | None:
    """Strict amount reader. Blank = 0. Returns units or None (error recorded)."""
    if value is None or str(value).strip() == "":
        return 0
    if isinstance(value, bool):
        errors.append(f"{where}: '{value}' is not a number")
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    try:
        d = Decimal(text)
    except InvalidOperation:
        errors.append(f"{where}: '{value}' is not a valid number")
        return None
    if not d.is_finite():
        errors.append(f"{where}: '{value}' is not a valid number")
        return None
    if d < 0:
        errors.append(f"{where}: negative amount ({value})")
        return None
    scaled = d * M.SCALE
    if scaled != scaled.to_integral_value():
        errors.append(f"{where}: more than 2 decimal places ({value})")
        return None
    return int(scaled)


def preview(filename: str, content: bytes, *, members: dict | None, snapshot) -> Preview:
    p = Preview(filename=filename, file_sha256=sha256_bytes(content), content=content)
    if len(content) > MAX_BYTES:
        p.errors.append("File is larger than 5 MB.")
        p.blocked = True
        return p
    try:
        table = _read_table(filename, content)
    except Exception as exc:  # noqa: BLE001
        p.errors.append(f"Could not read the file: {exc}")
        p.blocked = True
        return p
    table = [r for r in table if any(c not in (None, "") for c in r)]
    if len(table) < 2:
        p.errors.append("The file has no data rows.")
        p.blocked = True
        return p
    if len(table) - 1 > MAX_ROWS:
        p.errors.append(f"Too many rows (max {MAX_ROWS}).")
        p.blocked = True
        return p

    header = [str(h).strip().lower().replace(" ", "_") if h is not None else "" for h in table[0]]
    hmap = {}
    for i, h in enumerate(header):
        h = h.replace("nation_id", "nation_id")
        if h in ("nation_id", "nationid", "nation", "id", "nation_number"):
            hmap.setdefault("nation_id", i)
        elif h in ("resource", "type"):
            hmap["resource"] = i
        elif h in ("amount", "quantity", "qty", "value"):
            hmap["amount"] = i
        elif h in ("nation_name", "name", "leader", "leader_name"):
            hmap.setdefault("name", i)
        else:
            try:
                res = M.resolve_resource(h)
            except M.AmountError:
                if h:
                    p.warnings.append(f"Column '{table[0][i]}' is not a known resource and was ignored.")
                continue
            if res in hmap:
                p.errors.append(f"Resource '{res}' appears in two columns.")
            hmap[res] = i
    if "nation_id" not in hmap:
        p.errors.append("There is no 'nation_id' column.")
        p.blocked = True
        return p
    long_layout = "resource" in hmap and "amount" in hmap
    wide_cols = [r for r in M.RESOURCES if r in hmap]
    if not long_layout and not wide_cols:
        p.errors.append("No resource columns found (expected money, coal, oil... or resource+amount).")
        p.blocked = True
        return p

    seen_pairs = set()
    seen_rows = set()
    for lineno, row in enumerate(table[1:], start=2):
        row = list(row) + [None] * (len(header) - len(row))
        key = tuple("" if c is None else str(c).strip() for c in row)
        if key in seen_rows:
            p.errors.append(f"Row {lineno}: exact duplicate of an earlier row.")
            continue
        seen_rows.add(key)
        raw_id = row[hmap["nation_id"]]
        try:
            f = float(str(raw_id).strip())
            if f != int(f) or int(f) <= 0:
                raise ValueError
            nid = int(f)
        except (TypeError, ValueError):
            p.errors.append(f"Row {lineno}: '{raw_id}' is not a valid nation id.")
            continue
        entries = []
        if long_layout:
            rname = row[hmap["resource"]]
            try:
                res = M.resolve_resource(str(rname))
            except M.AmountError:
                p.errors.append(f"Row {lineno}: unknown resource '{rname}'.")
                continue
            u = _cell_units(row[hmap["amount"]], f"Row {lineno} {res}", p.errors)
            if u:
                entries.append((res, u))
        else:
            for res in wide_cols:
                u = _cell_units(row[hmap[res]], f"Row {lineno} {res}", p.errors)
                if u:
                    entries.append((res, u))
        for res, u in entries:
            if (nid, res) in seen_pairs:
                p.errors.append(f"Row {lineno}: nation {nid} already has {res} in this file (duplicate).")
                continue
            seen_pairs.add((nid, res))
            cap = MAX_PER_RESOURCE.get(res, DEFAULT_MAX_RESOURCE_UNITS)
            if u > cap:
                p.errors.append(f"Row {lineno}: {res} amount for nation {nid} is impossibly large.")
                continue
            p.rows.append((nid, res, u))
            p.nations.setdefault(nid, {})[res] = u

    if not p.rows and not p.errors:
        p.errors.append("The file contains no positive amounts.")
    for _, res, u in p.rows:
        p.totals[res] = p.totals.get(res, 0) + u
    p.valuation = value_amounts(p.totals, snapshot)
    if members is not None:
        p.unknown_nations = sorted(n for n in p.nations if n not in members)
        p.missing_members = sorted(n for n in members if n not in p.nations)
        if p.unknown_nations:
            p.errors.append(
                f"{len(p.unknown_nations)} nation(s) are not in the alliance: "
                + ", ".join(map(str, p.unknown_nations[:15])) + (" ..." if len(p.unknown_nations) > 15 else ""))
        if p.missing_members:
            p.warnings.append(f"{len(p.missing_members)} current alliance member(s) are not in the file "
                              "(they will start with zero).")
    else:
        p.errors.append("Could not read the alliance member list from PnW, so nations cannot be validated. "
                        "Try again in a minute.")
    if not p.valuation.complete:
        p.warnings.append("Some resource prices are missing: the market value is incomplete.")
    if p.valuation.stale or p.valuation.suspicious:
        p.warnings.append("Market prices are stale/unusual: treat the value as approximate.")
    p.blocked = bool(p.errors)
    return p


def commit(conn, p: Preview, *, admin_id: str, note: str, snapshot_id) -> dict:
    """Write the batch. Must be called inside db.tx(). Refuses anything blocked."""
    if p.blocked or p.errors:
        raise L.LedgerError("This import has errors and cannot be committed.")
    if not cfg_bool(conn, "opening_import_allowed"):
        raise L.LedgerError("Opening-balance import is switched off.")
    L.assert_can_mutate(conn, None, "import")
    dup = conn.execute("SELECT id FROM import_batches WHERE source_sha256=?", (p.file_sha256,)).fetchone()
    if dup:
        raise L.LedgerError(f"This exact file was already imported as batch #{dup['id']}.")
    already = conn.execute(
        "SELECT nation_id, resource FROM ledger_entries WHERE entry_type='OPENING'").fetchall()
    have = {(r["nation_id"], r["resource"]) for r in already}
    clash = [f"{n}/{r}" for n, r, _ in p.rows if (n, r) in have]
    if clash:
        raise L.LedgerError("These nation/resource pairs already have an opening balance: "
                            + ", ".join(clash[:10]) + ". Opening balances can never be overwritten; "
                            "use a documented /bank adjust instead.")
    fingerprint_rows = [{"nation_id": n, "resource": r, "amount": u} for n, r, u in p.rows]
    cur = conn.execute(
        "INSERT INTO import_batches(created_at,admin_discord_id,source_filename,source_sha256,row_count,"
        "totals_json,rows_sha256,price_snapshot_id,value_cents,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), str(admin_id), p.filename, p.file_sha256, len(p.rows), jdump(p.totals),
         batch_fingerprint(fingerprint_rows), snapshot_id, p.valuation.total_cents if p.valuation else None,
         note))
    batch_id = cur.lastrowid
    conn.execute("INSERT INTO import_files(batch_id, content) VALUES(?,?)", (batch_id, p.content))
    for n, r, u in p.rows:
        conn.execute("INSERT INTO opening_balance_rows(batch_id,nation_id,resource,amount) VALUES(?,?,?,?)",
                     (batch_id, n, r, u))
    g = L.new_group("OPEN")
    for n in p.nations:
        L.ensure_member(conn, n)
    L.post_entries(conn, [
        dict(group_id=g, nation_id=n, bucket="AVAILABLE", resource=r, delta=u, entry_type="OPENING",
             batch_id=batch_id, actor=admin_id, note=f"Opening balance import #{batch_id}",
             price_snapshot_id=snapshot_id)
        for n, r, u in p.rows])
    L.audit(conn, admin_id, "OPENING_IMPORT", f"batch:{batch_id}", {
        "filename": p.filename, "sha256": p.file_sha256, "rows": len(p.rows), "totals": p.totals,
        "note": note})
    return {"batch_id": batch_id, "rows": len(p.rows), "nations": len(p.nations), "totals": p.totals}
