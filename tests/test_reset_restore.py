"""Tests for: /deposit reset, the Locutus restoration spreadsheet (names, negatives, loans), negative balances.

Run with:  python -m unittest discover -s tests -v
"""
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
from test_core import PRICES  # noqa: E402

discord = TC.discord
from tunbank import cmds_deposit  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402
from tunbank import db as DBMOD  # noqa: E402
from tunbank import deposit_reset as DR  # noqa: E402
from tunbank import exports as X  # noqa: E402
from tunbank import importer  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank import reconcile as R  # noqa: E402
from tunbank.valuation import Snapshot, value_amounts  # noqa: E402

SNAP = Snapshot(7, "2026-10-01T00:00:00Z", PRICES)         # every resource = $1.00 per unit
MEMBERS = {713016: "CITADEL OF REALITY", 1: "Alpha", 2: "Beta", 3: "Gamma"}


def pv(csv_text, members=MEMBERS, mode="RESTORE", name="r.csv"):
    return importer.preview(name, csv_text.encode(), members=members, snapshot=SNAP, mode=mode)


class TestRestoreSpreadsheet(unittest.TestCase):
    """The importer's reading/validation rules. Nothing here touches a database (a preview writes nothing)."""

    def test_nation_id_and_name_import(self):
        p = pv("nation_id,nation_name,money,food,steel\n713016,CITADEL OF REALITY,5000000,1000000,50000\n")
        self.assertFalse(p.blocked, p.errors)
        self.assertEqual(p.nations[713016], {"money": 500000000, "food": 100000000, "steel": 5000000})

    def test_name_only_import_is_case_insensitive(self):
        p = pv("nation_name,money,food,steel\ncitadel  of REALITY,5000000,1000000,50000\n")
        self.assertFalse(p.blocked, p.errors)
        self.assertEqual(list(p.nations), [713016])
        self.assertEqual(p.used_names, 1)

    def test_id_only_still_works(self):
        p = pv("nation_id,money\n1,100\n")
        self.assertFalse(p.blocked, p.errors)

    def test_id_and_name_disagree_blocks_row(self):
        p = pv("nation_id,nation_name,money\n1,Beta,100\n2,Beta,50\n")
        self.assertTrue(p.blocked)
        self.assertEqual(len(p.errors), 1)
        self.assertTrue("disagree" in p.errors[0] and "Row 2" in p.errors[0])
        self.assertEqual(list(p.nations), [2])                              # row 3 (id 2 + Beta) is fine

    def test_unknown_name_blocks_and_nothing_is_guessed(self):
        p = pv("nation_name,money\nAlphaa,100\n")
        self.assertTrue(p.blocked)
        self.assertIn("not found", p.errors[0])
        self.assertEqual(p.rows, [])

    def test_ambiguous_name_blocks(self):
        p = pv("nation_name,money\nTwin,100\n", members={1: "Twin", 2: "twin", 3: "Other"})
        self.assertTrue(p.blocked)
        self.assertTrue(any("ambiguous" in e for e in p.errors))

    def test_negative_resources_are_valid_and_never_zeroed(self):
        p = pv("nation_name,money,food,steel\nAlpha,5000000,1000000,-30000\n")
        self.assertFalse(p.blocked, p.errors)
        self.assertEqual(p.nations[1]["steel"], -3000000)
        self.assertEqual(p.negative_totals, {"steel": 3000000})
        self.assertEqual(p.totals["steel"], -3000000)

    def test_accounting_style_negative(self):
        p = pv("nation_name,steel\nAlpha,(30000)\n")
        self.assertEqual(p.nations[1]["steel"], -3000000)

    def test_negative_is_blocked_for_a_plain_opening_import(self):
        p = pv("nation_name,steel\nAlpha,-5\n", mode="OPENING")
        self.assertTrue(p.blocked)
        self.assertIn("negative", p.errors[0])

    def test_loan_column_and_aliases_are_loans_not_deposits(self):
        for alias in ("loan", "outstanding_loan", "loan_balance"):
            p = pv(f"nation_name,money,food,steel,{alias}\nAlpha,5000000,1000000,-30000,25000000\n")
            self.assertFalse(p.blocked, (alias, p.errors))
            self.assertEqual(p.loans, {1: 2500000000})
            self.assertEqual(p.loan_total_cents, 2500000000)
            self.assertNotIn("loan", p.totals)                 # never a deposit
            self.assertEqual(p.nations[1]["money"], 500000000)

    def test_invalid_loan_values(self):
        p = pv("nation_name,loan\nAlpha,-5\nBeta,abc\n")
        self.assertTrue(p.blocked)
        self.assertEqual(len([e for e in p.errors if "loan" in e]), 2)

    def test_duplicate_rows_and_duplicate_nations(self):
        p = pv("nation_name,money\nAlpha,1\nAlpha,1\n")
        self.assertTrue(any("duplicate" in e for e in p.errors))
        p = pv("nation_id,nation_name,money\n1,,5\n,alpha,6\n")           # same nation by id and by name
        self.assertTrue(p.blocked)
        self.assertTrue(any("duplicate" in e for e in p.errors))

    def test_invalid_values(self):
        p = pv("nation_name,money,coal\nAlpha,abc,1.234\nBeta,1e999,2\nGamma,TRUE,1\n")
        text = " ".join(p.errors)
        self.assertTrue(p.blocked)
        self.assertIn("not a valid number", text)
        self.assertIn("2 decimal", text)

    def test_nation_not_in_alliance_blocks(self):
        p = pv("nation_id,money\n999,5\n")
        self.assertTrue(p.blocked)
        self.assertEqual(p.unknown_nations, [999])

    def test_preview_summary_numbers(self):
        p = pv("nation_name,money,food,steel,loan\nAlpha,100,50,-30,1000\nBeta,10,0,-5,200\n")
        self.assertEqual(len(p.nations), 2)
        self.assertEqual(p.positive_totals, {"money": 11000, "food": 5000})
        self.assertEqual(p.negative_totals, {"steel": 3500})
        self.assertEqual(p.loan_total_cents, 120000)
        self.assertEqual(p.valuation_positive.total_cents, 11000 + 5000 * 100)   # cash is 1:1, food = 100c/unit
        self.assertEqual(p.valuation_negative.total_cents, 3500 * 100)
        self.assertEqual(p.valuation.total_cents, 11000 + 500000 - 350000)        # net = positives - negatives
        self.assertTrue(any("Total outstanding loans" in n for n, _, _ in cmds_deposit.import_preview_card(p, "x").fields))

    def test_xlsx_with_names_negatives_and_loans(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Nation", "money", "steel", "Loan Balance"])
        ws.append(["alpha", 5000000, -30000, 25000000])
        buf = io.BytesIO()
        wb.save(buf)
        p = importer.preview("l.xlsx", buf.getvalue(), members=MEMBERS, snapshot=SNAP, mode="RESTORE")
        self.assertFalse(p.blocked, p.errors)
        self.assertEqual(p.nations[1]["steel"], -3000000)
        self.assertEqual(p.loans, {1: 2500000000})

    def test_members_unavailable_blocks(self):
        p = pv("nation_name,money\nAlpha,1\n", members=None)
        self.assertTrue(p.blocked)


class ResetBase(unittest.TestCase):
    """Real command code + real database, fake Discord and fake PnW."""
    make_settings = TC.Cmd.make_settings
    run_async = TC.Cmd.run_async
    call = TC.Cmd.call
    fund_alice = TC.Cmd.fund_alice

    def setUp(self):
        TC.Cmd.setUp(self)
        from discord import app_commands
        self.pnw.members = dict(MEMBERS)
        self.deposit = app_commands.Group("deposit")
        cmds_deposit.register(self.deposit, self.svc)

    tearDown = TC.Cmd.tearDown

    def count(self, table, where="1=1"):
        with self.db.read() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def bal(self, nid, bucket="AVAILABLE"):
        with self.db.read() as c:
            return L.get_balances(c, nid, bucket)

    def pause(self):
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "1")

    def reset(self, user=None, reason="Migrating to the Locutus verified balances", phrase=DR.RESET_PHRASE, **kw):
        return self.call(self.deposit, "reset", user or self.admin, reason, phrase, **kw)

    def restore(self, csv_text, name="locutus.csv", **kw):
        return self.call(self.bankset, "importopening", self.admin, discord.Attachment(name, csv_text.encode()), "locutus export", **kw)


