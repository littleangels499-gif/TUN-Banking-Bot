"""Tax reporting helpers. Taxes are alliance-owned: these views are for ECON only.

Exemptions are TUN-side bookkeeping. They never change what Politics & War collects."""
from __future__ import annotations

import datetime as dt
import json

from .util import ISO_FMT, jdump, now_iso, utcnow

PERIODS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
PERIOD_LABEL = {"7d": "last 7 days", "30d": "last 30 days", "90d": "last 90 days", "all": "all time"}


def _day(text):
    t = str(text or "").strip()
    return t[:10] if len(t) >= 10 and t[4] == "-" and t[7] == "-" else None


def rows(conn, period: str = "30d", nation_id: int | None = None) -> list[dict]:
    days = PERIODS.get(period)
    cutoff = (utcnow() - dt.timedelta(days=days)).date().isoformat() if days else None
    q = "SELECT * FROM tax_records"
    args: list = []
    if nation_id is not None:
        q += " WHERE nation_id=?"
        args.append(nation_id)
    out = []
    for r in conn.execute(q + " ORDER BY id", args):
        day = _day(r["record_date"]) or _day(r["recorded_at"])
        if cutoff and (not day or day < cutoff):
            continue
        out.append({"id": r["id"], "pnw_record_id": r["pnw_record_id"], "nation_id": r["nation_id"],
                    "tax_id": r["tax_id"], "date": day or "", "when": r["record_date"] or r["recorded_at"] or "", "amounts": json.loads(r["amounts_json"]),
                    "price_snapshot_id": r["price_snapshot_id"]})
    return out


def by_nation(rs: list[dict]) -> dict:
    out: dict = {}
    for r in rs:
        n = out.setdefault(r["nation_id"], {"amounts": {}, "count": 0, "brackets": set(), "records": []})
        for res, u in r["amounts"].items():
            n["amounts"][res] = n["amounts"].get(res, 0) + u
        n["count"] += 1
        if r["tax_id"]:
            n["brackets"].add(r["tax_id"])
        n["records"].append(r["pnw_record_id"])
    return out


# ----------------------------------------------------------- exemptions
def active_exemptions(conn) -> dict:
    """{nation_id: reason} for exemptions in force right now."""
    now = now_iso()
    out = {}
    for r in conn.execute("SELECT nation_id, reason, expires_at FROM tax_exemptions WHERE active=1"):
        if r["expires_at"] and r["expires_at"] < now:
            continue
        out[r["nation_id"]] = r["reason"]
    return out


def add_exemption(conn, nation_id: int, reason: str, actor: str, days: int | None) -> int:
    from . import ledger as L

    if not reason.strip():
        raise L.LedgerError("A reason is required.")
    if nation_id in active_exemptions(conn):
        raise L.LedgerError("That nation already has an active exemption. Remove it first to change it.")
    expires = (utcnow() + dt.timedelta(days=days)).strftime(ISO_FMT) if days else None
    cur = conn.execute("INSERT INTO tax_exemptions(nation_id,reason,set_by,set_at,expires_at) VALUES(?,?,?,?,?)",
                       (nation_id, reason.strip(), str(actor), now_iso(), expires))
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting="tax_exemption", previous="not exempt", new="exempt" + (f" until {expires}" if expires else ""),
              target=f"nation [#{nation_id}] · {reason}", category="POLICY")
    L.audit(conn, actor, "TAX_EXEMPTION_ADDED", f"nation:{nation_id}", {"reason": reason, "expires": expires})
    return cur.lastrowid


def remove_exemption(conn, nation_id: int, note: str, actor: str) -> bool:
    from . import ledger as L

    if not note.strip():
        raise L.LedgerError("A reason is required.")
    cur = conn.execute("UPDATE tax_exemptions SET active=0, removed_by=?, removed_at=?, removal_note=? "
                       "WHERE nation_id=? AND active=1", (str(actor), now_iso(), note.strip(), nation_id))
    if cur.rowcount:
        from . import configaudit as CA
        CA.record(conn, actor=actor, setting="tax_exemption", previous="exempt", new="not exempt", target=f"nation [#{nation_id}] · {note}", category="POLICY")
        L.audit(conn, actor, "TAX_EXEMPTION_REMOVED", f"nation:{nation_id}", {"note": note})
    return bool(cur.rowcount)


# ------------------------------------------------------------- brackets
def save_brackets(conn, brackets: list[dict]) -> int:
    n = 0
    for b in brackets:
        try:
            bid = int(b["id"])
        except (KeyError, TypeError, ValueError):
            continue
        conn.execute("INSERT INTO tax_brackets(id,data_json,synced_at) VALUES(?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, synced_at=excluded.synced_at",
                     (bid, jdump(b), now_iso()))
        n += 1
    return n


def stored_brackets(conn) -> dict:
    return {r["id"]: {**json.loads(r["data_json"]), "_synced_at": r["synced_at"]}
            for r in conn.execute("SELECT * FROM tax_brackets ORDER BY id")}


def rate_text(value) -> str:
    """Show a rate exactly as PnW reports it."""
    return "—" if value in (None, "") else f"{value}%"
