"""Member notification outbox.

`enqueue` is called INSIDE the database transaction that records a withdrawal, adjustment, credit or external withdrawal.
So a notification exists if and only if the movement was really recorded, exactly once (the dedupe key is unique).
Delivery happens later and separately (AlertService.flush_notifications): a failed DM can never undo or interrupt the
transaction, and every failure stays in `member_notifications` for staff to review.
"""
from __future__ import annotations

import json

from .util import now_iso


def enqueue(conn, *, dedupe_key: str, nation_id: int, kind: str, title: str, deltas: dict, before: dict | None = None,
            after: dict | None = None, reason: str = "", reference: str = "", source: str = "", origin: str = "TUN Bank",
            bucket: str = "available") -> bool:
    """Queue one DM for the nation's linked Discord user. Returns False when there is nobody to tell or it was already queued.

    deltas: {resource: signed change}. before/after: the member's balances (only the affected resources are shown).
    """
    m = conn.execute("SELECT discord_id, nation_name FROM members WHERE nation_id=?", (nation_id,)).fetchone()
    if not m or not m["discord_id"]:
        return False                                          # not linked: no one to notify
    affected = sorted(deltas)
    payload = {
        "nation_id": nation_id, "nation_name": m["nation_name"], "title": title, "ts": now_iso(),
        "deltas": {r: int(deltas[r]) for r in affected},
        "before": {r: int((before or {}).get(r, 0)) for r in affected} if before is not None else None,
        "after": {r: int((after or {}).get(r, 0)) for r in affected} if after is not None else None,
        "reason": reason or "", "reference": reference or "", "source": source or "", "origin": origin, "bucket": bucket,
    }
    cur = conn.execute(
        "INSERT OR IGNORE INTO member_notifications(dedupe_key,nation_id,discord_id,kind,payload_json,status,created_at) "
        "VALUES(?,?,?,?,?,'PENDING',?)",
        (dedupe_key, nation_id, str(m["discord_id"]), kind, json.dumps(payload, sort_keys=True), now_iso()))
    return bool(cur.rowcount)


def pending(conn, limit: int = 25) -> list:
    return conn.execute("SELECT * FROM member_notifications WHERE status='PENDING' ORDER BY id LIMIT ?", (limit,)).fetchall()


def mark(conn, notification_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE member_notifications SET status=?, error=?, sent_at=? WHERE id=?",
                 (status, (error or "")[:300] or None, now_iso() if status == "SENT" else None, notification_id))


def failures(conn, limit: int = 15) -> list:
    """DMs that could not be delivered (closed DMs, blocked bot...), newest first, for staff review."""
    return conn.execute("SELECT * FROM member_notifications WHERE status='FAILED' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def failure_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM member_notifications WHERE status='FAILED'").fetchone()[0]