class TestDepositReset(ResetBase):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000000, "coal": 2000})
        self.pause()

    def test_unauthorized_user_is_rejected(self):
        before = self.bal(1)
        i = self.reset(user=self.alice)
        self.assertIn("Admin", i.text())
        self.assertEqual(self.bal(1), before)
        self.assertEqual(self.count("deposit_resets"), 0)

    def test_wrong_confirm_phrase_changes_nothing(self):
        i = self.reset(phrase="yes")
        self.assertIn(DR.RESET_PHRASE, i.text())
        self.assertEqual(self.count("deposit_resets"), 0)
        self.assertTrue(self.bal(1))

    def test_confirmation_button_is_required(self):
        i = self.reset(auto_confirm=False)
        self.assertIn("cancelled", i.text().lower())
        self.assertEqual(self.count("deposit_resets"), 0)
        self.assertEqual(self.bal(1)["money"], 100000000)

    def test_withdrawals_must_be_paused_first(self):
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
        i = self.reset()
        self.assertIn("/bank lock", i.text())
        self.assertEqual(self.count("deposit_resets"), 0)

    def test_reset_works_and_writes_a_permanent_audit_record(self):
        with self.db.read() as c:
            ledger_before = c.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
            first_ledger_rows = [tuple(r) for r in c.execute("SELECT * FROM ledger_entries ORDER BY id")]
        pnw_before = self.count("pnw_records")
        audit_before = self.count("audit_log")
        i = self.reset()
        self.assertIn("reset complete", i.text().lower())
        self.assertEqual(self.bal(1), {})
        self.assertEqual(self.bal(1, "LOCKED"), {})
        with self.db.read() as c:
            rs = c.execute("SELECT * FROM deposit_resets").fetchone()
            self.assertEqual(rs["actor"], str(self.admin.id))
            self.assertIn("Locutus", rs["reason"])
            self.assertTrue(rs["created_at"])
            self.assertEqual(rs["value_cents"], 100000000 + 200000 * 100)     # $1M cash + 2000 coal (200000 units x price 100)
            self.assertEqual(json.loads(rs["totals_json"]), {"money": 100000000, "coal": 200000})
            before = json.loads(rs["before_json"])
            self.assertEqual({(ln["resource"], ln["amount"]) for ln in before["lines"]},
                             {("money", 100000000), ("coal", 200000)})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deposit_reset_items").fetchone()[0], 2)
            ents = c.execute("SELECT * FROM ledger_entries WHERE entry_type='RESET'").fetchall()
            self.assertEqual(len(ents), 2)
            self.assertTrue(all(e["reset_id"] == rs["id"] and e["delta"] < 0 for e in ents))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='DEPOSIT_RESET'").fetchone()[0], 1)
            ca = c.execute("SELECT * FROM config_audit WHERE setting='deposit_reset'").fetchone()
            self.assertEqual(ca["actor_id"], str(self.admin.id))
            self.assertEqual(ca["category"], "RESET")
            # history preserved, nothing rewritten
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], ledger_before + 2)
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM ledger_entries ORDER BY id LIMIT ?", (ledger_before,))],
                             first_ledger_rows)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertTrue(L.verify_chain(c, "audit_log")["ok"])
        self.assertEqual(self.count("pnw_records"), pnw_before)
        self.assertGreater(self.count("audit_log"), audit_before)
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**12, "coal": 10**12}, snapshot_id=None, triggered_by="t")
        self.assertFalse([f for f in res["findings"] if f["severity"] == "CRITICAL"], res["findings"])

    def test_reset_records_cannot_be_edited_or_deleted(self):
        self.reset()
        for sql in ("DELETE FROM deposit_resets", "UPDATE deposit_resets SET reason='x'",
                    "DELETE FROM deposit_reset_items", "UPDATE deposit_reset_items SET amount=1",
                    "DELETE FROM ledger_entries WHERE entry_type='RESET'"):
            with self.assertRaises(sqlite3.Error, msg=sql):
                with self.db.tx() as c:
                    c.execute(sql)

    def test_cannot_forge_a_reset_entry(self):
        with self.assertRaises(sqlite3.Error):
            with self.db.tx() as c:
                L.post_entries(c, [dict(group_id="g", nation_id=1, bucket="AVAILABLE", resource="money", delta=-5,
                                        entry_type="RESET", reset_id=99, actor="x")])

    def test_locked_funds_are_cleared_too(self):
        with self.db.tx() as c:
            L.create_lock(c, nation_id=1, amounts={"money": 40000000}, lock_type="WARCHEST", reason="r", actor="9")
        self.reset()
        self.assertEqual(self.bal(1, "LOCKED"), {})
        self.assertEqual(self.bal(1), {})
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT SUM(amount) FROM balances").fetchone()[0], 0)
            self.assertEqual(L.lock_remaining(c, 1), {})

    def test_in_flight_withdrawal_blocks_reset(self):
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
        with self.db.tx() as c:
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1,
                               lock_id=None, dest_nation_id=1, amounts={"money": 100}, actor="111", note="n",
                               reason="r", idempotency_key="k-reset-test")
        self.pause()
        i = self.reset()
        self.assertIn("in flight", i.text())
        self.assertEqual(self.count("deposit_resets"), 0)

    def test_nothing_to_reset(self):
        self.reset()
        self.restore("nation_name,loan\nAlpha,5\n", name="l.csv")
        i = self.reset()
        self.assertEqual(self.count("deposit_resets"), 1)
        self.assertIn("no member balances", i.text())

    def test_second_reset_blocked_until_restore(self):
        self.reset()
        i = self.reset()
        self.assertIn("waiting for its restore", i.text())
        self.assertEqual(self.count("deposit_resets"), 1)

    def test_reset_needs_a_real_reason(self):
        i = self.reset(reason="x")
        self.assertIn("reason", i.text().lower())
        self.assertEqual(self.count("deposit_resets"), 0)

    def test_emergency_lock_blocks_reset(self):
        with self.db.tx() as c:
            L.set_emergency_lock(c, True, "test", "9")
        self.reset()
        self.assertEqual(self.count("deposit_resets"), 0)
        self.assertTrue(self.bal(1))

    def test_help_lists_the_new_commands(self):
        from tunbank import cmds_help
        self.assertIn("deposit reset", cmds_help.CATALOG)
        self.assertNotIn("deposit restore", cmds_help.CATALOG)           # one import command only: /bankset importopening
        self.assertIn("bankset importopening", cmds_help.CATALOG)


