"""/offshore wipe: clear every alliance's recorded share of the shared offshore so it can be assigned again."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402

from tunbank import cmds_shared_offshore, reconcile as R  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import offshore_ledger as OL  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402

Choice = TC.discord.app_commands.Choice
ALL, OTHERS = Choice("all", "all"), Choice("others", "others")
PHRASE = OL.WIPE_PHRASE
RN, OTHER = 555, 777


class Base(TC.OffshoreCmd):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})
        self.off_grp = TC.app_commands.Group("offshore")
        cmds_shared_offshore.register(self.off_grp, self.svc)
        self.physical = {"money": 1000000000, "steel": 50000000}
        with self.db.tx() as c:
            OL.enable_shared(c, actor="9", host_alliance_id=TC.ALLIANCE, host_name="TUN")
            OL.register(c, alliance_id=RN, name="RN", actor="9")
            OL.register(c, alliance_id=OTHER, name="Others", actor="9")
            OL.assign(c, alliance_id=OL.host_id(c), amounts={"money": 200000000}, physical=self.physical, actor="9", note="TUN share")
            OL.assign(c, alliance_id=RN, amounts={"money": 500000000, "steel": 20000000}, physical=self.physical, actor="9", note="RN share")
            OL.assign(c, alliance_id=OTHER, amounts={"steel": 10000000}, physical=self.physical, actor="9", note="Others share")

    def wipe(self, who=None, scope=ALL, phrase=PHRASE, reason="starting over", **kw):
        return self.call(self.off_grp, "wipe", who or self.admin, scope, reason, phrase, **kw)

    def shares(self):
        with self.db.read() as c:
            return {a["alliance_id"]: OL.balances(c, a["alliance_id"]) for a in OL.alliances(c, active_only=False)}

    def entry_count(self):
        with self.db.read() as c:
            return c.execute("SELECT COUNT(*) FROM offshore_entries").fetchone()[0]


for _n in dir(TC.OffshoreCmd):
    if _n.startswith("test_"):
        setattr(Base, _n, None)
del _n


class TestWipe(Base):
    def test_every_share_is_cleared_without_deleting_or_rewriting_any_history(self):
        host = TC.ALLIANCE
        before_entries = self.entry_count()
        with self.db.read() as c:
            old_rows = [tuple(r) for r in c.execute("SELECT * FROM offshore_entries ORDER BY id")]
            member_before = L.get_balances(c, 1, "AVAILABLE")
        i = self.wipe()
        self.assertIn("Offshore assignments cleared", i.text())
        self.assertEqual(self.shares(), {host: {}, RN: {}, OTHER: {}})
        with self.db.read() as c:
            rows = [tuple(r) for r in c.execute("SELECT * FROM offshore_entries ORDER BY id")]
            self.assertEqual(rows[:before_entries], old_rows)                                    # history is byte-for-byte unchanged
            self.assertGreater(len(rows), before_entries)                                        # reversing entries were ADDED
            wipe_rows = rows[before_entries:]
            self.assertEqual({r[3] for r in wipe_rows}, {host, RN, OTHER})                       # col 3 = alliance_id
            self.assertEqual(len({r[2] for r in wipe_rows}), 1)                                  # one group = one identifiable event
            self.assertTrue(all(r[6] == "ADJUSTMENT" and r[5] < 0 for r in wipe_rows))           # col 5 = delta, col 6 = entry type
            self.assertTrue(OL.verify_chain(c)["ok"])
            self.assertTrue(OL.cache_matches(c))
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), member_before)                   # member balances are never touched
            a = c.execute("SELECT details_json FROM audit_log WHERE action='OFFSHORE_ASSIGNMENTS_WIPED'").fetchone()
            self.assertIn("RN", a["details_json"])                                               # the previous amounts are recoverable from the audit log
        self.assertEqual(self.pnw.withdraw_calls, 0)                                             # nothing was sent anywhere

    def test_the_previous_assignments_are_attached_so_you_can_assign_them_again(self):
        i = self.wipe()
        files = [m["file"] for m in i.sent if m.get("file") is not None]
        self.assertTrue(files)
        body = files[0].fp.getvalue().decode()
        for must in ("RN [#555]", "TUN", "Others [#777]", "Steel"):
            self.assertIn(must, body)

    def test_a_safety_backup_is_taken_first(self):
        self.wipe()
        self.assertTrue(list(self.settings.backup_dir.glob("pre-offshore-wipe-*.db")))

    def test_scope_others_keeps_this_alliances_own_share(self):
        self.wipe(scope=OTHERS)
        sh = self.shares()
        self.assertEqual(sh[TC.ALLIANCE], {"money": 200000000})
        self.assertEqual((sh[RN], sh[OTHER]), ({}, {}))

    def test_wrong_phrase_non_admin_and_cancel_change_nothing(self):
        before = self.shares()
        self.assertIn("Nothing was changed", self.wipe(phrase="yes").text())
        self.assertIn("Admin", self.wipe(who=self.alice).text())
        self.wipe(auto_confirm=False)
        self.assertEqual(self.shares(), before)
        self.assertIn("reason", self.wipe(reason=" ").text().lower())

    def test_it_refuses_while_a_withdrawal_is_in_flight_and_changes_nothing(self):
        with self.db.tx() as c:                                                                  # an ECON withdrawal that has reserved funds
            L.begin_withdrawal(c, tx_type="WITHDRAW_ECON", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None,
                               dest_nation_id=1, amounts={"money": 10000}, actor="9", note="n", reason="r", idempotency_key="inflight-1")
            c.execute("UPDATE transactions SET status='CONFIRMED' WHERE idempotency_key='inflight-1'")
        before = self.shares()
        i = self.wipe()
        self.assertIn("in flight", i.text())
        self.assertEqual(self.shares(), before)

    def test_the_emergency_lock_blocks_it(self):
        with self.db.tx() as c:
            L.set_state(c, "emergency_lock", "1")
        before = self.shares()
        self.wipe()
        self.assertEqual(self.shares(), before)

    def test_nothing_to_clear_and_not_in_shared_mode(self):
        self.wipe()
        self.assertIn("nothing to clear", self.wipe().text().lower())
        with self.db.tx() as c:
            L.set_state(c, "offshore_shared", "0")
        self.assertIn("not in shared mode", self.wipe().text())

    def test_after_a_wipe_the_funds_are_unassigned_not_a_reconciliation_problem_and_can_be_assigned_again(self):
        self.wipe()
        with self.db.read() as c:
            events = R.check_offshore(c, {"offshore": self.physical})
            self.assertEqual([e for e in events if e["kind"] in ("OFFSHORE_SHORTFALL", "OFFSHORE_NET_SHORTFALL")], [])
            un = OL.unassigned(self.physical, OL.totals(c))
        self.assertEqual(un, self.physical)                                                       # the whole bank shows as unassigned
        with self.db.tx() as c:
            OL.assign(c, alliance_id=RN, amounts={"money": 400000000}, physical=self.physical, actor="9", note="start over")
        self.assertEqual(self.shares()[RN], {"money": 400000000})

    def test_a_second_run_is_safe_and_the_wipe_is_visible_in_the_ledger_history(self):
        self.wipe()
        with self.db.read() as c:
            notes = [r["note"] for r in c.execute("SELECT note FROM offshore_entries WHERE note LIKE 'ASSIGNMENT WIPE%'")]
        self.assertTrue(notes and all("starting over" in n for n in notes))

    def test_help_lists_it_and_it_needs_admin(self):
        from tunbank.cmds_help import CATALOG
        self.assertEqual(CATALOG["offshore wipe"], ("shared", "ADMIN"))


if __name__ == "__main__":
    unittest.main()
