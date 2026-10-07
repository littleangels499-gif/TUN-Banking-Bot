"""Resource conversion inside the member's bank balance (ledger only, nothing in-game)."""
import sqlite3
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_reset_restore as TR  # noqa: E402

from tunbank import conversion as CV  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank import reconcile as R  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402
from tunbank.valuation import Snapshot  # noqa: E402

PR = {r: Decimal("1.00") for r in M.NON_CASH}
PR.update(food=Decimal("2.00"), steel=Decimal("5.00"), coal=Decimal("3.00"))
SNAP = Snapshot(11, "2026-10-07T00:00:00Z", PR)


class TestQuote(unittest.TestCase):
    def test_formula_value_divided_by_receiving_price(self):
        q = CV.make_quote(SNAP, "food", "steel", 100000)             # 1,000 food @2.00 = $2,000 -> steel @5.00
        self.assertEqual((q.value_cents, q.to_units), (200000, 40000))   # 400.00 steel

    def test_too_small_is_refused(self):
        with self.assertRaises(CV.ConversionError):
            CV.make_quote(SNAP, "food", "coal", 1)

    def test_rounding_example(self):
        q = CV.make_quote(SNAP, "food", "coal", 10)                  # 20c / 3.00 = 6.66 -> 6 (floor)
        self.assertEqual(q.to_units, 6)
        self.assertLessEqual(q.to_units * q.to_price, Decimal(q.from_units) * q.from_price)

    def test_cash_works_both_ways(self):
        q = CV.make_quote(SNAP, "money", "steel", 500000)           # $5,000.00 -> 1000.00 steel
        self.assertEqual(q.to_units, 100000)
        q = CV.make_quote(SNAP, "steel", "money", 100000)           # 1000 steel = $5,000.00
        self.assertEqual(q.to_units, 500000)

    def test_same_resource_zero_and_unknown(self):
        for args in (("food", "food", 5), ("food", "steel", 0), ("food", "gold", 5), ("food", "steel", -5)):
            with self.assertRaises(CV.ConversionError):
                CV.make_quote(SNAP, *args)

    def test_missing_stale_or_suspicious_prices_refuse(self):
        no_steel = Snapshot(1, "t", {r: v for r, v in PR.items() if r != "steel"})
        for snap in (None, no_steel, Snapshot(1, "t", PR, stale=True), Snapshot(1, "t", PR, suspicious=True)):
            with self.assertRaises(CV.ConversionError):
                CV.make_quote(snap, "food", "steel", 100000)


class ConvBase(TR.ResetBase):
    BAL = "nation_name,food,steel,coal\nAlpha,1000,0,0\n"

    def setUp(self):
        super().setUp()

        async def prices():
            return dict(PR)
        self.pnw.fetch_prices = prices
        self.fund_alice({"money": 100})
        self.pause()
        self.reset()
        self.restore(self.BAL)
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
        # the bank physically holds Alpha's food + 10,000 steel + 5,000 coal that belong to the alliance
        self.pnw.holdings = {"food": 100000, "steel": 1000000, "coal": 500000}
        self.holdings = dict(self.pnw.holdings)

    def convert(self, from_res="food", to_res="steel", units=100000, key=None, holdings="default", nid=1, snap=SNAP):
        q = CV.make_quote(snap, from_res, to_res, units)
        key = key or f"k-{from_res}-{to_res}-{units}"
        h = self.holdings if holdings == "default" else holdings
        with self.db.tx() as c:
            return CV.execute(c, nation_id=nid, quote=q, actor="111", idempotency_key=key, holdings=h)

    def assert_unchanged(self, expected):
        self.assertEqual(self.bal(1), expected)
        self.assertEqual(self.count("conversions"), 0)