class TestRestoreAfterReset(ResetBase):
    LOCUTUS = ("nation_name,money,food,steel,loan\n"
               "CITADEL OF REALITY,5000000,1000000,-30000,25000000\n"
               "beta,1000,0,5000,0\n")

    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000000})
        self.pause()

    def test_without_a_reset_the_same_command_is_a_plain_opening_import_that_refuses_negatives(self):
        i = self.restore(self.LOCUTUS)                       # contains a negative amount
        self.assertIn("BLOCKED", i.text())
        self.assertIn("OPENING", i.text())
        self.assertEqual(self.count("import_batches"), 0)

    def test_restore_requires_admin(self):
        self.reset()
        i = self.call(self.bankset, "importopening", self.alice, discord.Attachment("a.csv", self.LOCUTUS.encode()), "n")
        self.assertIn("Admin", i.text())
        self.assertEqual(self.count("import_batches"), 0)

    def test_preview_blocks_and_changes_nothing_until_confirmed(self):
        self.reset()
        i = self.restore("nation_name,money\nNobody,5\n")
        self.assertIn("BLOCKED", i.text())
        i = self.restore(self.LOCUTUS, auto_confirm=False)
        self.assertIn("cancelled", i.text().lower())
        text = i.text()
        for want in ("Total positive deposits", "NEGATIVE", "Total outstanding loans", "Net market value"):
            self.assertIn(want, text)
        self.assertEqual(self.count("import_batches"), 0)
        self.assertEqual(self.bal(713016), {})

    def test_full_restore_with_negatives_names_and_loans(self):
        self.reset()
        i = self.restore(self.LOCUTUS)
        self.assertIn("restored", i.text().lower())
        self.assertEqual(self.bal(713016), {"money": 500000000, "food": 100000000, "steel": -3000000})   # NOT clamped
        self.assertEqual(self.bal(2), {"money": 100000, "steel": 500000})
        self.assertEqual(self.bal(1), {})                       # alpha was reset and is not in the file
        with self.db.read() as c:
            batch = c.execute("SELECT * FROM import_batches").fetchone()
            self.assertEqual(batch["kind"], "RESTORE")
            rs = c.execute("SELECT * FROM deposit_resets").fetchone()
            self.assertEqual(rs["restore_batch_id"], batch["id"])
            self.assertEqual(batch["reset_id"], rs["id"])
            loans = c.execute("SELECT * FROM imported_loans").fetchall()
            self.assertEqual([(l["nation_id"], l["outstanding_cents"], l["status"]) for l in loans],
                             [(713016, 2500000000, "PENDING_LOAN_MODULE")])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger_entries WHERE entry_type='RESTORE'").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('RESTORE_IMPORT','LOAN_IMPORT')").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM config_audit WHERE setting='loan_import'").fetchone()[0], 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertEqual(L.get_member(c, 713016)["nation_name"], "CITADEL OF REALITY")
        loan_as_deposit = self.bal(713016).get("loan")
        self.assertIsNone(loan_as_deposit)

    def test_restore_cannot_be_applied_twice(self):
        self.reset()
        self.restore(self.LOCUTUS)
        for text in (self.LOCUTUS, self.LOCUTUS.replace("5000000", "5000001").replace("-30000", "30000")):
            self.restore(text)                               # same file again, or an edited copy: now a plain opening import
            self.assertEqual(self.count("import_batches"), 1)
            self.assertEqual(self.bal(713016)["money"], 500000000)   # never double-credited

    def test_restore_ledger_entries_cannot_be_forged(self):
        self.reset()
        with self.assertRaises(sqlite3.Error):
            with self.db.tx() as c:
                L.post_entries(c, [dict(group_id="g", nation_id=1, bucket="AVAILABLE", resource="money", delta=5,
                                        entry_type="RESTORE", batch_id=1, actor="x")])

    def test_loan_data_survives_and_cannot_be_edited(self):
        self.reset()
        self.restore(self.LOCUTUS)
        with self.assertRaises(sqlite3.Error):
            with self.db.tx() as c:
                c.execute("UPDATE imported_loans SET outstanding_cents=1")
        with self.assertRaises(sqlite3.Error):
            with self.db.tx() as c:
                c.execute("DELETE FROM imported_loans")

    def test_importopening_now_accepts_names_but_still_refuses_negatives(self):
        i = self.call(self.bankset, "importopening", self.admin, discord.Attachment("o.csv", b"nation_name,money\nGamma,5\n"), "n")
        self.assertEqual(self.bal(3), {"money": 500})
        i = self.call(self.bankset, "importopening", self.admin, discord.Attachment("p.csv", b"nation_name,steel\nBeta,-5\n"), "n")
        self.assertIn("BLOCKED", i.text())
        self.assertEqual(self.bal(2), {})

    def test_restore_is_not_confused_with_opening_after_an_old_opening_import(self):
        # an OPENING for nation 3 already exists; a reset + restore must still be able to load nation 3 again
        self.call(self.bankset, "importopening", self.admin, discord.Attachment("o.csv", b"nation_name,money\nGamma,5\n"), "n")
        self.reset()
        self.restore("nation_name,money\nGamma,9\n")
        self.assertEqual(self.bal(3), {"money": 900})
        with self.db.read() as c:
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])


