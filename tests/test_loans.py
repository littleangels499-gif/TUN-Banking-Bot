"""Loans: separate from deposits; #loan repayments go interest -> principal -> excess to deposit."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
import test_reset_restore as TR  # noqa: E402
from test_core import rec  # noqa: E402

from tunbank import cmds_loan  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import loans as LN  # noqa: E402
from tunbank import reconcile as R  # noqa: E402
from tunbank.ui import post_outcomes  # noqa: E402

discord = TC.discord


class LoanBase(TR.ResetBase):
    def setUp(self):
        super().setUp()
        from discord import app_commands
        self.loan = app_commands.Group("loan")
        cmds_loan.register(self.loan, self.svc)
        self.fund_alice({"money": 5000.0})                    # Alpha has $5,000.00 of cash deposit
        self.minister = TC.discord.User(402, "min")
        self.minister.roles = [discord.Role(77)]
        self.banker = TC.discord.User(403, "bnk")
        self.banker.roles = [discord.Role(88)]
        with self.db.tx() as c:
            for lvl, role in (("MINISTER", "77"), ("BANKER", "88")):
                c.execute("INSERT INTO role_permissions(level,role_id) VALUES(?,?)", (lvl, role))

    def make(self, principal=1000000, pct=5, days=30, nid=1):
        with self.db.tx() as c:
            return LN.record_loan(c, nation_id=nid, principal_cents=principal, interest_percent=pct, due_days=days,
                                  note="test loan", actor="9")["loan_id"]

    def loan_row(self, lid):
        with self.db.read() as c:
            return dict(c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone())

    def repay_record(self, rid, dollars, nid=1, note="#loan repayment"):
        """A real-looking PnW #loan record (classified by the real scanner)."""
        self.pnw.recs.append(rec(rid, nid, {"money": dollars}, note))

    def scan(self):
        r = self.run_async(self.svc.scanner.scan())
        self.run_async(post_outcomes(self.svc, r.outcomes))
        return r


