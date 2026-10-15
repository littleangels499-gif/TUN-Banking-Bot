"""Funded balance transfers (/bank credit) and the configurable reconciliation gate."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_reset_restore as TR  # noqa: E402

from tunbank import ledger as L  # noqa: E402
from tunbank import offshore_ledger as OL  # noqa: E402
from tunbank import reconcile as R  # noqa: E402
from tunbank import transfers as T  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402

OPERATOR = "900001"
APPROVER = "222"


class TBase(TR.ResetBase):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})                       # Alice (nation 1) owns $1,000 of real, deposited money
        self.holdings = {"money": 100000 + 500000}             # the real bank holds Alice's $1,000 + $5,000 that is the alliance's
        self.pnw.holdings = dict(self.holdings)
        self.n = 0
        with self.db.tx() as c:
            L.ensure_member(c, 2, "Beta")

    def approved(self, payload, requester=OPERATOR):
        with self.db.tx() as c:
            aid = L.create_approval(c, "ADJUSTMENT", payload, requester, "r")
            L.approve(c, aid, APPROVER)
        return aid

    def transfer(self, *, recipient=1, amounts=None, stype="ALLIANCE", sid=None, reason="reward", reference="",
                 override=False, ov_reason="", holdings="default", idem=None, approval="auto", operator=OPERATOR):
        amounts = {"money": 10000} if amounts is None else amounts
        payload = T.payload_for(recipient=recipient, amounts=amounts, source_type=stype, source_id=sid, reason=reason,
                                reference=reference, override=override)
        aid = self.approved(payload, operator) if approval == "auto" else approval
        self.n += 1
        with self.db.tx() as c:
            return T.post_transfer(c, recipient=recipient, amounts=amounts, source_type=stype, source_id=sid, reason=reason,
                                   reference=reference, operator=operator, approval_id=aid, idem=idem or f"t{self.n}",
                                   holdings=self.holdings if holdings == "default" else holdings, override=override,
                                   override_reason=ov_reason)

    def bal(self, nid, bucket="AVAILABLE"):
        with self.db.read() as c:
            return L.get_balances(c, nid, bucket)

    def lines(self):
        with self.db.read() as c:
            return [tuple(r) for r in c.execute("SELECT account_type, account_id, resource, delta FROM balance_transfer_lines ORDER BY id")]


class TestAllianceFunded(TBase):
    def test_credit_is_funded_and_both_sides_are_recorded(self):
        res = self.transfer(amounts={"money": 10000})
        self.assertEqual(res["ref"], "TUN-ADJ1")
        self.assertEqual(self.bal(1)["money"], 110000)
        self.assertEqual(self.lines(), [("MEMBER", 1, "money", 10000), ("ALLIANCE", None, "money", -10000)])
        with self.db.read() as c:
            self.assertEqual(T.verify(c), [])
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertTrue(L.verify_chain(c, "audit_log")["ok"])
            a = c.execute("SELECT * FROM audit_log WHERE action='BALANCE_TRANSFER'").fetchone()
            self.assertIsNotNone(a)

    def test_books_still_balance_after_credit(self):
        self.transfer(amounts={"money": 400000})                 # $4,000 of the alliance's $5,000
        with self.db.read() as c:
            pos = R.bank_position(c, self.holdings)
        self.assertEqual(pos["alliance_owned"]["money"], 100000)  # $1,000 left; nothing short
        self.assertFalse(any(v < 0 for v in pos["alliance_owned"].values()))

    def test_more_than_the_alliance_owns_is_refused(self):
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(amounts={"money": 600000})
        self.assertIn("short", str(cx.exception))
        self.assertEqual(self.bal(1)["money"], 100000)
        self.assertEqual(self.lines(), [])

    def test_unreadable_bank_means_it_cannot_be_verified(self):
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(holdings=None)
        self.assertIn("could not be read", str(cx.exception))
        self.assertEqual(self.bal(1)["money"], 100000)

    def test_deficit_override_needs_justification_and_is_flagged(self):
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={"money": 600000}, override=True, ov_reason="  ")
        res = self.transfer(amounts={"money": 600000}, override=True, ov_reason="Prize promised in the war announcement")
        self.assertTrue(res["overridden"])
        self.assertEqual(self.bal(1)["money"], 100000 + 600000)
        with self.db.read() as c:
            row = c.execute("SELECT deficit_override, override_reason, override_by FROM balance_transfers").fetchone()
            self.assertEqual(tuple(row), (1, "Prize promised in the war announcement", OPERATOR))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='BALANCE_TRANSFER_DEFICIT_OVERRIDE'").fetchone()[0], 1)
            pos = R.bank_position(c, self.holdings)
        self.assertLess(pos["alliance_owned"]["money"], 0)        # the deficit is visible to reconciliation, not hidden

    def test_override_flag_is_not_recorded_when_nothing_was_short(self):
        self.transfer(amounts={"money": 100}, override=True, ov_reason="just in case")
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT deficit_override FROM balance_transfers").fetchone()[0], 0)

    def test_no_silent_account_creation(self):
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(recipient=3)
        self.assertIn("no TUN Bank account", str(cx.exception))
        with self.db.read() as c:
            self.assertIsNone(L.get_member(c, 3))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM balance_transfers").fetchone()[0], 0)

    def test_reason_is_mandatory_and_amounts_must_be_positive(self):
        with self.assertRaises(T.TransferError):
            self.transfer(reason="   ")
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={"money": -5})
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={})


class TestApprovalAndIdempotency(TBase):
    def test_no_approval_no_credit(self):
        with self.assertRaises(T.TransferError):
            self.transfer(approval=None)
        self.assertEqual(self.bal(1)["money"], 100000)

    def test_approval_for_a_different_amount_cannot_be_reused(self):
        aid = self.approved(T.payload_for(recipient=1, amounts={"money": 100}, source_type="ALLIANCE", source_id=None,
                                          reason="reward", reference="", override=False))
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(amounts={"money": 999999}, approval=aid)
        self.assertIn("different transfer", str(cx.exception))

    def test_self_approval_is_refused(self):
        payload = T.payload_for(recipient=1, amounts={"money": 100}, source_type="ALLIANCE", source_id=None, reason="reward",
                                reference="", override=False)
        with self.db.tx() as c:
            aid = L.create_approval(c, "ADJUSTMENT", payload, OPERATOR, "r")
            with self.assertRaises(L.LedgerError):
                L.approve(c, aid, OPERATOR)
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={"money": 100}, approval=aid)

    def test_approval_is_single_use(self):
        payload = T.payload_for(recipient=1, amounts={"money": 100}, source_type="ALLIANCE", source_id=None, reason="reward",
                                reference="", override=False)
        aid = self.approved(payload)
        self.transfer(amounts={"money": 100}, approval=aid)
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={"money": 100}, approval=aid)
        self.assertEqual(self.bal(1)["money"], 100000 + 100)

    def test_same_idempotency_key_never_credits_twice(self):
        first = self.transfer(idem="same")
        again = self.transfer(idem="same")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["transfer_id"], again["transfer_id"])
        self.assertEqual(self.bal(1)["money"], 110000)


class TestMemberSource(TBase):
    def setUp(self):
        super().setUp()
        self.transfer(recipient=2, amounts={"money": 50000}, idem="seed")          # Beta now has $500 (funded, recorded)

    def test_member_to_member_moves_exactly_the_same_amount(self):
        before = self.bal(1)["money"] + self.bal(2)["money"]
        self.transfer(recipient=1, amounts={"money": 20000}, stype="MEMBER", sid=2)
        self.assertEqual(self.bal(2)["money"], 30000)
        self.assertEqual(self.bal(1)["money"], 120000)
        self.assertEqual(self.bal(1)["money"] + self.bal(2)["money"], before)
        with self.db.read() as c:
            self.assertEqual(T.verify(c), [])
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])

    def test_source_member_with_too_little_is_refused_without_override(self):
        with self.assertRaises(T.TransferError):
            self.transfer(recipient=1, amounts={"money": 60000}, stype="MEMBER", sid=2)
        self.assertEqual(self.bal(2)["money"], 50000)

    def test_override_makes_a_visible_debt_not_a_clamp(self):
        self.transfer(recipient=1, amounts={"money": 60000}, stype="MEMBER", sid=2, override=True, ov_reason="agreed in ticket 12")
        self.assertEqual(self.bal(2)["money"], -10000)

    def test_funds_held_for_a_pending_withdrawal_cannot_be_taken(self):
        with self.db.tx() as c:                                         # a real in-flight withdrawal holds $300 of Beta's $500
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=2, lock_id=None,
                               dest_nation_id=2, amounts={"money": 30000}, actor="2", note="n", reason="r", idempotency_key="hold-1")
        with self.db.read() as c:
            self.assertEqual(T.member_available(c, 2), {"money": 20000})
        with self.assertRaises(T.TransferError):
            self.transfer(recipient=1, amounts={"money": 40000}, stype="MEMBER", sid=2)            # only $200 is free
        with self.assertRaises(L.LedgerError):                                                    # an override cannot dip into the hold either
            self.transfer(recipient=1, amounts={"money": 40000}, stype="MEMBER", sid=2, override=True, ov_reason="try")
        self.assertEqual(self.bal(2)["money"], 50000)

    def test_source_and_recipient_must_differ_and_exist(self):
        with self.assertRaises(T.TransferError):
            self.transfer(recipient=2, amounts={"money": 100}, stype="MEMBER", sid=2)
        with self.assertRaises(T.TransferError):
            self.transfer(recipient=1, amounts={"money": 100}, stype="MEMBER", sid=3)


class TestOwnershipSource(TBase):
    X = 555

    def setUp(self):
        super().setUp()
        with self.db.tx() as c:
            OL.enable_shared(c, actor=OPERATOR, host_alliance_id=TR.TC.ALLIANCE, host_name="TUN")
            OL.register(c, alliance_id=self.X, name="Friends", actor=OPERATOR)
            OL.assign(c, alliance_id=self.X, amounts={"money": 200000}, physical={"money": 600000}, actor=OPERATOR, note="opening share")

    def test_share_moves_to_host_and_member_is_credited(self):
        with self.db.read() as c:
            total_before = sum(OL.totals(c).values())
        self.transfer(recipient=1, amounts={"money": 50000}, stype="OWNERSHIP", sid=self.X)
        with self.db.read() as c:
            self.assertEqual(OL.balances(c, self.X)["money"], 150000)
            self.assertEqual(OL.balances(c, OL.host_id(c))["money"], 50000)
            self.assertEqual(sum(OL.totals(c).values()), total_before)             # shares only moved, none created
            self.assertEqual(T.verify(c), [])
        self.assertEqual(self.bal(1)["money"], 150000)
        self.assertEqual(self.lines(), [("MEMBER", 1, "money", 50000), ("OWNERSHIP", self.X, "money", -50000)])

    def test_an_ownership_share_can_never_go_negative_and_cannot_be_overridden(self):
        with self.assertRaises(T.TransferError):
            self.transfer(amounts={"money": 300000}, stype="OWNERSHIP", sid=self.X)
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(amounts={"money": 300000}, stype="OWNERSHIP", sid=self.X, override=True, ov_reason="please")
        self.assertIn("cannot be overridden", str(cx.exception))
        with self.db.read() as c:
            self.assertEqual(OL.balances(c, self.X)["money"], 200000)

    def test_unregistered_alliance_and_host_itself_are_refused(self):
        with self.assertRaises(T.TransferError):
            self.transfer(stype="OWNERSHIP", sid=999)
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(stype="OWNERSHIP", sid=TR.TC.ALLIANCE)
        self.assertIn("host", str(cx.exception))

    def test_needs_shared_offshore_mode(self):
        # a fresh bank where shared mode was never switched on has no ownership accounts to draw from
        with self.db.tx() as c:
            L.set_state(c, "offshore_shared", "0")
        with self.assertRaises(T.TransferError) as cx:
            self.transfer(stype="OWNERSHIP", sid=self.X)
        self.assertIn("not switched on", str(cx.exception))


class TestImmutability(TBase):
    def test_journal_can_never_be_edited_or_deleted(self):
        self.transfer()
        with self.db.tx() as c:
            for sql in ("UPDATE balance_transfers SET reason='x'", "DELETE FROM balance_transfers",
                        "UPDATE balance_transfer_lines SET delta=1", "DELETE FROM balance_transfer_lines"):
                with self.assertRaises(sqlite3.DatabaseError):
                    c.execute(sql)

    def test_tampering_with_one_side_is_detected_by_reconciliation(self):
        self.transfer()
        # make the journal not balance by adding a stray line (new rows are allowed; edits are not) - verify must catch it
        with self.db.tx() as c:
            c.execute("INSERT INTO balance_transfer_lines(transfer_id,account_type,account_id,resource,delta) VALUES(1,'ALLIANCE',NULL,'money',5)")
        with self.db.read() as c:
            self.assertTrue(T.verify(c))
            out = R.check_transfers(c)
        self.assertEqual(out[0]["kind"], "TRANSFER_JOURNAL_BROKEN")


class TestReconciliationGate(TBase):
    def raise_event(self, kind, severity="RECON", nation_id=None):
        with self.db.tx() as c:
            L.raise_event(c, severity, kind, nation_id=nation_id, details={"message": "test"}, dedupe_key=f"t:{kind}")

    def blocked(self, op="withdraw", nation=1):
        with self.db.read() as c:
            try:
                L.assert_can_mutate(c, nation, op)
                return False
            except L.FinancialBlocked:
                return True

    def test_default_is_unchanged_open_deficit_blocks(self):
        self.raise_event("OFFSHORE_OWNERSHIP_SHORT")
        self.assertTrue(self.blocked())
        with self.db.read() as c:
            self.assertEqual(L.integrity_state(c)["state"], "RECONCILIATION_REQUIRED")

    def test_gate_off_lets_banking_continue_but_keeps_the_finding_open(self):
        self.raise_event("OFFSHORE_OWNERSHIP_SHORT")
        self.raise_event("RESOURCE_SHORTFALL")
        with self.db.tx() as c:
            cfg_set(c, "reconcile_gate_enabled", "0", "9")
        for op in ("withdraw", "convert", "lock", "import"):
            self.assertFalse(self.blocked(op), op)
        with self.db.read() as c:
            st = L.integrity_state(c)
            self.assertEqual(st["state"], "DISCREPANCY_NOT_BLOCKING")
            self.assertEqual(st["open_events"], 2)                      # still detected and reported
            self.assertEqual(st["blocking_events"], 0)

    def test_gate_off_never_unblocks_safety_findings(self):
        with self.db.tx() as c:
            cfg_set(c, "reconcile_gate_enabled", "0", "9")
        self.raise_event("TX_UNCERTAIN", nation_id=1)                   # a possibly-sent PnW transfer: duplicate protection
        self.assertTrue(self.blocked("withdraw", 1))
        self.assertFalse(self.blocked("withdraw", 2))                   # only that nation, as before
        self.raise_event("BALANCE_MISMATCH")
        self.assertTrue(self.blocked("withdraw", 2))

    def test_gate_off_never_unblocks_the_emergency_lock_or_pause(self):
        with self.db.tx() as c:
            cfg_set(c, "reconcile_gate_enabled", "0", "9")
            L.set_state(c, "bank_paused", "1")
        self.assertTrue(self.blocked("withdraw"))
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
            L.set_state(c, "emergency_lock", "1")
        self.assertTrue(self.blocked("adjust"))

    def test_in_game_deposit_is_blocked_by_the_gate_only_while_it_is_on(self):
        self.raise_event("OFFSHORE_OWNERSHIP_SHORT")
        start = lambda: self.run_async(self.svc.deposits.start(nation_id=1, discord_id=self.alice.id, amounts={"money": 100}, idem="dep-x"))  # noqa: E731
        got = start()
        self.assertEqual(got["status"], "BLOCKED")
        self.assertIn("RECONCILIATION REQUIRED", got["message"])
        with self.db.tx() as c:
            cfg_set(c, "reconcile_gate_enabled", "0", "9")
        got = self.run_async(self.svc.deposits.start(nation_id=1, discord_id=self.alice.id, amounts={"money": 100}, idem="dep-y"))
        self.assertNotEqual(got["status"], "BLOCKED")                    # now it reaches the credential / PnW stage

    def test_admin_command_toggles_with_confirmation_and_audit(self):
        from discord import app_commands as _ac  # noqa: F401
        i = self.call(self.bankset, "gate", self.admin, type("C", (), {"value": "off"})(), auto_confirm=False)
        self.assertIn("ancel", i.text())
        with self.db.read() as c:
            self.assertTrue(L.reconcile_gate_enabled(c))
        i = self.call(self.bankset, "gate", self.admin, type("C", (), {"value": "off"})())
        self.assertIn("OFF", i.text())
        with self.db.read() as c:
            self.assertFalse(L.reconcile_gate_enabled(c))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='RECONCILE_GATE_CHANGED'").fetchone()[0], 1)
        i = self.call(self.bankset, "gate", self.alice, type("C", (), {"value": "on"})())
        self.assertIn("Admin", i.text())
        with self.db.read() as c:
            self.assertFalse(L.reconcile_gate_enabled(c))


if __name__ == "__main__":
    unittest.main()