class TestNegativeBalances(ResetBase):
    LOCUTUS = "nation_name,money,food,steel\nAlpha,5000000,1000000,-30000\nBeta,100,0,0\n"

    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})
        self.pause()
        self.reset()
        self.restore(self.LOCUTUS)

    def test_negative_balance_is_stored_and_visible(self):
        self.assertEqual(self.bal(1)["steel"], -3000000)

    def test_net_value_uses_positive_and_negative_resources(self):
        with self.db.read() as c:
            av = L.get_balances(c, 1, "AVAILABLE")
        v = value_amounts(av, SNAP)
        self.assertEqual(v.total_cents, 500000000 + 100000000 * 100 - 3000000 * 100)   # $5M + food - steel (in units x price)
        self.assertEqual(v.parts["steel"], -3000000 * 100)

    def test_reconciliation_accepts_negative_balances(self):
        with self.db.tx() as c:
            res = R.run_checks(c, holdings={"money": 10**13, "food": 10**13, "steel": 10**13}, snapshot_id=None, triggered_by="t")
        kinds = {f["kind"] for f in res["findings"]}
        self.assertNotIn("NEGATIVE_BALANCE", kinds)
        self.assertNotIn("BALANCE_MISMATCH", kinds)
        self.assertNotIn("UNJUSTIFIED_ENTRIES", kinds)
        self.assertNotIn("OPENING_CHANGED", kinds)
        with self.db.read() as c:
            self.assertNotEqual(L.integrity_state(c)["state"], "EMERGENCY_LOCK")

    def test_a_debt_does_not_shrink_what_the_bank_must_hold_for_others(self):
        # Alpha owes steel; Beta is owed nothing in steel. The bank must not look like it has spare steel because of the debt.
        with self.db.read() as c:
            pos = R.bank_position(c, {"steel": 0, "money": 10**13, "food": 10**13})
        self.assertEqual(pos["member_total"].get("steel", 0), 0)
        self.assertEqual(pos["alliance_owned"].get("steel", 0), 0)
        self.assertEqual(pos["owed_to_alliance"], {"steel": 3000000})

    def test_negative_resources_are_in_the_export(self):
        data, name = X.build(self.db.conn, "balances", SNAP)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data)).worksheets[0]
        flat = [str(c) for row in ws.iter_rows(values_only=True) for c in row]
        self.assertTrue(any("-30000" in c or "-30,000" in c for c in flat), flat[:40])

    def test_dashboard_shows_the_negative_balance(self):
        i = self.call(self.bank, "dashboard", self.alice)
        self.assertIn("30,000", i.text())
        self.assertTrue("-30,000" in i.text() or "−30,000" in i.text())

    def test_member_cannot_withdraw_a_resource_they_owe(self):
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
        r = self.run_async(self.svc.wd.request(
            tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None,
            dest_nation_id=1, amounts={"steel": 1}, actor="111", note="n", reason="r", idempotency_key="neg-1"))
        self.assertEqual(r.status, "BLOCKED")


