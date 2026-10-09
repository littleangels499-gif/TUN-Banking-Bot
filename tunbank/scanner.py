"""Automatic scanning of the PnW bank records (deposits, tax, outgoing confirmations)."""
from __future__ import annotations

import asyncio
import logging

from . import ledger as L
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from .util import utcnow, now_iso
from .valuation import value_amounts

log = logging.getLogger("tunbank.scanner")


class ScanResult:
    def __init__(self):
        self.outcomes: list = []
        self.ok = False
        self.error: str | None = None
        self.baseline = False
        self.seen = 0
        self.tax_seen = 0
        self.tax_error: str | None = None


class Scanner:
    def __init__(self, db, pnw, prices, settings):
        self.db, self.pnw, self.prices, self.s = db, pnw, prices, settings
        self._lock = asyncio.Lock()
        self.roster: dict = {}   # nation_id -> name, refreshed each scan (used by nation autocomplete)

    async def scan(self, actor: str = "system:scanner") -> ScanResult:
        res = ScanResult()
        async with self._lock:
            recs_by_id: dict = {}
            try:
                for bank in self.s.banks:                       # main, and offshore when configured
                    for r in await self.pnw.fetch_bankrecs(bank):
                        recs_by_id[int(r["id"])] = r            # a main<->offshore transfer shows in both lists
            except (PnWRejected, PnWUncertain) as exc:
                res.error = f"{bank.name} bank: {exc}"
                await asyncio.to_thread(self._note_failure, res.error)
                return res
            # PnW keeps tax collections in a separate feed. A problem there must never stop deposits being scanned.
            tax_ok = False
            try:
                for r in await self.pnw.fetch_taxrecs(self.s.main):
                    res.tax_seen += 1
                    rid = int(r["id"])
                    if rid in recs_by_id:
                        recs_by_id[rid] = dict(recs_by_id[rid], _taxrec=True)
                    else:
                        recs_by_id[rid] = r
                tax_ok = True
            except (PnWRejected, PnWUncertain) as exc:
                res.tax_error = str(exc)
                log.warning("tax records could not be read: %s", exc)
            recs = list(recs_by_id.values())
            try:
                members = await self.pnw.fetch_alliance_members()
            except (PnWRejected, PnWUncertain):
                members = None  # deposits that need the member list go to review, never guessed
            if members is not None:
                self.roster = dict(members)
            snap = await self.prices.get()
            res.seen = len(recs)
            try:
                res.outcomes, res.baseline = await asyncio.to_thread(self._apply, recs, members, snap, tax_ok, res.tax_error)
                res.ok = True
            except Exception as exc:  # noqa: BLE001
                log.exception("scan failed while applying records")
                res.error = f"{type(exc).__name__}: {exc}"
                await asyncio.to_thread(self._note_failure, res.error)
        return res

    def _note_failure(self, msg: str):
        with self.db.tx() as conn:
            L.set_state(conn, "last_scan_error", f"{now_iso()} {msg[:300]}")

    def _apply(self, recs, members, snap, tax_ok=False, tax_error=None):
        recs = sorted(recs, key=lambda r: int(r.get("id") or 0))
        outcomes = []
        with self.db.tx() as conn:
            baseline = L.get_state(conn, "scan_baseline_done") != "1"
            for rec in recs:
                # value each record for alerts/limits/flags
                try:
                    from . import bankrec as B
                    n = B.normalize(rec)
                    val = value_amounts(n["amounts"], snap)
                except Exception:  # noqa: BLE001
                    val = None
                ctx = REC.Ctx(self.s.alliance_id, members, snap.id if snap else None, val, baseline, self.s.bank_ids,
                              self.s.offshore.alliance_id if self.s.offshore else None)
                out = REC.process_record(conn, rec, ctx)
                out.valuation = val
                outcomes.append(out)
            outcomes += REC.credit_pending(conn, members)
            if baseline:
                L.set_state(conn, "scan_baseline_done", "1")
                L.audit(conn, "system", "SCAN_BASELINE", None,
                        {"records_stored_without_credit": len(recs)})
            if tax_ok and L.get_state(conn, "tax_feed_started") != "1":
                # First time the tax feed is read on this database: the ~14 days of history are stored as evidence and
                # marked as already announced, so the tax channel isn't flooded with old turns.
                n_old = conn.execute("UPDATE tax_turns SET alerted_at='backfill' WHERE alerted_at IS NULL").rowcount
                L.set_state(conn, "tax_feed_started", "1")
                L.audit(conn, "system", "TAX_FEED_STARTED", None, {"old_turns_marked_announced": n_old})
            fixed = sum(1 for o in outcomes if o.note == "TAX_RECLASSIFIED")
            if fixed:
                # Tax records an earlier version had wrongly sent to ECON review were put back where they belong.
                # Their turns are history: only the last few hours may still be announced.
                from datetime import timedelta
                cutoff = (utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H")
                n_old = conn.execute("UPDATE tax_turns SET alerted_at='backfill' WHERE alerted_at IS NULL AND turn_key < ?",
                                     (cutoff,)).rowcount
                L.audit(conn, "system", "TAX_RECLASSIFIED", None, {"records": fixed, "old_turns_not_announced": n_old})
            L.set_state(conn, "last_tax_fetch_error", tax_error or "")
            L.set_state(conn, "last_scan_ok", now_iso())
            L.set_state(conn, "last_scan_error", "")
        return outcomes, baseline
