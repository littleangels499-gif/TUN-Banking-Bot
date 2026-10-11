"""Nation identity: ONE place that turns nation ids into names (and names into ids) for the whole bot.

  * The nation ID is always the real identifier (database, ledger, audit). Names are only for people to read.
  * A cache holds the last name PnW gave us (memory + the `nation_names` table). It is filled automatically by every PnW
    call the bot already makes (alliance roster, nation lookups, trades) and refreshed in the background, so renamed
    nations don't stay mislabelled and OLD records get their names too.
  * If a name isn't known, people see "Unknown Nation (ID: 123456)" - never a bare number.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time

from .util import now_iso

log = logging.getLogger("tunbank.identity")
STALE_SECONDS = 24 * 3600

_db = None
_cache: dict[int, dict] = {}          # nation_id -> {name, alliance_id, alliance_name, at (epoch)}
_dirty: set = set()


def init(db) -> None:
    """Load what is already known (the saved cache, then the members table) so labels work from the first second."""
    global _db
    _db = db
    _cache.clear()
    _dirty.clear()
    with db.read() as conn:
        for r in conn.execute("SELECT nation_id, nation_name FROM members WHERE nation_name IS NOT NULL AND nation_name != ''"):
            _cache[r["nation_id"]] = {"name": r["nation_name"], "alliance_id": None, "alliance_name": None, "at": 0.0}
        for r in conn.execute("SELECT * FROM nation_names"):
            _cache[r["nation_id"]] = {"name": r["name"], "alliance_id": r["alliance_id"], "alliance_name": r["alliance_name"],
                                      "at": _epoch(r["fetched_at"])}


def _epoch(iso: str) -> float:
    try:
        import datetime as dt
        return dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def remember(nation_id, name, alliance_id=None, alliance_name=None) -> None:
    """Record a name PnW just told us (memory now, saved to the database by flush())."""
    try:
        nid = int(nation_id)
    except (TypeError, ValueError):
        return
    name = (name or "").strip()
    if not nid or not name:
        return
    old = _cache.get(nid)
    _cache[nid] = {"name": name, "alliance_id": alliance_id if alliance_id is not None else (old or {}).get("alliance_id"),
                   "alliance_name": alliance_name or (old or {}).get("alliance_name"), "at": time.time()}
    _dirty.add(nid)


def remember_info(info: dict | None) -> None:
    """From a PnW nation row: {id, nation_name, alliance_id, alliance{name}}."""
    if not info:
        return
    ali = info.get("alliance") or {}
    remember(info.get("id"), info.get("nation_name"), info.get("alliance_id") and int(info["alliance_id"]), ali.get("name"))


def flush(conn) -> int:
    n = 0
    for nid in list(_dirty):
        c = _cache.get(nid)
        if not c:
            continue
        conn.execute("INSERT INTO nation_names(nation_id,name,alliance_id,alliance_name,fetched_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(nation_id) DO UPDATE SET name=excluded.name, alliance_id=excluded.alliance_id, "
                     "alliance_name=excluded.alliance_name, fetched_at=excluded.fetched_at",
                     (nid, c["name"], c["alliance_id"], c["alliance_name"], now_iso()))
        _dirty.discard(nid)
        n += 1
    return n


# ------------------------------------------------------------------ reading
def name(nation_id) -> str | None:
    try:
        nid = int(nation_id)
    except (TypeError, ValueError):
        return None
    c = _cache.get(nid)
    if c is None and _db is not None:                 # a member added after start-up: the members table knows the name
        try:
            with _db.read() as conn:
                r = conn.execute("SELECT nation_name FROM members WHERE nation_id=?", (nid,)).fetchone()
        except Exception:  # noqa: BLE001
            r = None
        if r and r["nation_name"]:
            _cache[nid] = c = {"name": r["nation_name"], "alliance_id": None, "alliance_name": None, "at": 0.0}
    return c["name"] if c else None


def info(nation_id) -> dict | None:
    try:
        return _cache.get(int(nation_id))
    except (TypeError, ValueError):
        return None


def label(nation_id, name_hint: str | None = None) -> str:
    """'Igala [#123456]'. Unknown -> 'Unknown Nation (ID: 123456)'. No nation -> a dash."""
    if not nation_id:
        return "—"
    nm = (name_hint or "").strip() or name(nation_id)
    return f"{nm} [#{int(nation_id)}]" if nm else f"Unknown Nation (ID: {int(nation_id)})"


_REF = re.compile(r"\b[Nn]ation(?::|\s)\s*\[?#?(\d{1,9})\]?")


def decorate(text) -> str:
    """For DISPLAY only: put the nation's name into stored text that mentions it by id (audit logs, targets).
    The stored text is never changed, so old records get names too wherever the name is known."""
    return _REF.sub(lambda m: label(int(m.group(1))), str(text or ""))


def exact(text: str) -> dict:
    """{id: name} of every KNOWN nation (any alliance) whose name equals `text`, ignoring capitals."""
    key = (text or "").strip().lower()
    return {nid: c["name"] for nid, c in _cache.items() if c["name"].lower() == key}


def suggest(text: str, limit: int = 8) -> list[tuple[int, str]]:
    """Known nations whose name contains the text, then close spellings. For autocomplete and 'did you mean'."""
    key = (text or "").strip().lower()
    if not key:
        return []
    hits = [(nid, c["name"]) for nid, c in _cache.items() if key in c["name"].lower() or key == str(nid)]
    hits.sort(key=lambda kv: (not kv[1].lower().startswith(key), len(kv[1]), kv[1].lower()))
    out = hits[:limit]
    if len(out) < limit:
        names = {c["name"].lower(): nid for nid, c in _cache.items()}
        for m in difflib.get_close_matches(key, list(names), n=limit, cutoff=0.72):
            nid = names[m]
            if all(nid != o[0] for o in out):
                out.append((nid, _cache[nid]["name"]))
    return out[:limit]


# ------------------------------------------------------------------ asking PnW
async def ensure(pnw, ids, max_age: float = STALE_SECONDS) -> None:
    """Make sure these nations have a fresh-enough name (one batched PnW request for the ones that don't). Never raises."""
    now = time.time()
    want = []
    for x in ids:
        try:
            nid = int(x)
        except (TypeError, ValueError):
            continue
        c = _cache.get(nid)
        if nid and (c is None or now - c["at"] > max_age):
            want.append(nid)
    if not want:
        return
    try:
        rows = await pnw.fetch_nations_info(sorted(set(want))[:50])
    except Exception as exc:  # noqa: BLE001 - names are a convenience; PnW being down must never break a command
        log.warning("could not refresh nation names: %s", exc)
        return
    for r in rows.values():
        remember_info(r)


async def find_by_name(pnw, text: str) -> list[dict]:
    """Search ALL of Politics & War for a nation by name (not just our alliance). Returns [{id, name, alliance_id, alliance}].
    Raises whatever the PnW client raises if PnW can't be reached, so the caller can say so honestly."""
    t = (text or "").strip()
    rows = await pnw.search_nations_by_name(t)
    for r in rows:
        remember_info(r)
    return rows


def pending_ids(conn, limit: int = 40) -> list[int]:
    """Nations that appear in our records but have no (or an old) name: the background job fills these in so OLD records
    and exports get names too."""
    stale = now_iso()
    rows = conn.execute(
        "WITH seen(id) AS ("
        " SELECT nation_id FROM members UNION SELECT nation_id FROM balances"
        " UNION SELECT member_nation_id FROM transactions WHERE member_nation_id IS NOT NULL"
        " UNION SELECT dest_nation_id FROM transactions WHERE dest_nation_id IS NOT NULL"
        " UNION SELECT credited_nation_id FROM pnw_records WHERE credited_nation_id IS NOT NULL"
        " UNION SELECT sender_id FROM pnw_records WHERE sender_type=1"
        " UNION SELECT receiver_id FROM pnw_records WHERE receiver_type=1"
        " UNION SELECT recipient_nation_id FROM grants WHERE recipient_nation_id IS NOT NULL"
        " UNION SELECT nation_id FROM loans UNION SELECT nation_id FROM tax_records"
        " UNION SELECT member_nation_id FROM trade_alerts UNION SELECT counterparty_id FROM trade_alerts WHERE counterparty_id IS NOT NULL)"
        " SELECT id FROM seen WHERE id IS NOT NULL AND id > 0 ORDER BY id DESC LIMIT 2000").fetchall()
    now = time.time()
    missing = [r["id"] for r in rows if r["id"] not in _cache]
    old = [r["id"] for r in rows if r["id"] in _cache and now - _cache[r["id"]]["at"] > STALE_SECONDS]
    return (missing + old)[:limit]


async def refresh_some(db, pnw, limit: int = 40) -> int:
    """One background step: fetch a batch of missing/old names, then save the cache."""
    def pick():
        with db.read() as conn:
            return pending_ids(conn, limit)
    ids = await asyncio.to_thread(pick)
    if ids:
        await ensure(pnw, ids, max_age=0)

    def save():
        with db.tx() as conn:
            return flush(conn)
    return await asyncio.to_thread(save)