class TestNetWorthWithdrawals(ResetBase):
    """Handover example: money $5M, food 100M, steel -30,000. Prices: food $1.00, steel $1,000 => net worth $75M."""
    CSV = "nation_name,money,food,steel\nAlpha,5000000,100000000,-30000\nBeta,1000,0,0\n"

    def setUp(self):
        super().setUp()

        async def prices():
            p = {r: Decimal("1.00") for r in M.NON_CASH}
            p["steel"] = Decimal("1000")
            return p
        self.pnw.fetch_prices = prices
        self.fund_alice({"money": 1000})
        self.pause()
        self.reset()
        self.restore(self.CSV)
        self.pnw.holdings = {r: 10**15 for r in M.RESOURCES}          # the bank physically holds plenty
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")

    def wd(self, amounts, key, nid=1, tx_type="WITHDRAW_SELF", source="MEMBER_AVAILABLE", **kw):
        return self.run_async(self.svc.wd.request(
            tx_type=tx_type, funding_source=source, member_nation_id=nid, lock_id=None, dest_nation_id=nid,
            amounts=amounts, actor="111", note="n", reason="r", idempotency_key=key, **kw))

    def test_net_worth_includes_negative_resources_at_live_prices(self):
        snap = self.run_async(self.svc.prices.get())
        from tunbank import limits as LIM
        with self.db.read() as c:
            amounts, val = LIM.net_worth(c, 1, snap)
        self.assertEqual(val.total_cents, 7500000000)                       # $5M + $100M - $30M
        self.assertEqual(val.parts["steel"], -3000000000)

    def test_withdrawal_worth_more_than_net_worth_is_rejected(self):
        r = self.wd({"food": 9000000000}, "nw-1")                           # 90M food = $90M > $75M
        self.assertEqual(r.status, "BLOCKED")
        for want in ("Withdrawal rejected.", "Current net deposit worth: $75,000,000.00",
                     "Requested withdrawal value: $90,000,000.00", "Maximum allowed: $75,000,000.00"):
            self.assertIn(want, r.message)
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertEqual(self.bal(1)["food"], 10000000000)                  # untouched, nothing held

    def test_withdrawal_within_net_worth_is_allowed(self):
        r = self.wd({"food": 7000000000}, "nw-2")                           # 70M food = $70M <= $75M
        self.assertEqual(r.status, "COMPLETED", r.message)
        self.assertEqual(self.bal(1)["food"], 3000000000)

    def test_exactly_equal_is_allowed_and_one_cent_over_is_not(self):
        r = self.wd({"money": 100000}, "nw-eq", nid=2)                      # Beta holds $10.00 exactly, worth $10.00
        self.assertEqual(r.status, "COMPLETED", r.message)
        r = self.wd({"money": 1}, "nw-over", nid=2)                         # nothing left
        self.assertEqual(r.status, "BLOCKED")

    def test_pending_withdrawals_reduce_net_worth(self):
        with self.db.tx() as c:                                             # a $60M withdrawal already in flight
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1,
                               lock_id=None, dest_nation_id=1, amounts={"food": 6000000000}, actor="111", note="n",
                               reason="r", idempotency_key="held-1")
        r = self.wd({"food": 2000000000}, "nw-3")                           # $20M > ($75M - $60M)
        self.assertEqual(r.status, "BLOCKED")
        self.assertIn("Current net deposit worth: $15,000,000.00", r.message)

    def test_locked_funds_count_when_configured(self):
        with self.db.tx() as c:
            L.create_lock(c, nation_id=1, amounts={"food": 5000000000}, lock_type="X", reason="r", actor="9")
        r = self.wd({"food": 5000000000}, "nw-4")                           # $50M <= $75M (locked still counts)
        self.assertEqual(r.status, "COMPLETED", r.message)
        with self.db.tx() as c:
            cfg_set(c, "net_worth_include_locked", "0", "9")
        r = self.wd({"food": 100000000}, "nw-5")                            # available-only: 0 food + $5M cash - $30M < 0
        self.assertEqual(r.status, "BLOCKED")

    def test_rule_can_be_switched_off(self):
        with self.db.tx() as c:
            cfg_set(c, "net_worth_withdraw_limit", "0", "9")
        r = self.wd({"food": 9000000000}, "nw-6")
        self.assertEqual(r.status, "COMPLETED", r.message)

    def test_econ_and_alliance_withdrawals_are_not_restricted(self):
        r = self.wd({"food": 9000000000}, "nw-7", tx_type="WITHDRAW_ECON", actor_role_ids=())
        self.assertNotIn("Withdrawal rejected", r.message)
        self.assertEqual(r.status, "COMPLETED", r.message)

    def test_missing_price_never_means_zero(self):
        async def prices():
            return {r: Decimal("1.00") for r in M.NON_CASH if r != "steel"}     # steel price missing
        self.pnw.fetch_prices = prices
        self.svc.prices._cached = None
        r = self.wd({"money": 100}, "nw-8")
        self.assertEqual(r.status, "BLOCKED")
        self.assertIn("prices can't be verified", r.message)
        self.assertIn("steel", r.message)

    def test_member_sees_the_rejection_before_confirming(self):
        self.call(self.nation, "link", self.alice, 1)
        i = self.call(self.bank, "withdrawself", self.alice, "food=90m", "n")
        self.assertIn("Withdrawal rejected", i.text())
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_negative_resource_still_cannot_be_withdrawn(self):
        r = self.wd({"steel": 100}, "nw-9")
        self.assertEqual(r.status, "BLOCKED")


