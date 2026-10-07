"""Turning raw PnW bank records into clean, stored evidence."""
from __future__ import annotations

import re

from . import money as M
from .util import jdump, now_iso, sha256_text

TX_TAG_RE = re.compile(r"TUN-TX(\d+)")
HASHTAG_RE = re.compile(r"#[A-Za-z0-9_-]+")


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize(rec: dict) -> dict:
    """Validate one PnW `Bankrec` dict and convert amounts to exact units."""
    if rec.get("id") in (None, ""):
        raise ValueError("bank record has no id")
    from_tax_feed = bool(rec.get("_taxrec"))
    rec = {k: v for k, v in rec.items() if k != "_taxrec"}      # the stored raw record stays exactly what PnW sent
    amounts = {}
    for res in M.RESOURCES:
        u = M.to_units(rec.get(res))
        if u < 0:
            raise ValueError(f"bank record {rec.get('id')} has a negative {res} amount")
        if u:
            amounts[res] = u
    raw = jdump(rec)
    return {
        "id": int(rec["id"]),
        "record_date": str(rec.get("date") or ""),
        "sender_id": _int(rec.get("sender_id")),
        "sender_type": _int(rec.get("sender_type")),
        "receiver_id": _int(rec.get("receiver_id")),
        "receiver_type": _int(rec.get("receiver_type")),
        "banker_id": _int(rec.get("banker_id")),
        "note": (rec.get("note") or "")[:1000],
        "tax_id": _int(rec.get("tax_id")) or None,
        "is_tax": from_tax_feed or bool(_int(rec.get("tax_id"))),
        "amounts": amounts,
        "raw_json": raw,
        "raw_sha256": sha256_text(raw),
    }


def note_tags(note: str) -> set:
    return {t.lower() for t in HASHTAG_RE.findall(note or "")}


def tx_tag(note: str) -> int | None:
    m = TX_TAG_RE.search(note or "")
    return int(m.group(1)) if m else None


def get_record(conn, record_id: int):
    return conn.execute("SELECT * FROM pnw_records WHERE id=?", (record_id,)).fetchone()


def insert_record(conn, n: dict, *, direction: str, classification: str, status: str,
                  credited_nation_id=None, tx_id=None, snapshot_id=None) -> None:
    conn.execute(
        "INSERT INTO pnw_records(id,record_date,sender_id,sender_type,receiver_id,receiver_type,"
        "banker_id,note,tax_id,amounts_json,raw_json,raw_sha256,direction,classification,status,"
        "credited_nation_id,tx_id,price_snapshot_id,first_seen_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (n["id"], n["record_date"], n["sender_id"], n["sender_type"], n["receiver_id"],
         n["receiver_type"], n["banker_id"], n["note"], n["tax_id"], jdump(n["amounts"]),
         n["raw_json"], n["raw_sha256"], direction, classification, status,
         credited_nation_id, tx_id, snapshot_id, now_iso()),
    )