class TestConversion(ConvBase):
    def test_converts_at_market_value_and_records_everything(self):
        r = self.convert()
        self.assertEqual(r["to_units"], 40000)
        self.assertEqual(self.bal(1), {"steel": 40000})
        with self.db.read() as c:
            row = c.execute("SELECT * FROM conversions").fetchone()
            self.assertEqual((row["from_resource"], row["from_units"], row["to_resource"], row["to_units"], row["value_cents"],
                              row["price_snapshot_id"], row["actor"]), ("food", 100000, "steel", 40000, 200000, 11, "111"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger_entries WHERE entry_type='CONVERSION'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='CONVERSION'").fetchone()[0], 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])

    def test_bank_stays_honest_after_conversion(self):
        self.convert()
        with self.db.tx() as c:
            res = R.run_checks(c, holdings=self.holdings, snapshot_id=None, triggered_by="t")
        self.assertFalse([f for f in res["findings"] if f["severity"] in ("CRITICAL", "RECON")], res["findings"])
        with self.db.read() as c:
            pos = R.bank_position(c, self.holdings)
        self.assertEqual(pos["alliance_owned"]["food"], 100000)          # the member's food is now alliance-owned
        self.assertEqual(pos["alliance_owned"]["steel"], 1000000 - 40000)

    def test_cannot_receive_what_the_alliance_does_not_own(self):
        self.holdings["steel"] = 100                                     # alliance owns only 1.00 steel
        with self.assertRaises(CV.AllianceStockShort) as cm:
            self.convert()
        self.assertNotIn("steel", str(cm.exception).lower())              # members learn nothing about the treasury
        self.assertIn("alliance only owns", cm.exception.internal)        # ECON gets the detail
        self.assert_unchanged({"food": 100000})

    def test_unreadable_bank_refuses(self):
        with self.assertRaises(CV.ConversionError):
            self.convert(holdings=None)
        self.assert_unchanged({"food": 100000})

    def test_cannot_convert_more_than_available(self):
        with self.assertRaises(L.InsufficientFunds):
            self.convert(units=100001)
        self.assert_unchanged({"food": 100000})

    def test_locked_funds_cannot_be_converted(self):
        with self.db.tx() as c:
            L.create_lock(c, nation_id=1, amounts={"food": 60000}, lock_type="X", reason="r", actor="9")
        with self.assertRaises(L.InsufficientFunds) as cm:
            self.convert(units=60000)
        self.assertIn("Locked", str(cm.exception))
        self.convert(units=40000)                                          # the free part is fine

    def test_pending_withdrawal_reduces_what_can_be_converted(self):
        with self.db.tx() as c:
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1,
                               lock_id=None, dest_nation_id=1, amounts={"food": 70000}, actor="111", note="n",
                               reason="r", idempotency_key="h1")
        with self.assertRaises(L.InsufficientFunds):
            self.convert(units=40000)

    def test_a_negative_balance_is_never_converted_as_if_positive(self):
        with self.db.tx() as c:                                          # Alpha now owes 20 coal
            L.apply_adjustment(c, nation_id=1, deltas={"coal": -2000}, reason="r", evidence="e", actor="9",
                               approval_id=self._approve(c))
        self.assertEqual(self.bal(1)["coal"], -2000)
        with self.assertRaises(L.InsufficientFunds):
            self.convert("coal", "steel", 1000)

    def _approve(self, c):
        aid = L.create_approval(c, "ADJUSTMENT", {"x": 1}, "8", "r")
        c.execute("UPDATE approval_requests SET approved_by='9' WHERE id=?", (aid,))
        return aid

    def test_receiving_a_resource_you_owe_pays_the_debt_without_needing_alliance_stock(self):
        with self.db.tx() as c:
            L.apply_adjustment(c, nation_id=1, deltas={"steel": -1000}, reason="r", evidence="e", actor="9",
                               approval_id=self._approve(c))
        self.assertEqual(self.bal(1)["steel"], -1000)
        self.holdings["steel"] = 0                                        # the alliance owns no steel at all
        self.convert(units=2000)                                          # 20 food = $40 -> 8.00 steel
        self.assertEqual(self.bal(1)["steel"], -200)                      # debt reduced, still not positive
        self.convert(units=500, key="rest")                               # exactly clears the rest (200 steel)
        self.assertNotIn("steel", self.bal(1))
        with self.assertRaises(CV.AllianceStockShort):                   # but going POSITIVE needs real alliance steel
            self.convert(units=500, key="over")

    def test_idempotent_replay_does_not_convert_twice(self):
        self.convert(units=10000, key="same")
        again = self.convert(units=10000, key="same")
        self.assertTrue(again["replay"])
        self.assertEqual(self.bal(1)["food"], 90000)
        self.assertEqual(self.count("conversions"), 1)

    def test_switched_off_paused_frozen_emergency_and_cap(self):
        with self.db.tx() as c:
            cfg_set(c, "conversion_enabled", "0", "9")
        with self.assertRaises(CV.ConversionError):
            self.convert()
        with self.db.tx() as c:
            cfg_set(c, "conversion_enabled", "1", "9")
            L.set_state(c, "bank_paused", "1")
        with self.assertRaises(L.FinancialBlocked):
            self.convert()
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
            c.execute("UPDATE members SET frozen=1, frozen_reason='review' WHERE nation_id=1")
        with self.assertRaises(CV.ConversionError) as cm:
            self.convert()
        self.assertIn("frozen", str(cm.exception))
        with self.db.tx() as c:
            c.execute("UPDATE members SET frozen=0 WHERE nation_id=1")
            cfg_set(c, "conversion_max_value", "1000", "9")
        with self.assertRaises(CV.ConversionError) as cm:
            self.convert()                                                # $2,000 > $1,000 cap
        self.assertIn("limited", str(cm.exception))
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "test", "9")
        with self.assertRaises(L.FinancialBlocked):
            self.convert(units=100)
        self.assert_unchanged({"food": 100000})

    def test_ledger_refuses_a_forged_conversion_entry(self):
        for entry in (dict(nation_id=1, resource="steel", delta=999, conversion_id=1),
                      dict(nation_id=1, resource="steel", delta=5, conversion_id=None)):
            with self.assertRaises(sqlite3.Error):
                with self.db.tx() as c:
                    L.post_entries(c, [dict(group_id="g", bucket="AVAILABLE", entry_type="CONVERSION", actor="x", **entry)])

    def test_conversion_records_are_permanent(self):
        self.convert()
        for sql in ("UPDATE conversions SET to_units=999999", "DELETE FROM conversions",
                    "DELETE FROM ledger_entries WHERE entry_type='CONVERSION'"):
            with self.assertRaises(sqlite3.Error, msg=sql):
                with self.db.tx() as c:
                    c.execute(sql)

    def test_convertible_and_parse_amount(self):
        with self.db.read() as c:
            self.assertEqual(CV.convertible(c, 1), {"food": 100000})
        self.assertEqual(CV.parse_amount("all", "food", 100000), 100000)
        self.assertEqual(CV.parse_amount("90m", "food", 0), 9000000000)
        self.assertEqual(CV.parse_amount("1,000", "food", 0), 100000)
        with self.assertRaises(CV.ConversionError):
            CV.parse_amount("lots", "food", 0)