class TestApprovedDeduction(ResetBase):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})
        self.pause()
        self.reset()
        self.restore("nation_name,money,steel\nAlpha,1000,10000\n")
        with self.db.tx() as c:
            c.execute("INSERT INTO bankers(discord_id,added_by,added_at) VALUES('222','x','x')")
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('MINISTER','77')")
        self.econ2.roles = [discord.Role(77)]

    def test_deduction_beyond_balance_needs_second_person_then_goes_negative(self):
        i = self.call(self.bank, "adjust", self.admin, 1, "loan deduction", "ticket 5", remove="steel=30000")
        self.assertIn("Approval request", i.text())
        self.assertEqual(self.bal(1)["steel"], 1000000)                      # nothing applied yet
        self.call(self.bank, "approve", self.econ2, 1)
        i = self.call(self.bank, "adjust", self.admin, 1, "loan deduction", "ticket 5", remove="steel=30000")
        self.assertIn("Accounting adjustment", i.text())
        self.assertEqual(self.bal(1)["steel"], -2000000)                     # 10,000 - 30,000 = -20,000, NOT clamped
        with self.db.read() as c:
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE '%ADJUST%'").fetchone()[0], 1)

    def test_same_person_cannot_approve_their_own_debt(self):
        self.call(self.bank, "adjust", self.admin, 1, "loan deduction", "ticket 5", remove="steel=30000")
        i = self.call(self.bank, "approve", self.admin, 1)
        self.assertIn("different staff member", i.text())
        self.assertEqual(self.bal(1)["steel"], 1000000)

    def test_deduction_within_balance_needs_no_approval(self):
        i = self.call(self.bank, "adjust", self.admin, 1, "fix", "ticket 6", remove="steel=4000")
        self.assertIn("Accounting adjustment", i.text())
        self.assertEqual(self.bal(1)["steel"], 600000)

    def test_ledger_function_refuses_debt_without_approval(self):
        with self.assertRaises(L.LedgerError):
            with self.db.tx() as c:
                L.apply_adjustment(c, nation_id=1, deltas={"steel": -3000000}, reason="r", evidence="e", actor="9",
                                   approval_id=None)
        self.assertEqual(self.bal(1)["steel"], 1000000)

    def test_debt_is_refused_while_a_withdrawal_holds_that_resource(self):
        with self.db.tx() as c:
            L.set_state(c, "bank_paused", "0")
            L.begin_withdrawal(c, tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1,
                               lock_id=None, dest_nation_id=1, amounts={"steel": 500000}, actor="111", note="n",
                               reason="r", idempotency_key="held-2")
        i = self.call(self.bank, "adjust", self.admin, 1, "loan deduction", "ticket 5", remove="steel=30000")
        self.assertIn("pending withdrawal", i.text())
        self.assertEqual(self.count("approval_requests"), 0)


