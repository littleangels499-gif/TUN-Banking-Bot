"""Moving funds between OUR OWN banks (main -> offshore).

Hard rules (same financial-integrity rules as everything else):
  * It changes where money physically sits. It NEVER touches a member balance, a lock, tax or #ignore records.
  * Every transfer is tied to a real PnW bank record; nothing is marked done without one.
  * It is sent to PnW at most once. If the answer is unclear, the bot looks for its TUN-OFF<id> tag in the
    PnW records instead of guessing or re-sending.
  * Sending needs the credentials of a nation INSIDE the main alliance (PnW performs a withdrawal as the nation
    that owns the API key). Without them the bot runs in MANUAL mode: ECON sends it in-game with the tag and the
    bot completes the record when it sees the real transfer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from . import bankrec as B
from . import ledger as L
from . import money as M
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from .util import jdump, now_iso

log = logging.getLogger("tunbank.offshore")
OFF_TAG = re.compile(r"TUN-OFF(\d+)")


def off_tag(note: str) -> int | None:
    m = OFF_TAG.search(note or "")
    return int(m.group(1)) if m else None


class OffshoreService:
    def __init__(self, db, pnw, prices, settings):
        self.db, self.pnw, self.prices, self.s = db, pnw, prices, settings

    @property
    def enabled(self) -> bool:
        return self.s.offshore is not None

    @property
    def mode(self) -> str:
        m = self.s.main
        return "AUTO" if (m.bot_key and m.api_key) else "MANUAL"

    def note_for(self, tid: int, reason: str = "") -> str:
        return f"{(reason or 'offshore').strip()[:60]} TUN-OFF{tid}"

    # ---------------------------------------------------------------- create
    @property
    def payout_mode(self) -> str:
        """Payouts are sent from the OFFSHORE bank, so they need the offshore's own credentials."""
        o = self.s.offshore
        return "AUTO" if (o and o.bot_key and o.api_key) else "MANUAL"

    def create(self, *, actor: str, amounts: dict, reason: str, key: str, value_cents, snapshot_id,
               direction: str = "TO_OFFSHORE", alliance_id: int | None = None, dest: tuple | None = None) -> tuple[int, bool]:
        """direction TO_OFFSHORE = main -> offshore (the original use). direction PAYOUT = the bot sends from the shared
        offshore on behalf of registered alliance `alliance_id` to dest=(receiver_type, receiver_id)."""
        from . import offshore_ledger as OL

        with self.db.tx() as conn:
            old = conn.execute("SELECT id FROM offshore_transfers WHERE idempotency_key=?", (key,)).fetchone()
            if old:
                return old["id"], False
            L.assert_can_mutate(conn, None, "withdraw")
            mode = self.mode
            if direction == "PAYOUT":
                if not (OL.shared_enabled(conn) and alliance_id and dest):
                    raise L.LedgerError("Shared offshore mode is not on, or the payout is incomplete.")
                if not OL.registered(conn, alliance_id):
                    raise L.LedgerError("That alliance is not registered in the offshore.")
                free = OL.spendable(conn, alliance_id)         # share minus payouts already promised
                short = [M.LABELS[r] for r, a in amounts.items() if free.get(r, 0) < a]
                if short:
                    raise L.LedgerError("That alliance's share in the offshore doesn't cover: " + ", ".join(short))
                mode = self.payout_mode
            now = now_iso()
            cur = conn.execute(
                "INSERT INTO offshore_transfers(created_at,updated_at,direction,mode,status,actor,reason,amounts_json,"
                "value_cents,price_snapshot_id,idempotency_key,alliance_id,dest_type,dest_id) VALUES(?,?,?,?,'PLANNED',?,?,?,?,?,?,?,?,?)",
                (now, now, direction, mode, str(actor), reason, jdump(amounts), value_cents, snapshot_id, key, alliance_id,
                 dest[0] if dest else None, dest[1] if dest else None))
            L.audit(conn, actor, "OFFSHORE_PLANNED", f"offshore:{cur.lastrowid}",
                    {"amounts": amounts, "mode": mode, "reason": reason, "direction": direction, "alliance_id": alliance_id})
            return cur.lastrowid, True

    def get(self, tid: int):
        with self.db.read() as conn:
            r = conn.execute("SELECT * FROM offshore_transfers WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def cancel(self, tid: int, actor: str) -> bool:
        """Cancel a plan that was never sent. Refused once PnW may have moved money."""
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE offshore_transfers SET status='CANCELLED', updated_at=? WHERE id=? "
                               "AND status='PLANNED' AND attempted_at IS NULL", (now_iso(), tid))
            if cur.rowcount:
                L.audit(conn, actor, "OFFSHORE_CANCELLED", f"offshore:{tid}", {})
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- send
    async def execute_auto(self, tid: int) -> dict:
        """Send a PLANNED AUTO transfer once. Returns {status, message}."""
        def gate():
            with self.db.tx() as conn:
                cur = conn.execute("UPDATE offshore_transfers SET attempted_at=?, status='PENDING', updated_at=? "
                                   "WHERE id=? AND mode='AUTO' AND status='PLANNED' AND attempted_at IS NULL",
                                   (now_iso(), now_iso(), tid))
                return cur.rowcount == 1
        if not await asyncio.to_thread(gate):
            row = self.get(tid)
            return {"status": row["status"] if row else "UNKNOWN", "message": "This transfer was already handled."}
        row = self.get(tid)
        amounts = json.loads(row["amounts_json"])
        try:
            if row["direction"] == "PAYOUT":         # from the shared offshore, to whoever the registered alliance chose
                rec = await self.pnw.bank_withdraw(row["dest_id"], amounts, self.note_for(tid, row["reason"]),
                                                   receiver_type=row["dest_type"], bank=self.s.offshore)
            else:
                rec = await self.pnw.bank_withdraw(self.s.offshore.alliance_id, amounts, self.note_for(tid, row["reason"]),
                                                   receiver_type=self.s.alliance_receiver_type, bank=self.s.main)
        except PnWRejected as exc:
            hint = ""
            if any(w in str(exc).lower() for w in ("receiver", "type")):
                hint = (f" The bot used receiver_type={self.s.alliance_receiver_type} for \"send to an alliance\". That number is a setting "
                        "(ALLIANCE_RECEIVER_TYPE in .env), not an assumption: if PnW's message above says it is wrong, change it.")
            await asyncio.to_thread(self._set, tid, "FAILED", f"PnW refused the transfer: {exc}")
            return {"status": "FAILED", "message": f"PnW refused the transfer, nothing moved: {exc}.{hint}"}
        except PnWUncertain as exc:
            return await self._unknown(tid, f"PnW did not give a clear answer ({exc})")
        except Exception as exc:  # noqa: BLE001
            log.exception("offshore %s unexpected", tid)
            return await self._unknown(tid, f"Unexpected error: {type(exc).__name__}")
        # evidence + link: the record is processed exactly like one seen by the scanner
        def book():
            with self.db.tx() as conn:
                REC.process_record(conn, rec, REC.Ctx(self.s.alliance_id, members=None, bank_ids=self.s.bank_ids, offshore_id=self.s.offshore.alliance_id if self.s.offshore else None))
                return conn.execute("SELECT status FROM offshore_transfers WHERE id=?", (tid,)).fetchone()["status"]
        try:
            status = await asyncio.to_thread(book)
        except Exception as exc:  # noqa: BLE001
            log.exception("offshore %s could not be booked", tid)
            await asyncio.to_thread(self._uncertain, tid, f"PnW answered but booking failed: {exc}")
            return {"status": "UNCERTAIN", "message": "PnW answered but booking failed; ECON was alerted. Nothing will be re-sent."}
        if status == "COMPLETED":
            return {"status": "COMPLETED", "message": "Transfer completed.", "record_id": int(rec["id"])}
        await asyncio.to_thread(self._uncertain, tid, "PnW's record did not match the requested transfer")
        return {"status": "UNCERTAIN", "message": "PnW's record differs from what was requested; ECON was alerted."}

    def _set(self, tid, status, why=None):
        with self.db.tx() as conn:
            conn.execute("UPDATE offshore_transfers SET status=?, failure_reason=?, updated_at=? WHERE id=? "
                         "AND status IN ('PENDING','PLANNED')", (status, why, now_iso(), tid))
            L.audit(conn, "system", "OFFSHORE_" + status, f"offshore:{tid}", {"why": why})

    def _uncertain(self, tid, why):
        with self.db.tx() as conn:
            conn.execute("UPDATE offshore_transfers SET status='UNCERTAIN', failure_reason=?, updated_at=? WHERE id=? "
                         "AND status IN ('PENDING','PLANNED')", (why[:300], now_iso(), tid))
            L.raise_event(conn, "WARNING", "OFFSHORE_UNCERTAIN", ref_type="offshore", ref_id=tid,
                          details={"message": f"Offshore transfer #{tid}: {why}. The bot will not re-send it; it completes "
                                              "automatically when the PnW record with its tag appears.", "tag": f"TUN-OFF{tid}"},
                          dedupe_key=f"offshore-uncertain:{tid}")

    async def _unknown(self, tid, why) -> dict:
        await asyncio.sleep(2)
        try:
            await self.lookup(tid)
        except (PnWRejected, PnWUncertain):
            pass
        row = self.get(tid)
        if row["status"] == "COMPLETED":
            return {"status": "COMPLETED", "message": "Transfer completed (confirmed from the PnW bank records).",
                    "record_id": row["pnw_record_id"]}
        await asyncio.to_thread(self._uncertain, tid, why)
        return {"status": "UNCERTAIN", "message": "I could not confirm whether PnW moved the funds. ECON was alerted; "
                                                  "nothing will be sent twice."}

    async def lookup(self, tid: int) -> str:
        """Look for the transfer's tag in the main bank's PnW records and book it if found."""
        row = self.get(tid)
        bank = self.s.offshore if (row and row["direction"] == "PAYOUT") else self.s.main
        recs = await self.pnw.fetch_bankrecs(bank)
        tag = f"TUN-OFF{tid}"
        mine = [r for r in recs if tag in (r.get("note") or "")]

        def apply():
            with self.db.tx() as conn:
                for r in mine:
                    REC.process_record(conn, r, REC.Ctx(self.s.alliance_id, members=None, bank_ids=self.s.bank_ids, offshore_id=self.s.offshore.alliance_id if self.s.offshore else None))
                return conn.execute("SELECT status FROM offshore_transfers WHERE id=?", (tid,)).fetchone()["status"]
        return await asyncio.to_thread(apply)

    # ------------------------------------------------------------- startup
    async def recover(self) -> list[str]:
        """After a restart: an AUTO transfer that was mid-flight is looked up, never re-sent."""
        def load():
            with self.db.read() as conn:
                return [r["id"] for r in conn.execute("SELECT id FROM offshore_transfers WHERE status='PENDING'")]
        notes = []
        for tid in await asyncio.to_thread(load):
            try:
                st = await self.lookup(tid)
            except (PnWRejected, PnWUncertain):
                st = "PENDING"
            if st != "COMPLETED":
                await asyncio.to_thread(self._uncertain, tid, "Bot restarted while this transfer was in flight")
            notes.append(f"offshore #{tid}: {st}")
        return notes
