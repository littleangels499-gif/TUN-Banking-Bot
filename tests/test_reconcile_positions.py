"""Reconciliation: resource level vs overall net position vs liquidity. Integrity protections stay as strict as ever."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
import test_reset_restore as TR  # noqa: E402

from tunbank import alerts as A  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank import recon_report as RR  # noqa: E402
from tunbank import reconcile as R  # noqa: E402

discord = TC.discord
PR = {r: Decimal("1.00") for r in M.NON_CASH}
PR.update(food=Decimal("2.00"), steel=Decimal("5.00"))


class ReconBase(TR.ResetBase):
    # Alpha: $100,000.00 cash, 800 food, owes 500 steel.
    BAL = "nation_name,money,food,steel\nAlpha,100000,800,-500\n"

    def setUp(self):
        super().setUp()

        async def prices():
            return dict(PR)
        self.pnw.fetch_prices = prices
        self.fund_alice({"money": 1})
        self.pause()
        self.reset()
        self.restore(self.BAL)
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
        self.bank_has(money=9_600_000, food=200_000)          # cash $96,000.00 (SHORT $4,000), food 2,000 (surplus), steel 0

    def bank_has(self, **units):
        self.pnw.holdings = {r: units.get(r, 0) for r in M.RESOURCES}

    def recon(self):
        return self.run_async(self.svc.do_reconcile("test"))

    def state(self):
        with self.db.read() as c:
            return L.integrity_state(c)

    def wd(self, amounts, key, nid=1):
        return self.run_async(self.svc.wd.request(
            tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=nid, lock_id=None, dest_nation_id=nid,
            amounts=amounts, actor="111", note="n", reason="r", idempotency_key=key))


class TestPositions(ReconBase):
    def test_per_resource_difference_is_bank_minus_members_net(self):
        with self.db.read() as c:
            pos = R.resource_positions(c, self.pnw.holdings)
        self.assertEqual(pos["money"]["difference"], 9_600_000 - 10_000_000)             # -$4,000.00 short
        self.assertEqual(pos["food"]["difference"], 200_000 - 80_000)                    # surplus
        self.assertEqual((pos["steel"]["member_net"], pos["steel"]["owed"], pos["steel"]["difference"]), (-50_000, 50_000, 50_000))

    def test_the_users_scenario_is_a_warning_not_a_lock(self):
        res = self.recon()
        self.assertEqual(res["status"], "WARNING")
        self.assertEqual({(f["kind"], f["severity"]) for f in res["findings"]}, {("RESOURCE_SHORTFALL", "WARNING")})
        self.assertGreater(res["position"]["net_cents"], 0)                              # surplus food + steel outweigh the cash gap
        st = self.state()
        self.assertEqual((st["state"], st["emergency_lock"]), ("WARNING", False))

    def test_banking_continues_but_a_short_resource_is_refused_by_liquidity(self):
        self.recon()
        ok = self.wd({"food": 5000}, "w1")                                               # food is in surplus: fine
        self.assertEqual(ok.status, "COMPLETED", ok.message)
        refused = self.wd({"money": 9_800_000}, "w2")                                    # $98,000 but the bank holds $96,000
        self.assertEqual(refused.status, "BLOCKED")
        self.assertIn("physically hold", refused.internal)
        self.assertNotIn("physically", refused.message)                                  # members learn nothing about the treasury
        self.assertEqual(self.bal(1)["money"], 10_000_000)
        ok2 = self.wd({"money": 9_000_000}, "w3")                                        # $90,000 the bank CAN pay
        self.assertEqual(ok2.status, "COMPLETED", ok2.message)

    def test_overall_negative_position_requires_reconciliation_without_locking(self):
        self.bank_has(money=1_000_000, food=100_000)                                     # cash gap $90,000 outweighs the surplus
        res = self.recon()
        kinds = {f["kind"]: f["severity"] for f in res["findings"]}
        self.assertEqual(kinds["NET_POSITION_SHORTFALL"], "RECON")
        self.assertEqual(res["status"], "RECONCILIATION_REQUIRED")
        self.assertLess(res["position"]["net_cents"], 0)
        st = self.state()
        self.assertEqual((st["state"], st["emergency_lock"]), ("RECONCILIATION_REQUIRED", False))
        self.assertEqual(self.wd({"food": 100}, "w4").status, "BLOCKED")                 # paused until ECON investigates

    def test_position_findings_clear_themselves_when_the_numbers_agree(self):
        self.bank_has(money=1_000_000, food=100_000)
        self.recon()
        self.assertEqual(self.state()["state"], "RECONCILIATION_REQUIRED")
        self.bank_has(money=10_000_000, food=200_000)                                    # funds arrive
        res = self.recon()
        self.assertEqual(res["status"], "NORMAL")
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM integrity_events WHERE status='OPEN'").fetchone()[0], 0)

    def test_an_unreadable_bank_never_clears_or_invents_a_shortfall(self):
        self.bank_has(money=1_000_000, food=100_000)
        self.recon()
        self.pnw.bank_readable = False
        res = self.recon()
        self.assertIn("BANK_UNAVAILABLE", {f["kind"] for f in res["findings"]})
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM integrity_events WHERE kind='NET_POSITION_SHORTFALL' AND status='OPEN'").fetchone()[0], 1)

    def test_missing_prices_give_a_warning_and_never_a_guess(self):
        async def prices():
            return {r: v for r, v in PR.items() if r != "steel"}
        self.pnw.fetch_prices = prices
        self.svc.prices._cached = None
        self.bank_has(money=1_000_000, food=100_000)
        res = self.recon()
        self.assertIsNone(res["position"]["net_cents"])
        self.assertNotIn("NET_POSITION_SHORTFALL", {f["kind"] for f in res["findings"]})
        self.assertIn("could not be calculated", " ".join(f["message"] for f in res["findings"]))

    def test_everything_covered_is_normal(self):
        self.bank_has(money=10_000_000, food=80_000)
        self.assertEqual(self.recon()["status"], "NORMAL")

    def test_in_flight_withdrawals_do_not_look_like_a_shortfall(self):
        with self.db.tx() as c:
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None,
                               dest_nation_id=1, amounts={"money": 100_000}, actor="111", note="n", reason="r", idempotency_key="f1")
        self.bank_has(money=10_000_000, food=80_000)
        self.assertEqual(self.recon()["status"], "NORMAL")


class TestIntegrityStillLocks(ReconBase):
    def test_tampered_balance_still_engages_the_emergency_lock(self):
        self.bank_has(money=10_000_000, food=200_000)
        with self.db.tx() as c:
            c.execute("UPDATE balances SET amount=amount+1 WHERE nation_id=1 AND resource='food'")
        res = self.recon()
        self.assertIn("BALANCE_MISMATCH", {f["kind"] for f in res["findings"]})
        self.assertEqual(res["status"], "EMERGENCY_LOCK")
        self.assertTrue(self.state()["emergency_lock"])

    def test_a_resource_shortfall_never_hides_a_real_problem(self):
        with self.db.tx() as c:
            c.execute("UPDATE balances SET amount=amount+1 WHERE nation_id=1 AND resource='food'")
        res = self.recon()                                                               # cash is short AND a balance was tampered with
        kinds = {f["kind"] for f in res["findings"]}
        self.assertTrue({"RESOURCE_SHORTFALL", "BALANCE_MISMATCH"} <= kinds)
        self.assertEqual(res["status"], "EMERGENCY_LOCK")

    def test_broken_hash_chain_still_locks(self):
        import sqlite3
        self.bank_has(money=10_000_000, food=200_000)
        raw = sqlite3.connect(str(self.db.path))
        raw.execute("DROP TRIGGER trg_ledger_no_update")
        raw.execute("UPDATE ledger_entries SET delta=delta+1 WHERE id=(SELECT MAX(id) FROM ledger_entries)")
        raw.commit()
        raw.close()
        self.assertEqual(self.recon()["status"], "EMERGENCY_LOCK")


class TestOldBlanketLockIsLifted(ReconBase):
    def test_lock_caused_only_by_the_old_rule_is_lifted_and_the_event_closed(self):
        self.bank_has(money=10_000_000, food=200_000)
        with self.db.tx() as c:                                                          # what the previous version did
            L.raise_event(c, "CRITICAL", "LEDGER_EXCEEDS_BANK", details={"message": "old"}, dedupe_key="recon:ledger-exceeds-bank")
        self.assertTrue(self.state()["emergency_lock"])
        res = self.recon()
        self.assertEqual(res["status"], "NORMAL")
        self.assertFalse(self.state()["emergency_lock"])
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='EMERGENCY_LOCK_OFF'").fetchone()[0], 1)

    def test_a_lock_with_another_critical_cause_is_left_alone(self):
        self.bank_has(money=10_000_000, food=200_000)
        with self.db.tx() as c:
            L.raise_event(c, "CRITICAL", "LEDGER_EXCEEDS_BANK", details={"message": "old"}, dedupe_key="recon:ledger-exceeds-bank")
            L.raise_event(c, "CRITICAL", "DUPLICATE_CREDIT", details={"message": "real"}, dedupe_key="x:dup")
        self.recon()
        self.assertTrue(self.state()["emergency_lock"])

    def test_a_manual_lock_is_never_lifted_by_reconciliation(self):
        self.bank_has(money=10_000_000, food=200_000)
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "ECON locked it by hand", "9")
        self.recon()
        self.assertTrue(self.state()["emergency_lock"])


class TestReport(ReconBase):
    def test_report_shows_every_resource_the_overall_position_and_the_status(self):
        i = self.call(self.ledger, "reconcile", self.admin)
        text = i.text()
        flat = text.replace("\n", " ")
        for want in ("TUN BANK RECONCILIATION", "WARNING", "Cash", "Bank $96.00K", "Members $100.00K",
                     "Difference **-$4.00K** ⚠️", "Food", "Steel", "owed to the alliance", "Overall net market position",
                     "Liquidity", "What to do", "No action is needed to keep banking running"):
            self.assertIn(want, flat)
        self.assertLess(flat.index("Cash"), flat.index("Food"))                          # shortages are listed first

    def test_steel_owed_by_members_shows_as_covered(self):
        text = self.call(self.ledger, "reconcile", self.admin).text()
        line = [ln for ln in text.splitlines() if "Steel" in ln][0]
        self.assertIn("Members -500", line)
        self.assertIn("✓", line)

    def test_figures_are_hidden_from_staff_without_the_holdings_permission(self):
        with self.db.tx() as c:
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('AUDITOR','66')")
        aud = discord.User(401, "aud")
        aud.roles = [discord.Role(66)]
        text = self.call(self.ledger, "reconcile", aud).text()
        self.assertIn("Short in the bank: Cash", text)
        self.assertNotIn("$96", text)
        self.assertNotIn("Bank $", text)

    def test_the_ledger_command_gives_the_same_report(self):
        self.assertIn("TUN BANK RECONCILIATION", self.call(self.ledger, "reconcile", self.admin).text())

    def test_every_status_has_guidance(self):
        for st in RR.STATUS:
            self.assertTrue(RR.what_to_do(st, ["Cash"], "why"))

    def test_alert_for_a_shortfall_keeps_treasury_figures_out_of_the_shared_channel(self):
        res = self.recon()
        ev = [f for f in res["findings"] if f["kind"] == "RESOURCE_SHORTFALL"][0]
        card = A.integrity_card(dict(ev))
        text = card.as_text()
        self.assertIn("No action needed", text)
        self.assertNotIn("4000", text)
        self.assertNotIn("net_value_cents", text)
        self.assertNotIn("$", text.replace("Net", ""))

    def test_internal_conversion_no_longer_trips_a_lock(self):
        """The exact situation that started this: convert between resources while the bank is short in cash."""
        from tunbank import conversion as CV
        self.bank_has(money=9_600_000, food=200_000, steel=100_000)
        snap = self.run_async(self.svc.prices.get())
        q = CV.make_quote(snap, "food", "steel", 10_000)
        with self.db.tx() as c:
            CV.execute(c, nation_id=1, quote=q, actor="111", idempotency_key="k", holdings=self.pnw.holdings)
        res = self.recon()
        self.assertEqual(res["status"], "WARNING")
        self.assertFalse(self.state()["emergency_lock"])
        self.assertEqual(self.wd({"food": 100}, "after-conv").status, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