class TestLoanService(LoanBase):
    def test_recording_a_loan_never_touches_the_deposit(self):
        before = self.bal(1)
        lid = self.make()
        l = self.loan_row(lid)
        self.assertEqual((l["principal_cents"], l["interest_cents"], l["status"]), (1000000, 50000, "ACTIVE"))
        self.assertEqual(LN.owed(l), 1050000)
        self.assertEqual(self.bal(1), before)
        self.assertTrue(l["due_at"])

    def test_bad_loans_are_refused(self):
        for kw in (dict(principal=0), dict(pct=-1), dict(pct=5000)):
            with self.assertRaises(LN.LoanError):
                self.make(**kw)

    def test_interest_rounds_half_up(self):
        lid = self.make(principal=333, pct=1.5)                  # 4.995 cents -> 5
        self.assertEqual(self.loan_row(lid)["interest_cents"], 5)

    def test_repayment_goes_interest_first_then_principal(self):
        lid = self.make()
        self.repay_record(9001, 300.00)                           # $300 < $500 interest
        self.scan()
        self.scan()
        self.repay_record(9002, 1000.00)                          # pays the other $200 interest + $800 principal
        self.scan()
        l = self.loan_row(lid)
        self.assertEqual((l["interest_paid_cents"], l["principal_paid_cents"]), (50000, 80000))
        self.assertEqual(LN.owed(l), 1050000 - 30000 - 100000)
        self.assertEqual(self.bal(1)["money"], 500000)            # the deposit never moved: a repayment is not a deposit

    def test_oldest_loan_is_paid_first(self):
        a, b = self.make(principal=100000, pct=0), self.make(principal=100000, pct=0)
        self.scan()
        self.repay_record(9101, 1500.00)
        self.scan()
        self.assertEqual(self.loan_row(a)["status"], "PAID")
        self.assertEqual(self.loan_row(b)["principal_paid_cents"], 50000)

    def test_excess_above_what_is_owed_becomes_deposit(self):
        lid = self.make(principal=100000, pct=0)                  # owes $1,000
        self.scan()
        self.repay_record(9201, 1500.00)
        r = self.scan()
        self.assertEqual(self.loan_row(lid)["status"], "PAID")
        self.assertEqual(self.bal(1)["money"], 500000 + 50000)    # +$500 excess
        self.assertTrue(any("above what was owed" in (o.note or "") for o in r.outcomes))
        with self.db.read() as c:
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**13}, snapshot_id=None, triggered_by="t")
        self.assertFalse([f for f in res["findings"] if f["severity"] in ("CRITICAL", "RECON")], res["findings"])

    def test_a_repayment_is_applied_only_once(self):
        self.make(principal=100000, pct=0)
        self.scan()
        self.repay_record(9301, 400.00)
        self.scan()
        self.scan()
        with self.db.tx() as c:
            with self.assertRaises(LN.LoanError):
                LN.apply_repayment(c, record_id=9301, actor="9")
        self.assertEqual(self.count("loan_events", "kind='REPAYMENT'"), 1)

    def test_nothing_is_credited_when_there_is_no_loan(self):
        self.scan()
        self.repay_record(9401, 400.00)
        r = self.scan()
        self.assertEqual(self.bal(1)["money"], 500000)
        self.assertTrue(any("No active loan" in (o.note or "") for o in r.outcomes))
        self.assertEqual(self.count("loan_events"), 0)

    def test_old_records_in_the_first_baseline_scan_are_never_applied(self):
        self.make(principal=100000, pct=0)
        self.repay_record(9451, 400.00)
        with self.db.tx() as c:
            c.execute("DELETE FROM system_state WHERE key='scan_baseline_done'")
        self.scan()
        self.assertEqual(self.count("loan_events", "kind='REPAYMENT'"), 0)

    def test_only_money_is_applied_other_resources_are_not(self):
        lid = self.make(principal=100000, pct=0)
        self.scan()
        self.pnw.recs.append(rec(9501, 1, {"money": 200.0, "coal": 50.0}, "#loan repayment"))
        r = self.scan()
        self.assertEqual(self.loan_row(lid)["principal_paid_cents"], 20000)
        self.assertTrue(any("Other resources" in (o.note or "") for o in r.outcomes))
        self.assertNotIn("coal", self.bal(1))

    def test_emergency_lock_defers_it_and_the_scan_survives(self):
        lid = self.make(principal=100000, pct=0)
        self.scan()
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "test", "9")
        self.repay_record(9601, 400.00)
        r = self.scan()
        self.assertTrue(r.ok)
        self.assertEqual(self.loan_row(lid)["principal_paid_cents"], 0)
        self.assertTrue(any("/loan applypayment" in (o.note or "") for o in r.outcomes))
        with self.db.tx() as c:
            L.set_emergency_lock(c, False, "test", "9")
            LN.apply_repayment(c, record_id=9601, actor="9")
        self.assertEqual(self.loan_row(lid)["principal_paid_cents"], 40000)

    def test_deduction_pays_the_loan_from_available_cash(self):
        lid = self.make(principal=100000, pct=10)                 # owes $1,100
        with self.db.tx() as c:
            res = LN.deduct(c, nation_id=1, amount_cents=60000, actor="9", note="war debt")
        l = self.loan_row(lid)
        self.assertEqual((l["interest_paid_cents"], l["principal_paid_cents"]), (10000, 50000))
        self.assertEqual(self.bal(1)["money"], 500000 - 60000)
        self.assertEqual(res["still_owed"], 50000)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger_entries WHERE entry_type='LOAN_DEDUCTION'").fetchone()[0], 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])

    def test_deduction_limits(self):
        self.make(principal=100000, pct=0)
        for cents, exc in ((0, LN.LoanError), (100001, LN.LoanError)):
            with self.assertRaises(exc):
                with self.db.tx() as c:
                    LN.deduct(c, nation_id=1, amount_cents=cents, actor="9", note="x")
        with self.db.tx() as c:                                    # lock nearly everything: locked cash can't be used
            L.create_lock(c, nation_id=1, amounts={"money": 480000}, lock_type="X", reason="r", actor="9")
        with self.assertRaises(L.InsufficientFunds):
            with self.db.tx() as c:
                LN.deduct(c, nation_id=1, amount_cents=50000, actor="9", note="x")
        with self.assertRaises(LN.LoanError):                      # no loan at all
            with self.db.tx() as c:
                LN.deduct(c, nation_id=999, amount_cents=1, actor="9", note="x")

    def test_a_loan_deduction_cannot_be_forged(self):
        self.make()
        for entry in (dict(delta=-100, loan_event_id=None), dict(delta=-100, loan_event_id=1)):
            with self.assertRaises(sqlite3.Error):
                with self.db.tx() as c:
                    L.post_entries(c, [dict(group_id="g", nation_id=1, bucket="AVAILABLE", resource="money",
                                            entry_type="LOAN_DEDUCTION", actor="x", **entry)])

    def test_write_off_forgives_the_rest_and_keeps_history(self):
        lid = self.make(principal=100000, pct=0)
        with self.assertRaises(LN.LoanError):
            with self.db.tx() as c:
                LN.write_off(c, loan_id=lid, reason="", actor="9")
        with self.db.tx() as c:
            res = LN.write_off(c, loan_id=lid, reason="alliance decision, ticket 12", actor="9")
        l = self.loan_row(lid)
        self.assertEqual((res["written_off_cents"], l["status"], l["written_off_cents"]), (100000, "WRITTEN_OFF", 100000))
        with self.assertRaises(LN.LoanError):
            with self.db.tx() as c:
                LN.write_off(c, loan_id=lid, reason="again please", actor="9")
        self.assertEqual(self.count("loan_events"), 2)             # ISSUED + WRITE_OFF, nothing erased

    def test_imported_loans_become_real_loans_once(self):
        self.pause()
        self.reset()
        self.restore("nation_name,money,loan\nAlpha,100,2500\nBeta,50,0\n")
        with self.db.tx() as c:
            res = LN.adopt_imported(c, actor="9", due_days=14)
        self.assertEqual((res["loans"], res["total_cents"]), (1, 250000))
        l = self.loan_row(1)
        self.assertEqual((l["nation_id"], l["source"], l["interest_cents"]), (1, "IMPORTED", 0))
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT status FROM imported_loans").fetchone()[0], "APPLIED")
        with self.db.tx() as c:
            self.assertEqual(LN.adopt_imported(c, actor="9")["loans"], 0)
        self.assertEqual(self.count("loans"), 1)

    def test_overdue_is_detected_and_announced_once(self):
        lid = self.make(days=1)
        with self.db.read() as c:
            self.assertEqual(LN.newly_overdue(c), [])
        raw = sqlite3.connect(str(self.db.path))
        raw.execute("UPDATE loans SET due_at='2020-01-01T00:00:00Z' WHERE id=?", (lid,))
        raw.commit()
        raw.close()
        self.assertTrue(LN.is_overdue(self.loan_row(lid)))
        self.assertEqual(self.run_async(self.svc.announce_overdue_loans()), 1)
        self.assertEqual(self.run_async(self.svc.announce_overdue_loans()), 0)       # never nags twice
        with self.db.tx() as c:
            LN.write_off(c, loan_id=lid, reason="settled by ECON", actor="9")
        self.assertFalse(LN.is_overdue(self.loan_row(lid)))

    def test_loan_amounts_can_never_be_edited_or_deleted(self):
        self.make()
        with self.db.tx() as c:
            LN.deduct(c, nation_id=1, amount_cents=60000, actor="9", note="part payment")   # some interest + principal paid
        for sql in ("UPDATE loans SET principal_cents=1", "UPDATE loans SET interest_cents=0",
                    "UPDATE loans SET principal_paid_cents=0", "UPDATE loans SET interest_paid_cents=0", "UPDATE loans SET nation_id=2",
                    "DELETE FROM loans", "UPDATE loan_events SET principal_cents=1", "DELETE FROM loan_events"):
            with self.assertRaises(sqlite3.Error, msg=sql):
                with self.db.tx() as c:
                    c.execute(sql)

    def test_reconciliation_notices_loan_books_that_do_not_match_their_history(self):
        lid = self.make()
        with self.db.tx() as c:                                    # someone pushes a payment in without a history row
            c.execute("UPDATE loans SET principal_paid_cents=500 WHERE id=?", (lid,))
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**13}, snapshot_id=None, triggered_by="t")
        self.assertTrue(any("loan" in str(f).lower() for f in res["findings"]), res["findings"])


