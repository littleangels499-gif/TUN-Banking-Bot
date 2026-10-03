"""Tests for the accounting core. Run with:  python -m unittest discover -s tests -v"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tunbank import importer  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank import reconcile as R  # noqa: E402
from tunbank import records as REC  # noqa: E402
from tunbank.config import Settings  # noqa: E402
from tunbank.db import Database  # noqa: E402
from tunbank.pnw import PnWRejected, PnWUncertain  # noqa: E402
from tunbank.scanner import Scanner  # noqa: E402
from tunbank.valuation import PriceService, Snapshot, value_amounts  # noqa: E402
from tunbank.withdrawals import WithdrawalService  # noqa: E402

ALLIANCE = 900
PRICES = {r: Decimal("100") for r in M.NON_CASH}  # 100 cents (=$1.00) per unit


def rec(id, sender, amounts, note="", receiver=ALLIANCE, sender_type=1, receiver_type=2, tax_id=0, **kw):
    r = {"id": id, "date": "2026-09-01", "sender_id": sender, "sender_type": sender_type,
         "receiver_id": receiver, "receiver_type": receiver_type, "banker_id": 0, "note": note,
         "tax_id": tax_id}
    for res in M.RESOURCES:
        r[res] = 0
    r.update(amounts)
    r.update(kw)
    return r


class FakePnW:
    def __init__(self):
        self.recs = []
        self.holdings = {}
        self.members = {1: "Alpha", 2: "Beta", 3: "Gamma"}
        self.withdraw_mode = "ok"      # ok | reject | timeout_sent | timeout_lost | slow
        self.withdraw_calls = 0
        self.next_id = 5000
        self.bank_readable = True

    async def fetch_bankrecs(self):
        if not self.bank_readable:
            raise PnWUncertain("down")
        return list(self.recs)

    async def fetch_bank_holdings(self):
        if not self.bank_readable:
            raise PnWUncertain("down")
        return dict(self.holdings)

    async def fetch_alliance_members(self):
        return dict(self.members)

    async def fetch_prices(self):
        return dict(PRICES)

    async def bank_withdraw(self, receiver, amounts, note):
        self.withdraw_calls += 1
        if self.withdraw_mode == "reject":
            raise PnWRejected("insufficient funds")
        self.next_id += 1
        r = rec(self.next_id, ALLIANCE, {k: M.units_to_float(v) for k, v in amounts.items()}, note,
                receiver=receiver, sender_type=2, receiver_type=1)
        if self.withdraw_mode == "timeout_sent":
            self.recs.append(r)
            raise PnWUncertain("timeout")
        if self.withdraw_mode == "timeout_lost":
            raise PnWUncertain("timeout")
        self.recs.append(r)
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "tunbank.db")
        self.db.migrate()
        self.pnw = FakePnW()
        self.settings = Settings("t", "k", "b", "k", ALLIANCE, Path(self.tmp.name), {1})
        self.prices = PriceService(self.db, self.pnw)
        self.scanner = Scanner(self.db, self.pnw, self.prices, self.settings)
        self.wd = WithdrawalService(self.db, self.pnw, self.prices, self.settings)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def scan(self):
        return self.run_async(self.scanner.scan())

    def bal(self, nation, bucket="AVAILABLE"):
        with self.db.read() as c:
            return L.get_balances(c, nation, bucket)

    def open_balance(self, nation, amounts):
        """Give a member an opening balance through the real importer."""
        snap = Snapshot(None, "2026-09-01T00:00:00Z", PRICES)
        csv_text = "nation_id," + ",".join(amounts) + "\n" + f"{nation}," + ",".join(
            str(v) for v in amounts.values())
        p = importer.preview("x.csv", csv_text.encode(), members=self.pnw.members, snapshot=snap)
        self.assertFalse(p.blocked, p.errors)
        with self.db.tx() as c:
            importer.commit(c, p, admin_id="1", note="t", snapshot_id=None)

    def first_scan(self):
        self.scan()  # baseline


class TestMoney(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(M.parse_one("1,000.50"), 100050)
        self.assertEqual(M.parse_one("2.5m"), 250000000)
        with self.assertRaises(M.AmountError):
            M.parse_one("-5")
        with self.assertRaises(M.AmountError):
            M.parse_one("1.001")
        with self.assertRaises(M.AmountError):
            M.parse_one("abc")
        with self.assertRaises(M.AmountError):
            M.parse_one("1e30")

    def test_parse_amounts(self):
        self.assertEqual(M.parse_amounts("money=1m aluminium=1000 coal=1000"),
                         {"money": 100000000, "aluminum": 100000, "coal": 100000})
        with self.assertRaises(M.AmountError):
            M.parse_amounts("money=1m junk")

    def test_float_safety(self):
        self.assertEqual(M.to_units(0.1 + 0.2), 30)


class TestDeposits(Base):
    def test_acceptance_deposit_credits_and_is_idempotent(self):
        self.first_scan()
        self.pnw.recs.append(rec(10, 1, {"money": 1000000, "aluminum": 1000, "coal": 1000}))
        r = self.scan()
        self.assertTrue(r.ok)
        self.assertEqual([o.kind for o in r.outcomes if o.record_id == 10], ["CREDIT"])
        self.assertEqual(self.bal(1), {"money": 100000000, "aluminum": 100000, "coal": 100000})
        # scanned twice -> no second credit
        r2 = self.scan()
        self.assertEqual([o.kind for o in r2.outcomes if o.record_id == 10], ["DUPLICATE"])
        self.assertEqual(self.bal(1)["money"], 100000000)

    def test_ignore_is_alliance_money(self):
        self.first_scan()
        self.pnw.recs.append(rec(11, 1, {"money": 5000}, "#ignore"))
        r = self.scan()
        self.assertEqual(r.outcomes[0].kind, "DONATION")
        self.assertEqual(self.bal(1), {})

    def test_loan_repayment_not_credited(self):
        self.first_scan()
        self.pnw.recs.append(rec(12, 1, {"money": 5000}, "#loan repayment"))
        r = self.scan()
        self.assertEqual(r.outcomes[0].kind, "LOAN")
        self.assertEqual(self.bal(1), {})

    def test_tax_hidden_and_recorded(self):
        self.first_scan()
        self.pnw.recs.append(rec(13, 2, {"money": 777}, "Automatic tax", tax_id=4))
        r = self.scan()
        self.assertEqual(r.outcomes[0].kind, "TAX")
        self.assertEqual(self.bal(2), {})
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tax_records").fetchone()[0], 1)

    def test_ambiguous_goes_to_review_and_non_member_too(self):
        self.first_scan()
        self.pnw.recs.append(rec(14, 1, {"money": 5}, "#grant"))
        self.pnw.recs.append(rec(15, 77, {"money": 5}))          # not an alliance member
        self.pnw.recs.append(rec(16, 5, {"money": 5}, sender_type=2))  # sender is an alliance
        r = self.scan()
        self.assertEqual([o.kind for o in r.outcomes], ["REVIEW", "REVIEW", "REVIEW"])
        self.assertEqual(self.bal(1), {})

    def test_baseline_history_is_not_credited(self):
        self.pnw.recs.append(rec(1, 1, {"money": 999999}))
        r = self.scan()
        self.assertTrue(r.baseline)
        self.assertEqual(self.bal(1), {})
        with self.db.tx() as c:  # staff can credit it explicitly, still tied to the real record
            REC.resolve_review(c, record_id=1, action="credit", actor="9", note="missed", nation_id=1)
        self.assertEqual(self.bal(1)["money"], 99999900)

    def test_emergency_lock_defers_credit(self):
        self.first_scan()
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "test", "1")
        self.pnw.recs.append(rec(20, 1, {"money": 100}))
        self.scan()
        self.assertEqual(self.bal(1), {})
        with self.db.tx() as c:
            L.set_emergency_lock(c, False, "", "1")
        self.scan()
        self.assertEqual(self.bal(1)["money"], 10000)

    def test_changed_record_raises_event(self):
        self.first_scan()
        self.pnw.recs.append(rec(30, 1, {"money": 100}))
        self.scan()
        self.pnw.recs[-1]["money"] = 999999
        r = self.scan()
        self.assertEqual(r.outcomes[-1].kind, "ANOMALY")
        self.assertEqual(self.bal(1)["money"], 10000)


class TestDatabaseGuards(Base):
    """The database itself must refuse to invent money, even if the bot's code is wrong."""

    def test_cannot_insert_deposit_without_pnw_record(self):
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                L.post_entries(c, [dict(group_id="g", nation_id=1, bucket="AVAILABLE", resource="money",
                                        delta=10**9, entry_type="DEPOSIT", pnw_record_id=123, actor="x")])
        self.assertEqual(self.bal(1), {})

    def test_cannot_edit_or_delete_ledger(self):
        self.first_scan()
        self.pnw.recs.append(rec(40, 1, {"money": 100}))
        self.scan()
        for sql in ("UPDATE ledger_entries SET delta=99999999", "DELETE FROM ledger_entries",
                    "UPDATE pnw_records SET amounts_json='{}'", "DELETE FROM pnw_records"):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.db.tx() as c:
                    c.execute(sql)

    def test_cannot_inject_fake_opening(self):
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                L.post_entries(c, [dict(group_id="g", nation_id=1, bucket="AVAILABLE", resource="money",
                                        delta=100, entry_type="OPENING", batch_id=1, actor="x")])

    def test_direct_balance_edit_is_detected_not_hidden(self):
        self.first_scan()
        self.pnw.recs.append(rec(41, 1, {"money": 100}))
        self.scan()
        with self.db.tx() as c:   # someone tampers with the cached balance directly
            c.execute("UPDATE balances SET amount = amount + 5000000000000 WHERE nation_id=1")
        self.assertEqual(self.bal(1)["money"], 5000000010000)
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**15}, snapshot_id=None, triggered_by="t")
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("BALANCE_MISMATCH", kinds)
        with self.db.read() as c:
            self.assertTrue(L.integrity_state(c)["emergency_lock"])   # auto emergency lock
            self.assertEqual(self.bal(1)["money"], 5000000010000)     # NOT silently corrected


