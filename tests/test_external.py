"""External withdrawals (B1) and member DM notifications (B3), through the real scanner and commands."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
from test_core import rec  # noqa: E402

from tunbank import external as EX  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import notify  # noqa: E402
from tunbank import offshore_ledger as OL  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402


class FakeUser:
    def __init__(self, uid, fail=False):
        self.id, self.fail, self.got = uid, fail, []

    async def send(self, embed=None, **kw):
        if self.fail:
            raise RuntimeError("Cannot send messages to this user")
        self.got.append(embed)


class DMBot:
    def __init__(self):
        self.users, self.chans = {}, {}

    def user(self, uid, fail=False):
        self.users[uid] = FakeUser(uid, fail)
        return self.users[uid]

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        return self.users[uid]

    def get_channel(self, i):
        return self.chans.get(i)


class XBase(TC.OffshoreCmd):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})                      # Alice (nation 1, linked) owns $1,000 of real deposited money
        self.bot = DMBot()
        self.alice_dm = self.bot.user(self.alice.id)
        self.svc.alerts.bot = self.bot

    def off(self, rid, amounts, note="", receiver=1, receiver_type=1):
        self.pnw.off_recs.append(rec(rid, self.OFF, amounts, note, receiver=receiver, sender_type=2, receiver_type=receiver_type))

    def scan(self):
        res = self.run_async(self.svc.scanner.scan())
        self.assertTrue(res.ok, res.error)
        return res

    def avail(self, nid=1):
        with self.db.read() as c:
            return L.get_balances(c, nid, "AVAILABLE")

    def journal(self):
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM external_withdrawals ORDER BY id")]

    def status(self, rid):
        with self.db.read() as c:
            return c.execute("SELECT classification, status FROM pnw_records WHERE id=?", (rid,)).fetchone()


# XBase inherits the whole OffshoreCmd suite; those tests already run in test_commands, so do not collect them again here.
for _name in dir(TC.OffshoreCmd):
    if _name.startswith("test_"):
        setattr(XBase, _name, None)


class TestAutoDeduction(XBase):
    def test_confident_external_withdrawal_is_deducted_once_with_full_record(self):
        self.off(8001, {"money": 300}, "withdrawal")
        res = self.scan()
        self.assertEqual(self.avail()["money"], 70000)                              # $1,000 - $300
        j = self.journal()
        self.assertEqual(len(j), 1)
        self.assertEqual((j[0]["pnw_record_id"], j[0]["nation_id"], j[0]["decision"], j[0]["decided_by"]),
                         (8001, 1, "AUTO", "system:scanner"))
        self.assertEqual(json.loads(j[0]["amounts_json"]), {"money": 30000})
        self.assertEqual(json.loads(j[0]["before_json"]), {"money": 100000})
        self.assertEqual(json.loads(j[0]["after_json"]), {"money": 70000})
        self.assertEqual(tuple(self.status(8001)), ("EXTERNAL_OUTFLOW", "NO_CREDIT"))
        self.assertIn("EXTERNAL_WITHDRAWAL", [o.kind for o in res.outcomes])
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='EXTERNAL_WITHDRAWAL'").fetchone()[0], 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertEqual(EX.verify(c), [])
            lab = c.execute(f"SELECT {L.LABELED_TYPE_SQL} AS t, entry_type FROM ledger_entries ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual((lab["t"], lab["entry_type"]), ("EXTERNAL_WITHDRAWAL", "ADJUSTMENT"))   # distinguishable from manual adjustments
        self.scan()
        self.scan()                                                                  # re-scanning never charges it again
        self.assertEqual(self.avail()["money"], 70000)
        self.assertEqual(len(self.journal()), 1)

    def test_insufficient_balance_goes_negative_truthfully(self):
        self.off(8002, {"money": 1500}, "cash out")
        self.scan()
        self.assertEqual(self.avail()["money"], -50000)                              # a real, visible debt - not clamped
        self.assertEqual(json.loads(self.journal()[0]["negative_json"]), {"money": -50000})
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='EXTERNAL_WITHDRAWAL_NEGATIVE_BALANCE'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM integrity_events WHERE kind='EXTERNAL_WITHDRAWAL_DEBT'").fetchone()[0], 1)

    def test_other_resources_are_deducted_from_the_right_resource_only(self):
        self.off(8003, {"money": 100, "coal": 50}, "mine")
        self.scan()
        bal = self.avail()
        self.assertEqual(bal["money"], 90000)
        self.assertEqual(bal["coal"], -5000)


class TestNeverGuess(XBase):
    def assert_untouched(self, rid, before=100000):
        self.assertEqual(self.avail().get("money", 0), before)
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.status(rid)["status"], "AWAITING_REVIEW")

    def test_grants_and_payments_by_note_go_to_review(self):
        for i, note in enumerate(("War grant for Beta", "Supplier payment #4", "reimbursement"), start=8100):
            self.off(i, {"money": 100}, note)
        self.scan()
        for i in (8100, 8101, 8102):
            self.assertEqual(self.status(i)["status"], "AWAITING_REVIEW")
        self.assertEqual(self.avail()["money"], 100000)
        self.assertEqual(self.journal(), [])

    def test_exempt_nation_goes_to_review(self):
        with self.db.tx() as c:
            cfg_set(c, "external_withdrawal_exempt_nations", "1", "t")
        self.off(8110, {"money": 100}, "x")
        self.scan()
        self.assert_untouched(8110)

    def test_unlinked_account_goes_to_review(self):
        with self.db.tx() as c:
            L.ensure_member(c, 2, "Beta")                                           # account exists, no Discord link
        self.off(8120, {"money": 100}, "x", receiver=2)
        self.scan()
        self.assertEqual(self.status(8120)["status"], "AWAITING_REVIEW")
        self.assertEqual(self.avail(2), {})
        self.assertEqual(self.journal(), [])

    def test_recipient_outside_the_alliance_goes_to_review(self):
        self.pnw.members = {2: "Beta", 3: "Gamma"}                                  # nation 1 is not in the alliance right now
        self.off(8130, {"money": 100}, "x")
        self.scan()
        self.assert_untouched(8130)

    def test_unreadable_member_list_goes_to_review(self):
        async def boom():
            from tunbank.pnw import PnWUncertain
            raise PnWUncertain("down")
        self.pnw.fetch_alliance_members = boom
        self.off(8140, {"money": 100}, "x")
        self.scan()
        self.assert_untouched(8140)

    def test_nation_with_no_account_and_alliance_payouts_are_not_member_charges(self):
        self.off(8150, {"money": 100}, "x", receiver=3)                             # nation 3 has no TUN account
        self.off(8151, {"money": 100}, "alliance payout", receiver=999, receiver_type=2)   # sent to another alliance
        self.scan()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.avail()["money"], 100000)

    def test_review_only_and_off_modes(self):
        with self.db.tx() as c:
            cfg_set(c, "external_withdrawal_mode", "review", "t")
        self.off(8160, {"money": 100}, "x")
        self.scan()
        self.assert_untouched(8160)
        with self.db.tx() as c:
            cfg_set(c, "external_withdrawal_mode", "off", "t")
        self.off(8161, {"money": 100}, "x")
        self.scan()
        self.assertEqual(self.journal(), [])
        with self.db.tx() as c:
            cfg_set(c, "external_withdrawal_mode", "garbage", "t")                   # an unknown value can never switch auto on
            self.assertEqual(EX.mode(c), "review")

    def test_emergency_lock_sends_it_to_review(self):
        with self.db.tx() as c:
            L.set_state(c, "emergency_lock", "1")
        self.off(8170, {"money": 100}, "x")
        self.scan()
        self.assertEqual(self.journal(), [])


class TestStaffReview(XBase):
    def test_staff_can_charge_a_reviewed_record_exactly_once(self):
        self.off(8200, {"money": 250}, "War grant")
        self.scan()
        self.assertEqual(self.avail()["money"], 100000)
        i = self.call(self.bank, "review", self.admin, 8200, TC.discord.app_commands.Choice("withdrawal", "withdrawal"), "", "it was a withdrawal")
        self.assertIn("resolved", i.text())
        self.assertEqual(self.avail()["money"], 75000)
        j = self.journal()
        self.assertEqual((len(j), j[0]["decision"], j[0]["decided_by"]), (1, "STAFF", str(self.admin.id)))
        i = self.call(self.bank, "review", self.admin, 8200, TC.discord.app_commands.Choice("withdrawal", "withdrawal"), "", "again")
        self.assertIn("Not applied", i.text())
        self.assertEqual(self.avail()["money"], 75000)

    def test_cannot_charge_a_different_nation_than_the_recipient(self):
        self.off(8210, {"money": 250}, "War grant")
        self.scan()
        i = self.call(self.bank, "review", self.admin, 8210, TC.discord.app_commands.Choice("withdrawal", "withdrawal"), "2", "x")
        self.assertIn("Not applied", i.text())
        self.assertEqual(self.journal(), [])

    def test_alliance_money_decision_still_works_and_charges_nobody(self):
        self.off(8220, {"money": 250}, "War grant")
        self.scan()
        self.call(self.bank, "review", self.admin, 8220, TC.discord.app_commands.Choice("alliance", "alliance"), "", "grant")
        self.assertEqual(self.avail()["money"], 100000)
        self.assertEqual(self.journal(), [])


class TestJournalGuards(XBase):
    def test_database_refuses_a_journal_row_without_the_real_pnw_record(self):
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                c.execute("INSERT INTO external_withdrawals(pnw_record_id,nation_id,source_bank_id,source_label,amounts_json,before_json,"
                          "after_json,decision,decided_by,note,group_id,created_at) VALUES(424242,1,777,'offshore','{}','{}','{}','AUTO','x','n','g','t')")

    def test_journal_is_immutable(self):
        self.off(8300, {"money": 100}, "x")
        self.scan()
        with self.db.tx() as c:
            for sql in ("UPDATE external_withdrawals SET note='y'", "DELETE FROM external_withdrawals"):
                with self.assertRaises(sqlite3.DatabaseError):
                    c.execute(sql)

    def test_tampering_is_caught_by_reconciliation(self):
        self.off(8310, {"money": 100}, "x")
        self.scan()
        with self.db.tx() as c:                                                      # a stray adjustment in the journal's own group
            g = c.execute("SELECT group_id FROM external_withdrawals").fetchone()[0]
            aid = c.execute("INSERT INTO adjustments(nation_id,bucket,reason,evidence,actor,created_at) VALUES(1,'AVAILABLE','r','e','a','t')").lastrowid
            c.execute("INSERT INTO adjustment_items(adjustment_id,resource,delta) VALUES(?, 'money', -5)", (aid,))
            L.post_entries(c, [dict(group_id=g, nation_id=1, bucket="AVAILABLE", resource="money", delta=-5, entry_type="ADJUSTMENT",
                                    adjustment_id=aid, actor="a", note="n")])
        with self.db.read() as c:
            self.assertTrue(EX.verify(c))


class TestSharedOffshore(XBase):
    X = 555

    def setUp(self):
        super().setUp()
        with self.db.tx() as c:
            OL.enable_shared(c, actor="9", host_alliance_id=TC.ALLIANCE, host_name="TUN")
            OL.register(c, alliance_id=self.X, name="RN", actor="9")
            OL.assign(c, alliance_id=self.X, amounts={"money": 500000}, physical={"money": 1000000}, actor="9", note="RN share")
            OL.assign(c, alliance_id=OL.host_id(c), amounts={"money": 200000}, physical={"money": 1000000}, actor="9", note="TUN share")

    def test_member_withdrawal_reduces_the_host_share_not_rns(self):
        self.off(8400, {"money": 300}, "withdrawal")
        self.scan()
        self.assertEqual(self.avail()["money"], 70000)
        with self.db.read() as c:
            self.assertEqual(OL.balances(c, self.X)["money"], 500000)                # RN's share is untouched
            self.assertEqual(OL.balances(c, OL.host_id(c))["money"], 200000 - 30000)

    def test_payout_to_rns_nation_or_alliance_is_never_charged_to_a_tun_member(self):
        self.off(8410, {"money": 300}, "RN member payout", receiver=4242)           # a nation that is not in TUN
        self.off(8411, {"money": 300}, "RN share", receiver=self.X, receiver_type=2)
        self.scan()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.avail()["money"], 100000)

    def test_an_rn_nation_that_is_linked_here_but_left_goes_to_review(self):
        self.pnw.members = {2: "Beta"}                                              # nation 1 no longer counts as a TUN member
        self.off(8420, {"money": 300}, "x")
        self.scan()
        self.assertEqual(self.journal(), [])
        with self.db.read() as c:
            self.assertEqual(OL.balances(c, OL.host_id(c))["money"], 200000)


class TestNotifications(XBase):
    def flush(self):
        self.run_async(self.svc.alerts.flush_notifications())

    def rows(self):
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM member_notifications ORDER BY id")]

    def test_external_withdrawal_dm_has_everything_the_member_needs(self):
        self.off(8500, {"money": 300}, "withdrawal")
        self.scan()
        self.flush()
        self.assertEqual(len(self.alice_dm.got), 1)
        text = " ".join(f"{e.title} {e.description} " + " ".join(f"{n} {v}" for n, v, _ in e.fields) for e in self.alice_dm.got)
        for must in ("External withdrawal detected", "[#1]", "Money", "$300", "$1,000", "$700", "Detected externally",
                     "PnW record #8500", "Not you?"):
            self.assertIn(must, text)
        self.assertEqual([r["status"] for r in self.rows()], ["SENT"])
        self.flush()
        self.assertEqual(len(self.alice_dm.got), 1)                                  # delivered exactly once

    def test_a_failed_dm_is_recorded_and_never_touches_the_transaction(self):
        self.alice_dm.fail = True
        self.off(8510, {"money": 300}, "withdrawal")
        self.scan()
        self.flush()
        self.assertEqual(self.avail()["money"], 70000)                               # the deduction stands
        r = self.rows()[0]
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("Cannot send", r["error"])
        with self.db.read() as c:
            self.assertEqual(notify.failure_count(c), 1)
            self.assertEqual(len(notify.failures(c)), 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])

    def test_dms_can_be_switched_off(self):
        with self.db.tx() as c:
            cfg_set(c, "dm_members", "0", "t")
        self.off(8520, {"money": 100}, "x")
        self.scan()
        self.flush()
        self.assertEqual(self.alice_dm.got, [])
        self.assertEqual(self.rows()[0]["status"], "SKIPPED")

    def test_unlinked_members_have_nobody_to_notify_but_are_still_recorded(self):
        with self.db.tx() as c:
            self.assertFalse(notify.enqueue(c, dedupe_key="k", nation_id=999, kind="X", title="t", deltas={"money": 1}))

    def test_member_withdrawal_dm_after_it_is_recorded(self):
        self.pnw.holdings = {"money": 10**9}
        self.pnw.off_holdings = {"money": 10**9}                       # the offshore physically holds it (member withdrawals are paid from there)
        i = self.call(self.bank, "withdrawself", self.alice, "money=400")
        self.assertIn("Withdrawal completed", i.text())
        self.flush()
        wd = [r for r in self.rows() if r["kind"] == "WITHDRAWAL"]
        self.assertEqual(len(wd), 1)
        text = " ".join(f"{e.title} " + " ".join(f"{n} {v}" for n, v, _ in e.fields) for e in self.alice_dm.got)
        for must in ("Withdrawal completed", "$400", "TUN-TX", "Through TUN Bank"):
            self.assertIn(must, text)

    def test_adjustment_and_credit_dms(self):
        self.pnw.holdings = {"money": 100000 + 500000}
        self.call(self.bank, "adjust", self.admin, 1, "overstated", "ticket", remove="money=100")
        self.flush()
        kinds = [r["kind"] for r in self.rows()]
        self.assertEqual(kinds.count("ADJUSTMENT") + kinds.count("CREDIT"), len(kinds))


if __name__ == "__main__":
    unittest.main()
