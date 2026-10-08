"""The one and only valuation engine ("Current Market Value").

Used by dashboards, alerts, exports, limits - everything. Cash counts $1 each;
resources are valued at the latest PnW trade price. Every valuation remembers
WHICH price snapshot (and when) it used, and says so if prices are stale,
missing or suspicious. It never silently guesses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from . import money as M
from .config import cfg_int
from .util import now_iso, seconds_since

log = logging.getLogger("tunbank.valuation")


@dataclass
class Snapshot:
    id: int | None
    fetched_at: str
    prices: dict
    stale: bool = False
    suspicious: bool = False
    notes: list = field(default_factory=list)


@dataclass
class Valuation:
    snapshot_id: int | None
    as_of: str | None
    total_cents: int | None
    parts: dict
    complete: bool
    stale: bool
    suspicious: bool
    missing: list

    @property
    def usable_for_limits(self) -> bool:
        return self.complete and not self.suspicious


def value_amounts(amounts: dict, snap: Snapshot | None) -> Valuation:
    """Value {resource: units}. 1 unit of cash = 1 cent. Returns per-resource cents."""
    parts: dict = {}
    missing: list = []
    for res, units in amounts.items():
        if not units:
            continue
        if res == "money":
            parts[res] = int(units)
            continue
        price = snap.prices.get(res) if snap else None
        if price is None or Decimal(price) <= 0:
            parts[res] = None
            missing.append(res)
            continue
        parts[res] = int((Decimal(units) * Decimal(price)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    known = [v for v in parts.values() if v is not None]
    only_cash = all(r == "money" for r in parts)
    total = sum(known) if (known or not parts) else None
    return Valuation(
        snapshot_id=snap.id if snap else None,
        as_of=snap.fetched_at if snap else None,
        total_cents=total,
        parts=parts,
        complete=not missing,
        stale=bool(snap and snap.stale) and not only_cash,
        suspicious=bool(snap and snap.suspicious) and not only_cash,
        missing=missing,
    )


class PriceService:
    """Fetches, caches and stores PnW market prices."""

    def __init__(self, db, pnw):
        self.db = db
        self.pnw = pnw
        self._cached: Snapshot | None = None
        self._cached_mono = 0.0
        self._lock = asyncio.Lock()
        self.last_error: str | None = None     # why the last refresh failed (shown by /prices)
        self.last_ok_at: str | None = None

    def _load_last(self) -> Snapshot | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM price_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            prices = {k: Decimal(v) for k, v in json.loads(row["prices_json"]).items()}
            stale_after = cfg_int(conn, "price_stale_seconds")
        age = seconds_since(row["fetched_at"])
        return Snapshot(row["id"], row["fetched_at"], prices,
                        stale=(age is None or age > stale_after))

    def _store(self, prices: dict) -> Snapshot:
        with self.db.tx() as conn:
            prev = conn.execute("SELECT prices_json FROM price_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            warn_pct = cfg_int(conn, "price_change_warn_pct")
            suspicious = False
            notes = []
            if prev and warn_pct:
                old = json.loads(prev["prices_json"])
                for res, new in prices.items():
                    o = Decimal(old.get(res, "0"))
                    if o > 0 and abs(new - o) / o * 100 > warn_pct:
                        suspicious = True
                        notes.append(f"{res} moved {abs(new - o) / o * 100:.0f}%")
            ts = now_iso()
            cur = conn.execute(
                "INSERT INTO price_snapshots(fetched_at,prices_json,source) VALUES(?,?,?)",
                (ts, json.dumps({k: str(v) for k, v in prices.items()}, sort_keys=True), "pnw_tradeprices"))
            snap = Snapshot(cur.lastrowid, ts, prices, False, suspicious, notes)
            if suspicious:
                from .ledger import raise_event
                raise_event(conn, "WARNING", "PRICE_ANOMALY", ref_type="price_snapshot",
                            ref_id=snap.id, details={"notes": notes},
                            dedupe_key="price-anomaly")
        return snap

    async def get(self, force: bool = False) -> Snapshot | None:
        async with self._lock:
            with self.db.read() as conn:
                ttl = cfg_int(conn, "price_ttl_seconds")
            if (not force and self._cached and (time.monotonic() - self._cached_mono) < ttl):
                return self._cached
            try:
                raw = await self.pnw.fetch_prices()
                prices = {}
                for res in M.NON_CASH:
                    val = raw.get(res)
                    if val is not None and Decimal(val) > 0:
                        prices[res] = Decimal(val)
                if not prices:
                    raise ValueError("PnW returned no usable prices")
                # A few missing prices must not blank all the others: each resource is valued on its own,
                # and any resource without a price is clearly reported as such.
                snap = await asyncio.to_thread(self._store, prices)
                missing = [r for r in M.NON_CASH if r not in prices]
                if missing:
                    snap.notes.append("no price for: " + ", ".join(missing))
                self.last_error, self.last_ok_at = None, snap.fetched_at
                self._cached, self._cached_mono = snap, time.monotonic()
                return snap
            except Exception as exc:  # noqa: BLE001 - any failure -> fall back, flagged
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Price fetch failed (%s); falling back to last stored snapshot", exc)
                last = await asyncio.to_thread(self._load_last)
                if last:
                    last.notes.append(f"price refresh failed: {exc}")
                self._cached, self._cached_mono = last, time.monotonic() - max(0, ttl - 30)
                return last


def snapshot_by_id(conn, snapshot_id) -> "Snapshot | None":
    """Load a stored price snapshot (used by reconciliation to value the bank at the prices of that run)."""
    if snapshot_id is None:
        return None
    row = conn.execute("SELECT * FROM price_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        return None
    return Snapshot(row["id"], row["fetched_at"], {k: Decimal(v) for k, v in json.loads(row["prices_json"]).items()})