class FakeChannel:
    id = 555
    mention = "<#555>"

    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class TestPanel(ConvBase):
    def setUp(self):
        super().setUp()
        from tunbank import cmds_convert
        self.cc = cmds_convert
        cmds_convert.register(self.bankset, self.svc)
        self.chan = FakeChannel()

    def open_converter(self, user=None):
        user = user or self.alice
        view = self.chan.sent[-1]["view"]
        button = view.children[-1]
        i = TR.discord.User and TC_FI(user)
        self.run_async(button.callback(i))
        return i

    def pick(self, conv, from_res, to_res):
        for sel, val, handler in ((conv.from_select, from_res, conv._picked_from), (conv.to_select, to_res, conv._picked_to)):
            sel.values = [val]
            self.run_async(handler(TC_FI(self.alice)))

    def amount_modal(self, conv, text, auto_confirm=True):
        i = TC_FI(self.alice, auto_confirm=auto_confirm)
        self.run_async(conv.amount_button.callback(i))
        modal = i.modal
        modal.amount.value = text
        self.run_async(modal.on_submit(i))
        return i

    def post(self):
        self.call(self.bankset, "conversionpanel", self.admin, channel=self.chan)

    def test_only_admin_can_post_the_panel(self):
        i = self.call(self.bankset, "conversionpanel", self.alice, channel=self.chan)
        self.assertIn("Admin", i.text())
        self.assertEqual(self.chan.sent, [])
        self.post()
        self.assertEqual(len(self.chan.sent), 1)
        self.assertEqual(self.chan.sent[0]["view"].children[-1].custom_id, "tunbank:convert:open")   # survives restarts
        self.assertIsNone(self.chan.sent[0]["view"].timeout)

    def test_full_flow_converts(self):
        self.post()
        i = self.open_converter()
        conv = i.sent[-1]["view"]
        self.assertEqual([o.value for o in conv.from_select.options], ["food"])      # only what she can convert
        self.pick(conv, "food", "steel")
        j = self.amount_modal(conv, "all")
        text = j.text()
        for want in ("RESOURCE CONVERSION", "You are converting", "Market value", "Price snapshot", "Conversion complete"):
            self.assertIn(want, text)
        self.assertEqual(self.bal(1), {"steel": 40000})
        self.assertEqual(self.pnw.withdraw_calls, 0)                                  # nothing happened in-game

    def test_cancel_changes_nothing(self):
        self.post()
        conv = self.open_converter().sent[-1]["view"]
        self.pick(conv, "food", "steel")
        j = self.amount_modal(conv, "all", auto_confirm=False)
        self.assertIn("cancelled", j.text().lower())
        self.assertEqual(self.bal(1), {"food": 100000})
        self.assertEqual(self.count("conversions"), 0)

    def test_amount_too_large_and_same_resource_and_bad_text(self):
        self.post()
        conv = self.open_converter().sent[-1]["view"]
        self.pick(conv, "food", "steel")
        self.assertIn("only have", self.amount_modal(conv, "9999999").text())
        self.assertIn("couldn't read", self.amount_modal(conv, "lots").text())
        self.pick(conv, "food", "food")
        i = TC_FI(self.alice)
        self.run_async(conv.amount_button.callback(i))
        self.assertIn("two different", i.text())
        self.assertEqual(self.count("conversions"), 0)

    def test_price_move_between_quote_and_confirm_converts_nothing(self):
        self.post()
        conv = self.open_converter().sent[-1]["view"]
        self.pick(conv, "food", "steel")
        calls = {"n": 0}
        real = self.svc.prices.get

        async def moving(force=False):
            calls["n"] += 1
            snap = await real(force)
            if calls["n"] >= 2:                                                       # the second look: steel is dearer
                p = dict(snap.prices)
                p["steel"] = Decimal("9.00")
                return Snapshot(snap.id + 1, snap.fetched_at, p)
            return snap
        self.svc.prices.get = moving
        j = self.amount_modal(conv, "all")
        self.assertIn("Prices changed", j.text())
        self.assertEqual(self.count("conversions"), 0)

    def test_alliance_without_the_stock_gets_a_generic_refusal_and_econ_is_told(self):
        self.pnw.holdings = {"food": 100000, "steel": 10}                              # the alliance owns almost no steel
        self.post()
        conv = self.open_converter().sent[-1]["view"]
        self.pick(conv, "food", "steel")
        j = self.amount_modal(conv, "all")
        self.assertIn("can't be processed right now", j.text())
        self.assertNotIn("alliance only owns", j.text())
        self.assertEqual(self.bal(1), {"food": 100000})

    def test_unlinked_user_and_other_peoples_panels(self):
        self.post()
        i = TC_FI(TR.discord.User(777, "stranger"))
        self.run_async(self.chan.sent[-1]["view"].children[-1].callback(i))
        self.assertIn("/nation link", i.text())
        conv = self.open_converter().sent[-1]["view"]
        i = TC_FI(TR.discord.User(777, "stranger"))
        conv.from_select.values = ["food"]
        self.run_async(conv._picked_from(i))
        self.assertIn("belongs to someone else", i.text())
        self.assertIsNone(conv.from_res)

    def test_switched_off_and_nothing_to_convert(self):
        self.post()
        with self.db.tx() as c:
            cfg_set(c, "conversion_enabled", "0", "9")
        self.assertIn("switched off", self.open_converter().text())
        with self.db.tx() as c:
            cfg_set(c, "conversion_enabled", "1", "9")
            L.create_lock(c, nation_id=1, amounts={"food": 100000}, lock_type="X", reason="r", actor="9")
        self.assertIn("nothing available", self.open_converter().text())

    def test_the_bot_registers_the_panel_view_and_help_lists_the_command(self):
        from tunbank import cmds_help
        self.assertIn("bankset conversionpanel", cmds_help.CATALOG)


import test_commands as _TC  # noqa: E402
TC_FI = _TC.FI


if __name__ == "__main__":
    unittest.main()