class TestLocksAndWithdrawals(Base):
    def setUp(self):
        super().setUp()
        self.first_scan()
        self.pnw.recs.append(rec(50, 1, {"money": 1000000, "coal": 500}))
        self.pnw.holdings = {"money": 100000000, "coal": 50000}
        self.scan()

    def withdraw(self, amounts, key="k1", source="MEMBER_AVAILABLE", lock_id=None, dest=1):
        return self.run_async(self.wd.request(
            tx_type="WITHDRAW_SELF", funding_source=source, member_nation_id=1, lock_id=lock_id,
            dest_nation_id=dest, amounts=amounts, actor="111", note="test", reason="test",
            idempotency_key=key))

    def test_reserve_and_release_no_pnw(self):
        with self.db.tx() as c:
            res = L.create_lock(c, nation_id=1, amounts={"money": 50000000}, lock_type="WARCHEST",
                                reason="warchest", actor="9")
        self.assertEqual(self.bal(1)["money"], 50000000)
        self.assertEqual(self.bal(1, "LOCKED")["money"], 50000000)
        self.assertEqual(self.pnw.withdraw_calls, 0)
        with self.db.tx() as c:
            L.release_lock(c, lock_id=res["lock_id"], amounts=None, reason="done", actor="9")
        self.assertEqual(self.bal(1)["money"], 100000000)
        self.assertEqual(self.bal(1, "LOCKED"), {})

    def test_cannot_withdraw_locked_funds_as_member(self):
        with self.db.tx() as c:
            L.create_lock(c, nation_id=1, amounts={"money": 100000000}, lock_type="X", reason="r", actor="9")
        r = self.withdraw({"money": 1})
        self.assertEqual(r.status, "BLOCKED")
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_happy_withdrawal(self):
        r = self.withdraw({"money": 40000000})
        self.assertEqual(r.status, "COMPLETED", r.message)
        self.assertEqual(self.bal(1)["money"], 60000000)
        self.assertIsNotNone(r.pnw_record_id)

    def test_pnw_rejects_nothing_deducted(self):
        self.pnw.withdraw_mode = "reject"
        r = self.withdraw({"money": 40000000})
        self.assertEqual(r.status, "FAILED")
        self.assertEqual(self.bal(1)["money"], 100000000)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT status FROM transactions").fetchone()[0], "FAILED")
            self.assertEqual(L.spendable(c, 1)["money"], 100000000)

    def test_timeout_but_pnw_sent_is_confirmed_from_records(self):
        self.pnw.withdraw_mode = "timeout_sent"
        r = self.withdraw({"money": 40000000})
        self.assertEqual(r.status, "COMPLETED")
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertEqual(self.bal(1)["money"], 60000000)

    def test_timeout_and_lost_stays_on_hold_no_double_spend(self):
        self.pnw.withdraw_mode = "timeout_lost"
        r = self.withdraw({"money": 40000000})
        self.assertEqual(r.status, "UNCERTAIN")
        self.assertEqual(self.bal(1)["money"], 100000000)        # not deducted...
        with self.db.read() as c:
            self.assertEqual(L.spendable(c, 1)["money"], 60000000)  # ...but held, so can't be re-spent
        r2 = self.withdraw({"money": 70000000}, key="k2")
        self.assertEqual(r2.status, "BLOCKED")

    def test_retry_with_same_key_never_duplicates(self):
        r1 = self.withdraw({"money": 10000000}, key="same")
        r2 = self.withdraw({"money": 10000000}, key="same")
        self.assertEqual(r1.status, "COMPLETED")
        self.assertTrue(r2.replay)
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertEqual(self.bal(1)["money"], 90000000)

    def test_overdraw_blocked(self):
        r = self.withdraw({"money": 999999999999})
        self.assertEqual(r.status, "BLOCKED")
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_frozen_member_cannot_self_withdraw(self):
        with self.db.tx() as c:
            c.execute("UPDATE members SET frozen=1 WHERE nation_id=1")
        self.assertEqual(self.withdraw({"money": 1}).status, "BLOCKED")

    def test_emergency_lock_blocks_withdrawals(self):
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "test", "1")
        self.assertEqual(self.withdraw({"money": 1}).status, "BLOCKED")
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_bank_unreadable_blocks_alliance_withdraw(self):
        self.pnw.bank_readable = False
        r = self.run_async(self.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None,
            dest_nation_id=2, amounts={"money": 100}, actor="9", note="n", reason="r",
            idempotency_key="a1"))
        self.assertEqual(r.status, "BLOCKED")
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_alliance_funds_exclude_member_money(self):
        # bank holds 1,000,000; member owns all of it -> alliance-owned is 0
        self.pnw.holdings = {"money": 100000000, "coal": 50000}
        r = self.run_async(self.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None,
            dest_nation_id=2, amounts={"money": 100}, actor="9", note="n", reason="r",
            idempotency_key="a2"))
        self.assertEqual(r.status, "BLOCKED")
        self.pnw.holdings = {"money": 100000000 + 500000, "coal": 50000}
        r = self.run_async(self.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None,
            dest_nation_id=2, amounts={"money": 500000}, actor="9", note="n", reason="r",
            idempotency_key="a3"))
        self.assertEqual(r.status, "COMPLETED", r.message)
        self.assertEqual(self.bal(1)["money"], 100000000)   # member untouched

    def test_locked_withdraw_from_correct_lock_only(self):
        with self.db.tx() as c:
            lk = L.create_lock(c, nation_id=1, amounts={"money": 30000000}, lock_type="X", reason="r", actor="9")
        r = self.run_async(self.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="MEMBER_LOCKED", member_nation_id=1,
            lock_id=lk["lock_id"], dest_nation_id=1, amounts={"money": 10000000}, actor="9",
            note="n", reason="r", idempotency_key="lk1"))
        self.assertEqual(r.status, "COMPLETED", r.message)
        self.assertEqual(self.bal(1, "LOCKED")["money"], 20000000)
        self.assertEqual(self.bal(1)["money"], 70000000)

    def test_recovery_after_crash_before_send(self):
        with self.db.tx() as c:
            tx_id, _ = L.begin_withdrawal(
                c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1,
                lock_id=None, dest_nation_id=1, amounts={"money": 100}, actor="1", note="n", reason="r",
                idempotency_key="crash")
        notes = self.run_async(self.wd.recover())
        with self.db.read() as c:
            self.assertEqual(L.get_tx(c, tx_id)[0]["status"], "FAILED")
        self.assertEqual(self.pnw.withdraw_calls, 0)