class TestMigration007(unittest.TestCase):
    def test_upgrade_from_006_preserves_everything(self):
        d = Path(tempfile.mkdtemp())
        old = d / "old"
        old.mkdir()
        for f in sorted(DBMOD.MIGRATIONS_DIR.glob("00[1-6]_*.sql")):
            shutil.copy(f, old)
        real = DBMOD.MIGRATIONS_DIR
        DBMOD.MIGRATIONS_DIR = old
        try:
            db = DBMOD.Database(d / "t.db")
            db.migrate()
            with db.tx() as c:          # an opening import exactly as the pre-007 code wrote it
                c.execute("INSERT INTO import_batches(created_at,admin_discord_id,source_filename,source_sha256,row_count,"
                          "totals_json,rows_sha256,note) VALUES('2026-10-01T00:00:00Z','9','o.csv','abc',4,'{}','x','old')")
                rows = [(1, "money", 50000), (1, "coal", 2000), (2, "money", 10000), (2, "coal", 500)]
                for n, r, u in rows:
                    c.execute("INSERT INTO opening_balance_rows(batch_id,nation_id,resource,amount) VALUES(1,?,?,?)", (n, r, u))
                    L.ensure_member(c, n)
                prev = L.GENESIS
                for n, r, u in rows:        # raw inserts: the old schema has no reset_id column
                    e = dict(ts="2026-10-01T00:00:00Z", group_id="OPEN-x", nation_id=n, bucket="AVAILABLE", resource=r,
                             delta=u, entry_type="OPENING", batch_id=1, actor="9", note="old")
                    h = L._ledger_hash(prev, e)
                    c.execute("INSERT INTO ledger_entries(ts,group_id,nation_id,bucket,resource,delta,entry_type,batch_id,"
                              "actor,note,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (e["ts"], e["group_id"], n, "AVAILABLE", r, u, "OPENING", 1, "9", "old", prev, h))
                    prev = h
            with db.read() as c:
                chain = L.verify_chain(c, "ledger_entries")
                bal = [tuple(r) for r in c.execute("SELECT * FROM balances ORDER BY 1,2,3")]
                rows = [tuple(r)[:17] for r in c.execute("SELECT * FROM ledger_entries ORDER BY id")]
        finally:
            DBMOD.MIGRATIONS_DIR = real
        self.assertEqual(db.migrate(backup_dir=d / "bk"), ["007_negative_balances_reset_restore_loans.sql", "008_resource_conversion.sql", "009_loans.sql", "010_trade_monitor.sql"])
        self.assertEqual(len(list((d / "bk").glob("pre-migration-*.db"))), 1)
        with db.read() as c:
            self.assertEqual(L.verify_chain(c, "ledger_entries"), chain)
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM balances ORDER BY 1,2,3")], bal)
            self.assertEqual([tuple(r)[:17] for r in c.execute("SELECT * FROM ledger_entries ORDER BY id")], rows)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(c.execute("SELECT kind FROM import_batches").fetchone()[0], "OPENING")
        with self.assertRaises(sqlite3.Error):                   # immutability triggers survived the rebuild
            with db.tx() as c:
                c.execute("UPDATE ledger_entries SET delta=1 WHERE id=1")
        with db.tx() as c:                                       # the chain still extends
            L.create_lock(c, nation_id=2, amounts={"money": 1000}, lock_type="X", reason="r", actor="9")
        with db.read() as c:
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
        db.close()


if __name__ == "__main__":
    unittest.main()
