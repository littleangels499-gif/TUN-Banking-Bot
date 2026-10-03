"""Expected deposits. A member says what they are ABOUT to deposit; the bot gives exact in-game steps.

An intent is a note, never money: it writes nothing to the ledger. A balance only appears when the scanner
sees the REAL PnW deposit record. Matching just lets the bot tell the member 'this is the deposit you planned'.
"""
from __future__ import annotations

import datetime as dt
import json

from .util import ISO_FMT, jdump, now_iso, utcnow

HOURS = 24


def create(conn, nation_id: int, amounts: dict) -> int:
    conn.execute("UPDATE deposit_intents SET status='EXPIRED' WHERE status='WAITING' AND expires_at<?", (now_iso(),))
    exp = (utcnow() + dt.timedelta(hours=HOURS)).strftime(ISO_FMT)
    cur = conn.execute("INSERT INTO deposit_intents(nation_id,amounts_json,created_at,expires_at,status) VALUES(?,?,?,?,'WAITING')",
                       (nation_id, jdump(amounts), now_iso(), exp))
    return cur.lastrowid


def match(conn, nation_id: int, amounts: dict, record_id: int) -> int | None:
    """Mark the oldest waiting intent with exactly these amounts as MATCHED by a real PnW record."""
    for r in conn.execute("SELECT id, amounts_json FROM deposit_intents WHERE nation_id=? AND status='WAITING' "
                          "AND expires_at>=? ORDER BY id", (nation_id, now_iso())).fetchall():
        if json.loads(r["amounts_json"]) == amounts:
            conn.execute("UPDATE deposit_intents SET status='MATCHED', pnw_record_id=? WHERE id=?", (record_id, r["id"]))
            return r["id"]
    return None
