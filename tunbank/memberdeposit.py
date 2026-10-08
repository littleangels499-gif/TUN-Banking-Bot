"""Member-initiated deposits: the member confirms in Discord, the bot starts the deposit FROM THEIR NATION using their
own API key (+ our verified bot key), then waits for the REAL PnW bank record before anything is credited.

Rules (the same accounting principle as everywhere):
  * No money, ever, from this module. A balance appears only when the scanner sees the PnW bank record.
  * Sent to PnW at most once; an unclear answer is resolved by looking for the TUN-DEP<id> tag, never by re-sending.
  * A key only ever acts for the nation it is stored for, and only for the member it belongs to.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from . import bankrec as B
from . import credentials as CR
from . import ledger as L
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from .util import jdump, now_iso

log = logging.getLogger("tunbank.memberdeposit")
DEP_TAG = re.compile(r"TUN-DEP(\d+)")

HELP_WHITELIST = ("Open your Politics & War **Account** page, switch **Whitelisted access** on for your API key, and try again. "
                  "(A deposit started by a bot is only allowed while that is on.)")


def dep_tag(note: str) -> int | None:
    m = DEP_TAG.search(note or "")
    return int(m.group(1)) if m else None


class MemberDepositService:
    def __init__(self, db, pnw, prices, settings, crypto: CR.Crypto, scanner):
        self.db, self.pnw, self.prices, self.s, self.crypto, self.scanner = db, pnw, prices, settings, crypto, scanner

    # ------------------------------------------------------------------ availability
    def available(self) -> bool:
        return self.crypto.enabled and bool(self.s.deposit_bot_key)

    def usable_for(self, conn, nation_id: int, discord_id) -> bool:
        from .config import cfg_bool

        if not (self.available() and cfg_bool(conn, "member_deposit_enabled")):
            return False
        row = CR.get_row(conn, nation_id)
        return bool(row and not row["disabled"] and str(row["discord_id"]) == str(discord_id))

    async def holdings(self, *, nation_id: int, discord_id) -> dict:
        """The member's own nation holdings, read with THEIR key (for 'Deposit Excess'). Raises CredentialError /
        PnWRejected / PnWUncertain with messages that never contain the key."""
        def load():
            with self.db.tx() as conn:
                key = CR.load_for_member(conn, self.crypto, nation_id=nation_id, discord_id=discord_id)
                if key:
                    conn.execute("UPDATE member_credentials SET last_used_at=? WHERE nation_id=?", (now_iso(), nation_id))
                return key
        key = await asyncio.to_thread(load)
        if not key:
            raise CR.CredentialError("You haven't set up direct deposits (or your key was removed). Use /nation setkey first.")
        try:
            return await self.pnw.fetch_nation_holdings(key)
        except (PnWRejected, PnWUncertain) as exc:
            raise type(exc)(CR.redact(exc)) from None

    # --------------------------------------------------------------------- start
    async def start(self, *, nation_id: int, discord_id, amounts: dict, idem: str, value_cents=None, snapshot_id=None) -> dict:
        """Returns {status, message, deposit_id, outcomes}. status: CREDITED, SENT, FAILED, UNCERTAIN, BLOCKED."""
        def create():
            with self.db.tx() as conn:
                L.assert_can_mutate(conn, nation_id, "lock")          # not during an emergency lock / open reconciliation
                member = L.get_member(conn, nation_id)
                if not member or str(member["discord_id"]) != str(discord_id):
                    raise L.LedgerError("Your Discord account is not linked to that nation.")
                if member["frozen"]:
                    raise L.FinancialBlocked("Your account is frozen by ECON. Please contact ECON staff.")
                old = conn.execute("SELECT id FROM member_deposits WHERE idempotency_key=?", (idem,)).fetchone()
                if old:
                    return old["id"], False
                now = now_iso()
                cur = conn.execute("INSERT INTO member_deposits(nation_id,discord_id,amounts_json,status,created_at,updated_at,idempotency_key,"
                                   "value_cents,price_snapshot_id) VALUES(?,?,?,'PENDING',?,?,?,?,?)",
                                   (nation_id, str(discord_id), jdump(amounts), now, now, idem, value_cents, snapshot_id))
                L.audit(conn, discord_id, "MEMBER_DEPOSIT_STARTED", f"member_deposit:{cur.lastrowid}", {"nation": nation_id, "amounts": amounts})
                return cur.lastrowid, True
        try:
            dep_id, created = await asyncio.to_thread(create)
        except L.LedgerError as exc:
            return {"status": "BLOCKED", "message": str(exc), "deposit_id": None, "outcomes": []}
        if not created:
            row = await asyncio.to_thread(self._row, dep_id)
            return {"status": row["status"], "message": "This deposit was already handled.", "deposit_id": dep_id, "outcomes": []}

        def gate():
            with self.db.tx() as conn:
                cur = conn.execute("UPDATE member_deposits SET attempted_at=?, updated_at=? WHERE id=? AND attempted_at IS NULL AND status='PENDING'",
                                   (now_iso(), now_iso(), dep_id))
                if cur.rowcount != 1:
                    return None
                try:
                    key = CR.load_for_member(conn, self.crypto, nation_id=nation_id, discord_id=discord_id)
                except CR.CredentialError as exc:
                    conn.execute("UPDATE member_deposits SET status='FAILED', failure_reason=?, updated_at=? WHERE id=?", (str(exc), now_iso(), dep_id))
                    return ("ERR", str(exc))
                if not key:
                    conn.execute("UPDATE member_deposits SET status='FAILED', failure_reason='no usable key', updated_at=? WHERE id=?", (now_iso(), dep_id))
                    return ("ERR", "You haven't set up direct deposits (or your key was removed). Use /nation setkey first.")
                conn.execute("UPDATE member_credentials SET last_used_at=? WHERE nation_id=?", (now_iso(), nation_id))
                return ("KEY", key)
        got = await asyncio.to_thread(gate)
        if got is None:
            return {"status": "UNCERTAIN", "message": "This deposit is already being processed.", "deposit_id": dep_id, "outcomes": []}
        if got[0] == "ERR":
            return {"status": "FAILED", "message": got[1], "deposit_id": dep_id, "outcomes": []}
        key = got[1]
        note = f"TUN-DEP{dep_id}"
        try:
            rec = await self.pnw.bank_deposit(amounts, note, key, self.s.deposit_bot_key)
        except PnWRejected as exc:
            msg = CR.redact(exc)
            await asyncio.to_thread(self._finish, dep_id, "FAILED", msg)
            extra = (" " + HELP_WHITELIST) if any(w in msg.lower() for w in ("whitelist", "bot", "key", "permission", "authoriz", "access")) else ""
            return {"status": "FAILED", "message": f"PnW refused the deposit, nothing was sent: {msg}.{extra}", "deposit_id": dep_id, "outcomes": []}
        except PnWUncertain as exc:
            return await self._unclear(dep_id, nation_id, CR.redact(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("member deposit %s unexpected error", dep_id)
            return await self._unclear(dep_id, nation_id, f"Unexpected error: {type(exc).__name__}")
        finally:
            key = None

        # PnW accepted it. Check the record really is OUR member's deposit into OUR bank before trusting the link.
        try:
            n = B.normalize(rec)
        except Exception as exc:  # noqa: BLE001
            return await self._unclear(dep_id, nation_id, f"unreadable record: {type(exc).__name__}")
        if n["sender_type"] != 1 or n["sender_id"] != nation_id:
            await asyncio.to_thread(self._wrong_owner, dep_id, nation_id, n["sender_id"])
            res = await self.scanner.scan()          # the deposit is real: it is credited to whoever really sent it
            return {"status": "FAILED", "message": "That API key does not belong to your nation, so it was switched off. ECON has been told.",
                    "deposit_id": dep_id, "outcomes": res.outcomes if res.ok else []}

        def sent():
            with self.db.tx() as conn:
                conn.execute("UPDATE member_deposits SET status='SENT', pnw_record_id=?, updated_at=? WHERE id=? AND status='PENDING'",
                             (n["id"], now_iso(), dep_id))
        await asyncio.to_thread(sent)
        res = await self.scanner.scan()              # credit ONLY through the normal path, from PnW's own bank records
        row = await asyncio.to_thread(self._row, dep_id)
        outcomes = res.outcomes if res.ok else []
        if row["status"] == "CREDITED":
            return {"status": "CREDITED", "message": "Your deposit arrived and your balance is credited.", "deposit_id": dep_id,
                    "record_id": row["pnw_record_id"], "outcomes": outcomes}
        return {"status": "SENT", "message": "PnW accepted your deposit. It will be credited as soon as its bank record shows up "
                                              "(this happens automatically).", "deposit_id": dep_id, "record_id": n["id"], "outcomes": outcomes}

    # ------------------------------------------------------------------- helpers
    def _row(self, dep_id):
        with self.db.read() as conn:
            return dict(conn.execute("SELECT * FROM member_deposits WHERE id=?", (dep_id,)).fetchone())

    def _finish(self, dep_id, status, why):
        with self.db.tx() as conn:
            conn.execute("UPDATE member_deposits SET status=?, failure_reason=?, updated_at=? WHERE id=? AND status IN ('PENDING','SENT')",
                         (status, why[:300], now_iso(), dep_id))
            L.audit(conn, "system", "MEMBER_DEPOSIT_" + status, f"member_deposit:{dep_id}", {"why": why[:200]})

    def _wrong_owner(self, dep_id, nation_id, real_sender):
        with self.db.tx() as conn:
            conn.execute("UPDATE member_deposits SET status='FAILED', failure_reason=?, updated_at=? WHERE id=?",
                         (f"the key belongs to nation {real_sender}, not {nation_id}", now_iso(), dep_id))
            CR.disable(conn, nation_id, "system", f"the key acted as nation {real_sender}, not the linked nation")
            L.raise_event(conn, "WARNING", "MEMBER_KEY_WRONG_NATION", nation_id=nation_id, ref_type="member_deposit", ref_id=dep_id,
                          details={"message": f"The API key saved for nation {nation_id} belongs to nation {real_sender}. The key was disabled. "
                                              "The deposit itself is real and is credited to the nation that actually sent it."},
                          dedupe_key=f"wrong-key:{nation_id}")

    async def _unclear(self, dep_id, nation_id, why) -> dict:
        """We can't tell whether PnW executed it. Look for the tag in the bank records; never re-send."""
        await asyncio.sleep(1)
        outcomes = []
        try:
            res = await self.scanner.scan()
            outcomes = res.outcomes if res.ok else []
        except Exception:  # noqa: BLE001
            pass
        row = await asyncio.to_thread(self._row, dep_id)
        if row["status"] == "CREDITED":
            return {"status": "CREDITED", "message": "Your deposit arrived and your balance is credited.", "deposit_id": dep_id,
                    "record_id": row["pnw_record_id"], "outcomes": outcomes}

        def mark():
            with self.db.tx() as conn:
                conn.execute("UPDATE member_deposits SET status='UNCERTAIN', failure_reason=?, updated_at=? WHERE id=? AND status IN ('PENDING','SENT')",
                             (why[:300], now_iso(), dep_id))
                L.raise_event(conn, "WARNING", "MEMBER_DEPOSIT_UNCERTAIN", nation_id=nation_id, ref_type="member_deposit", ref_id=dep_id,
                              details={"message": f"Member deposit #{dep_id}: {why}. It is NOT re-sent. If PnW did move the money, "
                                                  "the real bank record credits the member automatically.", "tag": f"TUN-DEP{dep_id}"},
                              dedupe_key=f"member-deposit-uncertain:{dep_id}")
        await asyncio.to_thread(mark)
        return {"status": "UNCERTAIN", "message": "I couldn't confirm whether PnW took the deposit. Nothing was credited and nothing will be sent twice. "
                                                  "If your nation's money did move, it will be credited automatically when the bank record appears.",
                "deposit_id": dep_id, "outcomes": outcomes}