class TestAdjustAndReconcile(Base):
    def test_adjust_requires_evidence_and_approval(self):
        with self.db.tx() as c:
            with self.assertRaises(L.LedgerError):
                L.apply_adjustment(c, nation_id=1, deltas={"money": 100}, reason="r", evidence="",
                                   actor="9", approval_id=None)
        with self.db.tx() as c:
            with self.assertRaises(L.LedgerError):
                L.apply_adjustment(c, nation_id=1, deltas={"money": 100}, reason="r", evidence="e",
                                   actor="9", approval_id=None)
        with self.db.tx() as c:
            ap = L.create_approval(c, "ADJUSTMENT", {}, "9", "r")
            with self.assertRaises(L.LedgerError):
                L.approve(c, ap, "9")   # same person cannot approve
            L.approve(c, ap, "8")
            L.apply_adjustment(c, nation_id=1, deltas={"money": 100}, reason="r", evidence="ticket 5",
                               actor="9", approval_id=ap)
        self.assertEqual(self.bal(1)["money"], 100)

    def test_ledger_larger_than_bank_locks(self):
        self.first_scan()
        self.pnw.recs.append(rec(60, 1, {"money": 1000000}))
        self.scan()
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 5}, snapshot_id=None, triggered_by="t")
        self.assertIn("LEDGER_EXCEEDS_BANK", {f["kind"] for f in res["findings"]})
        with self.db.read() as c:
            self.assertEqual(L.integrity_state(c)["state"], "EMERGENCY_LOCK")

    def test_clean_books_reconcile_ok(self):
        self.first_scan()
        self.pnw.recs.append(rec(61, 1, {"money": 1000000}))
        self.scan()
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 100000000}, snapshot_id=None, triggered_by="t")
        bad = [f for f in res["findings"] if f["severity"] in ("RECON", "CRITICAL")]
        self.assertEqual(bad, [], bad)

    def test_deleted_history_detected(self):
        self.first_scan()
        self.pnw.recs.append(rec(62, 1, {"money": 100}))
        self.scan()
        with self.db.tx() as c:
            R.run_checks(c, holdings={"money": 10**9}, snapshot_id=None, triggered_by="t")
        raw = sqlite3.connect(str(self.db.path))     # attacker bypasses the app AND drops the trigger
        raw.execute("DROP TRIGGER trg_ledger_no_delete")
        raw.execute("DELETE FROM ledger_entries")
        raw.commit()
        raw.close()
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**9}, snapshot_id=None, triggered_by="t")
        self.assertIn("LEDGER_HISTORY_LOST", {f["kind"] for f in res["findings"]})


