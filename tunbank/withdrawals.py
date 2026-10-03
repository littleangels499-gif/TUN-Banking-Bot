"""Executing withdrawals safely.

Flow (spec section 9):
  validate -> [confirmation shown by the Discord layer] -> hold funds ->
  call PnW ONCE -> verify success -> book the ledger in one DB transaction ->
  store the PnW id -> alerts.

Guarantees:
  * A transfer is sent to PnW at most once (see ledger.mark_attempt).
  * If we can't tell whether PnW executed it, nothing is guessed: the hold stays,
    ECON is alerted, and the bank records are checked for our TUN-TX tag.
  * A failed transfer never changes a balance.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import bankrec as B
from . import ledger as L
from . import limits as LIM
from . import money as M
from . import reconcile as R
from .banks import live_holdings
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from .valuation import value_amounts

log = logging.getLogger("tunbank.withdrawals")


@dataclass
class Result:
    status: str          # COMPLETED, FAILED, UNCERTAIN, IN_PROGRESS, BLOCKED
    message: str
    tx_id: int | None = None
    pnw_record_id: int | None = None
    valuation: object | None = None
    before: dict | None = None
    after: dict | None = None
    replay: bool = False


class WithdrawalService:
    def __init__(self, db, pnw, prices, settings):
        self.db, self.pnw, self.prices, self.s = db, pnw, prices, settings

    # ---------------------------------------------------------- planning
    async def prepare(self, *, funding_source: str, amounts: dict):
        """Live data needed BEFORE showing a confirmation screen."""
        snap = await self.prices.get()
        val = value_amounts(amounts, snap)
        alliance_free = None
        if funding_source == "ALLIANCE":
            holdings, _ = await live_holdings(self.pnw, self.s)  # raises if ANY bank is unreadable
            with self.db.read() as conn:
                pos = R.bank_position(conn, holdings)
            alliance_free = {r: v for r, v in (pos["alliance_owned"] or {}).items() if v > 0}
        return snap, val, alliance_free

    # ----------------------------------------------------------- request
    async def request(self, *, tx_type, funding_source, member_nation_id, lock_id, dest_nation_id,
                      amounts, actor, note, reason, idempotency_key, actor_role_ids=(),
                      approver=None) -> Result:
        try:
            snap, val, alliance_free = await self.prepare(funding_source=funding_source, amounts=amounts)
        except (PnWRejected, PnWUncertain) as exc:
            return Result("BLOCKED", f"Could not read the live PnW bank, so nothing was sent: {exc}")

        # The bank that actually pays (offshore when configured) must physically hold the funds.
        try:
            phys = await self.pnw.fetch_bank_holdings(self.s.payout)
        except (PnWRejected, PnWUncertain) as exc:
            return Result("BLOCKED", f"Could not read the live PnW bank, so nothing was sent: {exc}")
        short = [r for r, a in amounts.items() if phys.get(r, 0) < a]
        if short:
            where = self.s.payout.name
            hint = " ECON can move funds there with /bank offshore." if self.s.offshore else ""
            return Result("BLOCKED", f"The {where} bank does not physically hold enough " +
                          ", ".join(M.LABELS[r] for r in short) + f" right now.{hint}", valuation=val)

        def begin():
            with self.db.tx() as conn:
                existing = conn.execute("SELECT id FROM transactions WHERE idempotency_key=?",
                                        (idempotency_key,)).fetchone()
                if existing:
                    return existing["id"], False, None
                LIM.check(conn, is_self=(tx_type == "WITHDRAW_SELF"), nation_id=member_nation_id,
                          actor_id=actor, actor_role_ids=actor_role_ids, amounts=amounts, valuation=val)
                tx_id, created = L.begin_withdrawal(
                    conn, tx_type=tx_type, funding_source=funding_source,
                    member_nation_id=member_nation_id, lock_id=lock_id, dest_nation_id=dest_nation_id,
                    amounts=amounts, actor=actor, note=note, reason=reason,
                    idempotency_key=idempotency_key, snapshot_id=val.snapshot_id,
                    value_cents=val.total_cents, alliance_free=alliance_free, approver=approver)
                return tx_id, created, None

        try:
            tx_id, created, _ = await asyncio.to_thread(begin)
        except L.LedgerError as exc:
            return Result("BLOCKED", str(exc), valuation=val)

        if not created:
            return await asyncio.to_thread(self._replay, tx_id, val)

        gate = await asyncio.to_thread(self._gate, tx_id)
        if not gate:
            return Result("IN_PROGRESS", "This transfer is already being processed.", tx_id=tx_id, valuation=val)
        return await self._send(tx_id, dest_nation_id, amounts, note, val)

    def _gate(self, tx_id) -> bool:
        with self.db.tx() as conn:
            return L.mark_attempt(conn, tx_id)

    def _replay(self, tx_id, val) -> Result:
        with self.db.read() as conn:
            tx, _ = L.get_tx(conn, tx_id)
        return self._result_from_tx(tx, val, replay=True)

    def _result_from_tx(self, tx, val=None, replay=False) -> Result:
        st = tx["status"]
        if st == "COMPLETED":
            return Result("COMPLETED", "Transfer completed.", tx["id"], tx["pnw_record_id"], val, replay=replay)
        if st == "FAILED":
            return Result("FAILED", tx["failure_reason"] or "Transfer failed.", tx["id"], valuation=val, replay=replay)
        if st == "RECONCILIATION_REQUIRED":
            return Result("UNCERTAIN", "The outcome is not confirmed yet; ECON has been alerted.",
                          tx["id"], tx["pnw_record_id"], val, replay=replay)
        return Result("IN_PROGRESS", "This transfer is still being processed.", tx["id"], valuation=val, replay=replay)

    # -------------------------------------------------------------- send
    async def _send(self, tx_id, dest_nation_id, amounts, note, val) -> Result:
        pnw_note = f"{(note or 'TUN Bank').strip()[:100]} TUN-TX{tx_id}"
        try:
            rec = await self.pnw.bank_withdraw(dest_nation_id, amounts, pnw_note, bank=self.s.payout)
        except PnWRejected as exc:
            await asyncio.to_thread(self._fail, tx_id, f"PnW refused the transfer: {exc}")
            return Result("FAILED", f"PnW refused the transfer, no funds moved: {exc}", tx_id, valuation=val)
        except PnWUncertain as exc:
            log.warning("tx %s outcome unknown: %s", tx_id, exc)
            return await self._resolve_unknown(tx_id, f"PnW did not give a clear answer ({exc})", val)
        except Exception as exc:  # noqa: BLE001
            log.exception("tx %s unexpected error", tx_id)
            return await self._resolve_unknown(tx_id, f"Unexpected error: {type(exc).__name__}", val)

        try:
            return await asyncio.to_thread(self._finalize, tx_id, rec, amounts, dest_nation_id, val)
        except Exception as exc:  # noqa: BLE001
            log.exception("tx %s could not be booked", tx_id)
            await asyncio.to_thread(self._uncertain, tx_id, f"PnW sent it but booking failed: {exc}")
            return Result("UNCERTAIN", "PnW accepted the transfer but booking it failed; ECON was alerted "
                          "and the balance stays on hold.", tx_id, valuation=val)

    def _fail(self, tx_id, reason):
        with self.db.tx() as conn:
            L.fail_tx(conn, tx_id, reason)

    def _uncertain(self, tx_id, reason):
        with self.db.tx() as conn:
            L.mark_uncertain(conn, tx_id, reason)

    def _finalize(self, tx_id, rec, amounts, dest_nation_id, val) -> Result:
        """PnW returned a record: verify it matches, store evidence, book the ledger."""
        n = B.normalize(rec)
        ok = (n["amounts"] == amounts and n["receiver_id"] == dest_nation_id
              and n["sender_id"] == self.s.payout.alliance_id and n["sender_type"] == 2)
        # Step 1: evidence first (own transaction) - even if booking later fails.
        with self.db.tx() as conn:
            existing = B.get_record(conn, n["id"])
            if existing is None:
                if ok:
                    B.insert_record(conn, n, direction="OUT", classification="OUTGOING_TX",
                                    status="LINKED_TX", tx_id=tx_id, snapshot_id=val.snapshot_id)
                else:
                    B.insert_record(conn, n, direction="OUT", classification="EXTERNAL_OUTFLOW",
                                    status="AWAITING_REVIEW", snapshot_id=val.snapshot_id)
            if not ok:
                L.raise_event(conn, "CRITICAL", "TX_MISMATCH", ref_type="tx", ref_id=tx_id,
                              details={"sent": amounts, "pnw_record": n["amounts"], "record_id": n["id"]},
                              dedupe_key=f"tx-mismatch:{tx_id}")
                tx, _ = L.get_tx(conn, tx_id)
                L.mark_uncertain(conn, tx_id, "PnW's record does not match the requested transfer")
                return Result("UNCERTAIN", "PnW's record differs from what was requested. ECON was alerted.",
                              tx_id, n["id"], val)
            L.attach_pnw_record(conn, tx_id, n["id"])
        # Step 2: book the ledger and mark COMPLETED (all-or-nothing).
        with self.db.tx() as conn:
            tx, _ = L.get_tx(conn, tx_id)
            before = tx["balance_before_json"]
            L.complete_withdrawal(conn, tx_id)
            tx, _ = L.get_tx(conn, tx_id)
        import json
        return Result("COMPLETED", "Transfer completed.", tx_id, n["id"], val,
                      before=json.loads(tx["balance_before_json"] or "{}"),
                      after=json.loads(tx["balance_after_json"] or "{}"))

    async def _resolve_unknown(self, tx_id, why, val) -> Result:
        """We don't know if PnW executed it. Look for our TUN-TX tag in the bank records."""
        await asyncio.sleep(2)
        found = None
        try:
            found = await self.lookup_and_link(tx_id)
        except (PnWRejected, PnWUncertain):
            found = None
        if found == "COMPLETED":
            with self.db.read() as conn:
                tx, _ = L.get_tx(conn, tx_id)
            return Result("COMPLETED", "Transfer completed (confirmed from the PnW bank records).",
                          tx_id, tx["pnw_record_id"], val)
        await asyncio.to_thread(self._uncertain, tx_id, why)
        return Result("UNCERTAIN",
                      "I could not confirm whether PnW sent this transfer. Your balance is ON HOLD "
                      "(not deducted, not spendable). ECON has been alerted and it will be settled "
                      "automatically when the PnW record appears.", tx_id, valuation=val)

    async def lookup_and_link(self, tx_id: int) -> str:
        """Search the PnW bank records for this transaction's tag. Returns the tx status."""
        recs = await self.pnw.fetch_bankrecs(self.s.payout)
        tag = f"TUN-TX{tx_id}"
        mine = [r for r in recs if tag in (r.get("note") or "")]

        def apply():
            with self.db.tx() as conn:
                for r in mine:
                    REC.process_record(conn, r, REC.Ctx(self.s.alliance_id, members=None, bank_ids=self.s.bank_ids))
                tx, _ = L.get_tx(conn, tx_id)
                return tx["status"]

        return await asyncio.to_thread(apply)

    # ------------------------------------------------- staff resolution
    async def resolve(self, tx_id: int, action: str, actor: str) -> str:
        """action: 'check' (look for the PnW record) or 'mark_failed'."""
        status = await self.lookup_and_link(tx_id)  # always look first
        if status == "COMPLETED":
            return f"Transaction #{tx_id} is COMPLETED: the PnW record was found and booked."
        if action == "check":
            return f"Transaction #{tx_id} is still {status}; no matching PnW record found yet."
        if action != "mark_failed":
            raise L.LedgerError("Unknown action.")

        def fail():
            with self.db.tx() as conn:
                tx, _ = L.get_tx(conn, tx_id)
                if tx["status"] not in L.IN_FLIGHT:
                    raise L.LedgerError(f"Transaction is {tx['status']}; nothing to fail.")
                from .util import seconds_since
                if (seconds_since(tx["attempted_at"]) or 0) < 300 and tx["attempted_at"]:
                    raise L.LedgerError("Wait at least 5 minutes after the attempt before marking it failed.")
                if not L.fail_tx(conn, tx_id, f"Marked failed by staff after checking PnW records ({actor})"):
                    raise L.LedgerError("Could not mark failed (a PnW record exists for it).")
                L.audit(conn, actor, "TX_MARKED_FAILED", f"tx:{tx_id}", {})
            return "ok"

        await asyncio.to_thread(fail)
        return f"Transaction #{tx_id} marked FAILED (no PnW record found). The hold was released."

    # ---------------------------------------------------------- startup
    async def recover(self) -> list[str]:
        """After a crash/restart: settle transfers that were mid-flight."""
        notes = []

        def load():
            with self.db.read() as conn:
                return [dict(r) for r in conn.execute(
                    "SELECT id, attempted_at, pnw_record_id, status FROM transactions "
                    "WHERE status IN ('CONFIRMED','RECONCILIATION_REQUIRED')")]

        for tx in await asyncio.to_thread(load):
            tid = tx["id"]
            if tx["status"] == "CONFIRMED" and tx["attempted_at"] is None:
                await asyncio.to_thread(self._fail, tid, "Bot restarted before this transfer was sent")
                notes.append(f"tx #{tid}: never sent, released")
            elif tx["pnw_record_id"] is not None:
                def fin(tid=tid):
                    with self.db.tx() as conn:
                        L.complete_withdrawal(conn, tid)
                try:
                    await asyncio.to_thread(fin)
                    notes.append(f"tx #{tid}: booked from stored PnW record")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"tx #{tid}: could not book ({exc})")
            else:
                try:
                    st = await self.lookup_and_link(tid)
                    if st != "COMPLETED":
                        await asyncio.to_thread(self._uncertain, tid, "Bot restarted while this transfer was in flight")
                    notes.append(f"tx #{tid}: {st}")
                except (PnWRejected, PnWUncertain):
                    await asyncio.to_thread(self._uncertain, tid, "Bot restarted while this transfer was in flight")
                    notes.append(f"tx #{tid}: uncertain (PnW unreachable)")
        return notes