class TestLoanCommands(LoanBase):
    def test_add_needs_minister_and_confirmation_and_sends_no_money(self):
        i = self.call(self.loan, "add", self.alice, "alpha", "10m", "loan for war")
        self.assertIn("Minister", i.text())
        self.assertEqual(self.count("loans"), 0)
        i = self.call(self.loan, "add", self.minister, "alpha", "10m", "loan for war", interest_percent=5, due_days=30, auto_confirm=False)
        self.assertEqual(self.count("loans"), 0)
        i = self.call(self.loan, "add", self.minister, "alpha", "10m", "loan for war", interest_percent=5, due_days=30)
        self.assertIn("recorded", i.text())
        self.assertEqual(self.loan_row(1)["interest_cents"], 50000000)
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_a_member_sees_only_their_own_loans(self):
        self.make()
        i = self.call(self.loan, "mine", self.alice)
        self.assertIn("$10,500.00", i.text())
        i = self.call(self.loan, "view", self.alice, "alpha")
        self.assertIn("Auditor", i.text())                         # members cannot look at anyone's loans
        i = self.call(self.loan, "list", self.alice)
        self.assertIn("Auditor", i.text())

    def test_dashboard_shows_the_loan_separately_from_the_deposit(self):
        self.make()
        out = self.call(self.bank, "dashboard", self.alice).text()
        self.assertIn("owed to the alliance (separate from your deposit)", out)
        self.assertIn("$10,500.00", out)

    def test_staff_list_and_view(self):
        self.make()
        out = self.call(self.loan, "list", self.admin).text()
        self.assertIn("Active loans", out)
        self.assertIn("$10,500.00", out)
        self.assertIn("#1", self.call(self.loan, "view", self.admin, "alpha").text())

    def test_deduct_command_confirms_then_pays(self):
        self.make(principal=100000, pct=0)
        i = self.call(self.loan, "deduct", self.minister, "alpha", "400", "settle", auto_confirm=False)
        self.assertEqual(self.bal(1)["money"], 500000)
        i = self.call(self.loan, "deduct", self.minister, "alpha", "400", "settle")
        self.assertIn("Deducted", i.text())
        self.assertEqual(self.bal(1)["money"], 460000)
        i = self.call(self.loan, "deduct", self.minister, "alpha", "999999", "too much")
        self.assertIn("more than is owed", i.text())

    def test_writeoff_and_adopt_are_admin_only(self):
        lid = self.make()
        self.assertIn("Admin", self.call(self.loan, "writeoff", self.minister, lid, "forgive it all").text())
        self.assertIn("Admin", self.call(self.loan, "adopt", self.minister).text())
        self.assertIn("written off", self.call(self.loan, "writeoff", self.admin, lid, "forgive it all").text())

    def test_applypayment_command(self):
        self.make(principal=100000, pct=0)
        self.scan()
        self.repay_record(9701, 250.00)
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "t", "9")
        self.scan()
        with self.db.tx() as c:
            L.set_emergency_lock(c, False, "t", "9")
        self.assertIn("Banker", self.call(self.loan, "applypayment", self.alice, 9701).text())
        self.assertIn("Applied", self.call(self.loan, "applypayment", self.banker, 9701).text())
        self.assertIn("already applied", self.call(self.loan, "applypayment", self.banker, 9701).text())

    def test_help_knows_every_loan_command(self):
        from tunbank import cmds_help
        for name in ("add", "adopt", "list", "view", "mine", "deduct", "writeoff", "applypayment"):
            self.assertIn(f"loan {name}", cmds_help.CATALOG)


if __name__ == "__main__":
    unittest.main()
