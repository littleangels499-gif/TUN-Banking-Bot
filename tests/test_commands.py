"""Drives the real command code with a fake Discord. Run: python -m unittest discover -s tests -v"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_discord  # noqa: E402

fake_discord.install()

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from test_core import ALLIANCE, PRICES, FakePnW, rec  # noqa: E402
from tunbank import cmds_admin, cmds_audit, cmds_backup, cmds_chart, cmds_econ, cmds_help, cmds_member  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank.alerts import AlertService  # noqa: E402
from tunbank.config import Settings  # noqa: E402
from tunbank.db import Database  # noqa: E402
from tunbank.scanner import Scanner  # noqa: E402
from tunbank.ui import Services  # noqa: E402
from tunbank.valuation import PriceService  # noqa: E402
from tunbank.withdrawals import WithdrawalService  # noqa: E402

FI = fake_discord.FakeInteraction


class PnW2(FakePnW):
    async def fetch_nation(self, nid):
        return {"id": nid, "nation_name": f"Nation{nid}", "alliance_id": ALLIANCE if nid != 55 else 7,
                "alliance_position": "MEMBER", "discord": {1: "alice", 2: "bob"}.get(nid, "")}


class Cmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.migrate()
        self.pnw = PnW2()
        self.settings = Settings("t", "k", "b", "k", ALLIANCE, Path(self.tmp.name), {900001})
        prices = PriceService(self.db, self.pnw)
        self.svc = Services(self.settings, self.db, self.pnw, prices, Scanner(self.db, self.pnw, prices, self.settings),
                            WithdrawalService(self.db, self.pnw, prices, self.settings), AlertService(self.db))
        self.bank = app_commands.Group("bank")
        self.bankset = app_commands.Group("bankset")
        self.nation = app_commands.Group("nation")
        self.ledger = app_commands.Group("ledger")
        self.tax = app_commands.Group("tax")
        self.audit = app_commands.Group("audit")
        self.chart = app_commands.Group("chart")
        cmds_member.register(self.bank, self.nation, self.svc)
        cmds_econ.register(self.bank, self.svc)
        cmds_admin.register(self.bank, self.bankset, self.ledger, self.svc)
        cmds_backup.register(self.bankset, self.svc)
        cmds_chart.register(self.chart, self.svc)
        cmds_audit.register_tax(self.tax, self.svc)
        cmds_audit.register_audit(self.audit, self.ledger, self.svc)
        self.alice = discord.User(111, "alice")
        self.admin = discord.User(900001, "owner")      # owner => ADMIN
        self.econ2 = discord.User(222, "second")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_async(self, c):
        return asyncio.run(c)

    def call(self, group, name, user, *a, auto_confirm=True, **kw):
        i = FI(user, auto_confirm=auto_confirm)
        self.run_async(getattr(group, "commands")[name].callback(i, *a, **kw))
        return i

    def fund_alice(self, amounts):
        """Real path: baseline scan, link, real deposit detected by scanner."""
        self.run_async(self.svc.scanner.scan())
        self.call(self.nation, "link", self.alice, 1)
        self.pnw.recs.append(rec(900, 1, amounts))
        self.pnw.holdings = {k: int(v * 100) for k, v in amounts.items()}  # bank really holds it
        self.run_async(self.svc.scanner.scan())

    def test_command_registration_is_valid(self):
        names = sorted(self.bank.commands)
        self.assertIn("withdrawself", names)
        self.assertIn("settransferlimit", self.bankset.commands)
        for g in (self.bank, self.bankset, self.nation, self.tax, self.audit, self.ledger):
            self.assertLessEqual(len(g.commands), 25, g.name)

    def test_link_requires_matching_discord_name_and_alliance(self):
        i = self.call(self.nation, "link", self.alice, 1)
        self.assertIn("Linked", i.text())
        i = self.call(self.nation, "link", discord.User(333, "mallory"), 2)      # nation 2 is bob's
        self.assertIn("can't verify", i.text())
        i = self.call(self.nation, "link", discord.User(333, "mallory"), 55)     # other alliance
        self.assertIn("not in our alliance", i.text())

    def test_dashboard_shows_quantity_and_value_but_never_tax(self):
        self.fund_alice({"money": 1000000, "aluminum": 1000, "coal": 1000})
        self.pnw.recs.append(rec(901, 1, {"money": 5555}, "tax", tax_id=3))
        self.run_async(self.svc.scanner.scan())
        i = self.call(self.bank, "dashboard", self.alice)
        t = i.text()
        self.assertIn("Current Market Value", t)
        self.assertIn("$1,000,000.00", t)
        self.assertNotIn("5,555", t)
        self.assertNotIn("tax", t.lower().replace("taxes", ""))

    def test_withdrawself_needs_confirmation_then_sends(self):
        self.fund_alice({"money": 1000000})
        i = self.call(self.bank, "withdrawself", self.alice, "money=400k", auto_confirm=False)
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertIn("Cancelled", i.text())
        i = self.call(self.bank, "withdrawself", self.alice, "money=400k")
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertIn("Withdrawal completed", i.text())
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 60000000)

    def test_withdrawself_locked_funds_rejected(self):
        self.fund_alice({"money": 1000000})
        self.call(self.bank, "reserve", self.admin, 1, "money=1m", "WARCHEST", "war prep")
        i = self.call(self.bank, "withdrawself", self.alice, "money=1")
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertIn("Locked", i.text())

    def test_reserve_release_no_pnw_call_and_shows_value(self):
        self.fund_alice({"money": 1000000, "coal": 100})
        i = self.call(self.bank, "reserve", self.admin, 1, "money=500k coal=50", "WARCHEST", "war")
        self.assertIn("Current Market Value", i.text())
        self.assertEqual(self.pnw.withdraw_calls, 0)
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "LOCKED")["money"], 50000000)
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 50000000)
        i = self.call(self.bank, "release", self.admin, 1, "done")
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "LOCKED"), {})
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 100000000)

    def test_non_staff_cannot_use_staff_commands(self):
        for name, args in (("reserve", (1, "money=1", "X", "r")), ("holdings", ()), ("adjust", (1, "r", "e")),
                           ("importopening", (discord.Attachment("a.csv", b"x"), "n"))):
            i = self.call(self.bank, name, self.alice, *args)
            self.assertIn("permission", i.text(), name)
        for name, args in (("setrole", (discord.app_commands.Choice("ADMIN", "ADMIN"), discord.Role(1))),
                           ("backup", ()), ("config", ())):
            i = self.call(self.bankset, name, self.alice, *args)
            self.assertIn("permission", i.text(), name)

    def test_adjust_positive_needs_second_person(self):
        self.fund_alice({"money": 1000})
        i = self.call(self.bank, "adjust", self.admin, 1, "missed deposit", "ticket 9", add="money=100")
        self.assertIn("Approval request", i.text())
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 100000)
        # same person can't approve
        i = self.call(self.bank, "approve", self.admin, 1)
        self.assertIn("different staff member", i.text())
        # second person (give them Minister via the owner-only setrole path)
        with self.db.tx() as c:
            c.execute("INSERT INTO bankers(discord_id,added_by,added_at) VALUES('222','x','x')")
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('MINISTER','77')")
        self.econ2.roles = [discord.Role(77)]
        i = self.call(self.bank, "approve", self.econ2, 1)
        self.assertIn("Approved", i.text())
        i = self.call(self.bank, "adjust", self.admin, 1, "missed deposit", "ticket 9", add="money=100")
        self.assertIn("Accounting adjustment", i.text())
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 110000)

    def test_large_econ_withdrawal_needs_approval(self):
        self.fund_alice({"money": 100000000})
        with self.db.tx() as c:
            from tunbank.config import cfg_set
            cfg_set(c, "approval_threshold_value", "1000", "t")
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('MINISTER','77')")
        self.econ2.roles = [discord.Role(77)]
        ch = discord.app_commands.Choice("ALLIANCE", "MEMBER_AVAILABLE")
        i = self.call(self.bank, "withdraw", self.admin, ch, 1, "money=5m", "payout", member="1")
        self.assertIn("second approver", i.text())
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.call(self.bank, "approve", self.econ2, 1)
        i = self.call(self.bank, "withdraw", self.admin, ch, 1, "money=5m", "payout", member="1")
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertIn("completed", i.text())

    def test_import_preview_blocks_bad_file_and_good_file_commits(self):
        bad = discord.Attachment("b.csv", b"nation_id,money\n1,-5\n99,3\n")
        i = self.call(self.bank, "importopening", self.admin, bad, "from locutus")
        self.assertIn("BLOCKED", i.text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0], 0)
        good = discord.Attachment("g.csv", b"nation_id,money,coal\n1,500,20\n2,100,0\n")
        i = self.call(self.bank, "importopening", self.admin, good, "from locutus", auto_confirm=False)
        self.assertIn("cancelled", i.text().lower())
        i = self.call(self.bank, "importopening", self.admin, good, "from locutus")
        self.assertIn("imported", i.text().lower())
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {"money": 50000, "coal": 2000})

    def test_vault_shows_alliance_owned_and_tax(self):
        self.fund_alice({"money": 1000000})
        self.pnw.recs.append(rec(902, 2, {"money": 777}, "tax", tax_id=3))
        self.pnw.recs.append(rec(903, 2, {"money": 50}, "#ignore"))
        self.pnw.holdings = {"money": 100000000 + 300000 + 77700 + 5000}
        self.run_async(self.svc.scanner.scan())
        i = self.call(self.bank, "holdings", self.admin)
        t = i.text()
        for word in ("Real PnW bank", "ALLIANCE-OWNED", "Taxes collected", "#ignore", "$777.00"):
            self.assertIn(word, t)

    def test_exports_and_tax_commands(self):
        self.fund_alice({"money": 1000000})
        self.pnw.recs.append(rec(905, 2, {"money": 777}, "tax", tax_id=3))
        self.run_async(self.svc.scanner.scan())
        for kind in ("balances", "ledger", "deposits", "withdrawals", "locks", "tax", "audit", "integrity", "vault"):
            i = self.call(self.bank, "records", self.admin, discord.app_commands.Choice(kind, kind))
            files = [m.get("file") for m in i.sent if m.get("file")]
            self.assertEqual(len(files), 1, kind)
            self.assertGreater(len(files[0].fp.getvalue()), 1000)
        i = self.call(self.tax, "dashboard", self.admin)
        self.assertIn("$777.00", i.text())
        i = self.call(self.tax, "export", self.admin)
        self.assertTrue(any(m.get("file") for m in i.sent))

    def test_reconcile_and_emergency_lock_commands(self):
        self.fund_alice({"money": 1000000})
        i = self.call(self.bank, "reconcile", self.admin)
        self.assertIn("Reconciliation", i.text())
        ch = discord.app_commands.Choice("on", "on")
        self.call(self.ledger, "emergencylock", self.admin, ch, "test drill")
        i = self.call(self.bank, "withdrawself", self.alice, "money=1")
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertIn("EMERGENCY LOCK", i.text())
        i = self.call(self.ledger, "dashboard", self.admin)
        self.assertIn("EMERGENCY_LOCK", i.text())

    def test_hash_chains_stay_valid_after_real_command_use(self):
        """Regression: Discord IDs are numbers; the tamper-check must still verify."""
        from tunbank import reconcile as R
        self.fund_alice({"money": 1000000})
        self.call(self.bank, "reserve", self.admin, 1, "money=1k", "X", "r")
        self.call(self.bank, "withdrawself", self.alice, "money=10")
        self.call(self.bank, "freeze", self.admin, 1, "test")
        self.call(self.bankset, "setrole", self.admin, discord.app_commands.Choice("BANKER", "BANKER"), discord.Role(5))
        with self.db.read() as c:
            self.assertEqual(R.check_chains(c), [])
        i = self.call(self.bank, "reconcile", self.admin)
        self.assertIn("Reconciliation OK", i.text(), i.text())

    def test_nation_fields_accept_id_name_link_username_and_mention(self):
        self.fund_alice({"money": 1000})
        for typed in ("1", "#1", "ALPHA", "alpha", "alice", "@Alice", "<@111>", "Alpha",
                      "https://politicsandwar.com/nation/id=1"):
            i = self.call(self.bank, "freeze", self.admin, typed, "test")
            self.assertIn("Account #1 frozen", i.text(), typed)
        self.call(self.bank, "unfreeze", self.admin, "alice", "done")
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT frozen FROM members WHERE nation_id=1").fetchone()[0], 0)

    def test_nation_lookup_never_guesses(self):
        self.fund_alice({"money": 1000})
        i = self.call(self.bank, "freeze", self.admin, "Alph", "x")      # only part of a name
        self.assertIn("couldn't find", i.text())
        self.assertIn("Did you mean", i.text())
        self.assertIn("[#1]", i.text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT frozen FROM members WHERE nation_id=1").fetchone()[0], 0)
        i = self.call(self.bank, "freeze", self.admin, "nobody-here", "x")
        self.assertIn("couldn't find", i.text())
        with self.db.tx() as c:
            L.ensure_member(c, 2, "alpha")                                  # two nations, same name
        i = self.call(self.bank, "freeze", self.admin, "alpha", "x")
        self.assertIn("More than one nation", i.text())
        i = self.call(self.bank, "freeze", self.admin, "<@999>", "x")        # nobody linked
        self.assertIn("has not linked", i.text())

    def test_nation_autocomplete_suggests_names(self):
        self.fund_alice({"money": 1000})
        cb = self.bank.commands["reserve"].autocompletes["nation"]
        choices = self.run_async(cb(FI(self.admin), "alp"))
        self.assertTrue(any(c.value == "1" and "Alpha" in c.name for c in choices))
        self.assertIn("withdraw", self.bank.commands)
        self.assertEqual(set(self.bank.commands["withdraw"].autocompletes), {"destination", "member"})
        self.assertEqual(self.run_async(cb(FI(self.admin), "zzzz")), [])

    def test_icons_are_configurable_without_code_changes(self):
        from tunbank import icons
        try:
            self.fund_alice({"money": 1000000, "oil": 100})
            i = self.call(self.bank, "dashboard", self.alice)
            self.assertIn("💵", i.text())
            self.assertIn("🛢️", i.text())
            money = discord.app_commands.Choice("Cash", "money")
            i = self.call(self.bankset, "seticon", self.alice, money, "<:cash:123456789012345678>")
            self.assertIn("permission", i.text())
            i = self.call(self.bankset, "seticon", self.admin, money, "abc")
            self.assertIn("doesn't look like an emoji", i.text())
            self.call(self.bankset, "seticon", self.admin, money, "<:cash:123456789012345678>")
            i = self.call(self.bank, "dashboard", self.alice)
            self.assertIn("<:cash:123456789012345678>", i.text())
            self.call(self.bankset, "seticon", self.admin, money, "default")
            i = self.call(self.bank, "dashboard", self.alice)
            self.assertIn("💵", i.text())
            i = self.call(self.bankset, "icons", self.admin)
            self.assertIn("Resource icons", i.text())
        finally:
            icons.load({})

    def test_long_lists_are_paginated_and_short_ones_are_not(self):
        self.fund_alice({"money": 1000})
        with self.db.tx() as c:
            for n in range(9):
                c.execute("INSERT INTO transactions(created_at,updated_at,tx_type,status,funding_source,member_nation_id,"
                          "dest_nation_id,actor_discord_id,idempotency_key,value_cents) VALUES('2026-10-01T00:00:00Z','x',"
                          "'WITHDRAW_SELF','FAILED','MEMBER_AVAILABLE',1,1,'111',?,500)", (f"k{n}",))
        i = self.call(self.audit, "transactions", self.admin)
        self.assertIsNotNone(i.sent[-1].get("view"))
        self.assertIn("Page 1 of 2", i.sent[-1]["embed"].footer_text if hasattr(i.sent[-1]["embed"], "footer_text") else "Page 1 of 2")
        i = self.call(self.bank, "transactions", self.admin, status="NOPE")
        self.assertIsNone(i.sent[-1].get("view"))

    def test_staff_lists_and_vault_render(self):
        self.fund_alice({"money": 1000})
        self.pnw.recs.append(rec(950, 1, {"money": 5}, "#grant"))     # unrecognised note -> review
        self.run_async(self.svc.scanner.scan())
        i = self.call(self.bank, "review", self.admin)
        self.assertIn("waiting for review", i.text())
        self.assertIn("PnW #950", i.text())
        for grp, name, args in ((self.bank, "approvals", ()), (self.audit, "stafflog", ()), (self.bank, "holdings", ()),
                                (self.bank, "history", ()), (self.audit, "nation", ("1",)), (self.tax, "report", ())):
            user = self.alice if name == "history" else self.admin
            i = self.call(grp, name, user, *args)
            self.assertTrue(i.sent, name)

    def test_charts_for_members_and_staff(self):
        self.fund_alice({"money": 1000000, "coal": 500})
        self.pnw.recs.append(rec(960, 2, {"money": 777}, "tax", tax_id=3))
        self.run_async(self.svc.scanner.scan())
        self.call(self.bank, "reconcile", self.admin)
        for name in ("mybalance", "mytrend"):
            i = self.call(self.chart, name, self.alice)
            files = [m.get("file") for m in i.sent if m.get("file")]
            self.assertEqual(len(files), 1, name)
            self.assertTrue(files[0].fp.getvalue().startswith(b"\x89PNG"), name)
            self.assertEqual(i.sent[-1]["embed"].image_url, "attachment://" + files[0].filename)
        for name, args in (("vault", ()), ("members", ()), ("deposits", ()), ("tax", ()),
                           ("nation", ("alice", discord.app_commands.Choice("Resource mix", "mix"))),
                           ("nation", ("1", discord.app_commands.Choice("Value over time", "trend"))),
                           ("nation", ("1", discord.app_commands.Choice("Deposits", "deposits")))):
            i = self.call(self.chart, name, self.admin, *args)
            self.assertTrue(any(m.get("file") for m in i.sent), (name, i.text()))
        # members must not see staff charts (tax especially)
        for name in ("vault", "tax", "deposits", "members"):
            i = self.call(self.chart, name, self.alice)
            self.assertIn("permission", i.text(), name)
        # no data -> a friendly message, not a crash
        i = self.call(self.chart, "mytrend", discord.User(555, "stranger"))
        self.assertIn("Link your nation", i.text())

    def test_help_is_role_aware(self):
        from tunbank import bot as botmod
        b = botmod.TunBankBot(self.settings, self.db)
        self.run_async(b.setup_hook())
        helpfn = b.tree.top["help"]
        i = FI(self.alice)
        self.run_async(helpfn(i))
        t = i.text()
        self.assertIn("/bank withdrawself", t)
        self.assertNotIn("/bankset", t)
        self.assertNotIn("/bank reserve", t)
        i = FI(self.admin)
        self.run_async(helpfn(i))
        t = i.text()
        for word in ("/bankset setrole", "/bank reserve", "/bank withdraw`", "/bank holdings", "/bank withdrawself"):
            self.assertIn(word, t)

    def test_bot_wiring_registers_without_clashes(self):
        from tunbank import bot as botmod
        b = botmod.TunBankBot(self.settings, self.db)
        self.run_async(b.setup_hook())
        self.assertEqual(sorted(b.tree.cmds), ["audit", "bank", "bankset", "chart", "ledger", "nation", "tax"])


if __name__ == "__main__":
    unittest.main()