class TestImport(Base):
    def test_validation_rejects_bad_rows(self):
        snap = Snapshot(None, "2026-09-01T00:00:00Z", PRICES)
        bad = ("nation_id,money,coal\n"
               "1,100,5\n1,100,5\n"          # exact duplicate row
               "2,-5,0\n"                     # negative
               "3,abc,0\n"                    # malformed
               "99,10,0\n"                    # unknown nation
               "x,1,1\n")                     # bad id
        p = importer.preview("m.csv", bad.encode(), members=self.pnw.members, snapshot=snap)
        self.assertTrue(p.blocked)
        text = " ".join(p.errors)
        for word in ("duplicate", "negative", "not a valid number", "not in the alliance", "not a valid nation id"):
            self.assertIn(word, text)
        with self.db.tx() as c:
            with self.assertRaises(L.LedgerError):
                importer.commit(c, p, admin_id="1", note="x", snapshot_id=None)
        self.assertEqual(self.bal(1), {})

    def test_good_import_then_no_overwrite(self):
        self.open_balance(1, {"money": 500, "coal": 7})
        self.assertEqual(self.bal(1), {"money": 50000, "coal": 700})
        snap = Snapshot(None, "2026-09-01T00:00:00Z", PRICES)
        p = importer.preview("y.csv", b"nation_id,money\n1,999\n", members=self.pnw.members, snapshot=snap)
        with self.db.tx() as c:
            with self.assertRaises(L.LedgerError):
                importer.commit(c, p, admin_id="1", note="x", snapshot_id=None)
        with self.db.tx() as c:
            R.run_checks(c, holdings={"money": 10**9, "coal": 10**9}, snapshot_id=None, triggered_by="t")
        with self.db.read() as c:
            self.assertIn(L.integrity_state(c)["state"], ("NORMAL", "WARNING"))  # WARNING = no scan yet

    def test_opening_balance_has_no_pnw_id(self):
        self.open_balance(2, {"money": 5})
        with self.db.read() as c:
            row = c.execute("SELECT pnw_record_id FROM ledger_entries WHERE entry_type='OPENING'").fetchone()
        self.assertIsNone(row[0])


