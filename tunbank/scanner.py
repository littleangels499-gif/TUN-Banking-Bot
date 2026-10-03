"""Automatic scanning of the PnW bank records (deposits, tax, outgoing confirmations)."""
from __future__ import annotations

import asyncio
import logging

from . import ledger as L
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from .util import now_iso
from .valuation import value_amounts

log = logging.getLogger("tunbank.scanner")


class ScanResult:
    def __init__(self):
        self.outcomes: list = []
        self.ok = False
        self.error: str | None = None
        self.baseline = False
        self.seen = 0


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
                res.outcomes, res.baseline = await asyncio.to_thread(self._apply, recs, members, snap)
                res.ok = True
            except Exception as exc:  # noqa: BLE001
                log.exception("scan failed while applying records")
                res.error = f"{type(exc).__name__}: {exc}"
                await asyncio.to_thread(self._note_failure, res.error)
        return res

    def _note_failure(self, msg: str):
        with self.db.tx() as conn:
            L.set_state(conn, "last_scan_error", f"{now_iso()} {msg[:300]}")

    def _apply(self, recs, members, snap):
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
                ctx = REC.Ctx(self.s.alliance_id, members, snap.id if snap else None, val, baseline, self.s.bank_ids)
                out = REC.process_record(conn, rec, ctx)
                out.valuation = val
                outcomes.append(out)
            outcomes += REC.credit_pending(conn, members)
            if baseline:
                L.set_state(conn, "scan_baseline_done", "1")
                L.audit(conn, "system", "SCAN_BASELINE", None,
                        {"records_stored_without_credit": len(recs)})
            L.set_state(conn, "last_scan_ok", now_iso())
            L.set_state(conn, "last_scan_error", "")
        return outcomes, baseline