class TestMigrations(unittest.TestCase):
    def test_upgrade_preserves_data_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "tunbank.db")
            db.migrate()
            with db.tx() as c:
                L.set_state(c, "marker", "keep-me")
            # simulate a future migration
            mig = Path(__file__).resolve().parent.parent / "tunbank" / "migrations" / "099_test.sql"
            mig.write_text("CREATE TABLE future_thing (id INTEGER PRIMARY KEY);")
            try:
                done = db.migrate(backup_dir=Path(d) / "backups")
                self.assertEqual(done, ["099_test.sql"])
                self.assertEqual(len(list((Path(d) / "backups").glob("pre-migration-*.db"))), 1)
                with db.read() as c:
                    self.assertEqual(L.get_state(c, "marker"), "keep-me")
                self.assertEqual(db.migrate(), [])
            finally:
                mig.unlink()
                db.close()

    def test_edited_old_migration_refused(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "tunbank.db")
            db.migrate()
            db.conn.execute("UPDATE schema_migrations SET checksum='bad'")
            from tunbank.db import DatabaseError
            with self.assertRaises(DatabaseError):
                db.migrate()
            db.close()

    def test_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "tunbank.db")
            db.migrate()
            out = db.backup_to(Path(d) / "b" / "x.db")
            chk = sqlite3.connect(str(out))
            self.assertEqual(chk.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            chk.close()
            db.close()


class TestValuation(unittest.TestCase):
    def test_value_and_flags(self):
        snap = Snapshot(3, "2026-09-01T00:00:00Z", {"coal": Decimal("400"), "aluminum": Decimal("500")})
        v = value_amounts({"money": 100000000, "coal": 100000, "aluminum": 100000}, snap)
        self.assertEqual(v.total_cents, 100000000 + 40000000 + 50000000)
        self.assertTrue(v.complete)
        v2 = value_amounts({"money": 100, "oil": 100}, snap)
        self.assertFalse(v2.complete)
        self.assertEqual(v2.missing, ["oil"])
        self.assertEqual(value_amounts({"coal": 5}, None).total_cents, None)


if __name__ == "__main__":
    unittest.main()
