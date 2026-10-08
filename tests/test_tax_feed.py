"""PnW keeps tax collections in a separate `taxrecs` feed. These tests make sure the bot reads it, never mistakes a tax
record for a member deposit, never floods the tax channel with old history, and says so when the feed is broken."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
from test_core import rec  # noqa: E402

from tunbank import ledger as L  # noqa: E402
from tunbank.pnw import PnWClient, PnWRejected  # noqa: E402
from tunbank.ui import post_outcomes  # noqa: E402


class TestTaxFeed(unittest.TestCase):
    make_settings = TC.Cmd.make_settings
    run_async = TC.Cmd.run_async
    call = TC.Cmd.call
    fund_alice = TC.Cmd.fund_alice
    give = TC.SecurityCmd.give
    tax_rec = TC.SecurityCmd.tax_rec
    tearDown = TC.Cmd.tearDown

    def setUp(self):
        TC.Cmd.setUp(self)                                   # same wiring as the other tax tests
        self.bot = TC.FakeBotChannels()
        self.econ_log, self.tax_chan = TC.FakeChannel(), TC.FakeChannel()
        self.bot.chans = {111000: self.econ_log, 222000: self.tax_chan}
        self.svc.alerts.bot = self.bot
        from tunbank.config import cfg_set
        with self.db.tx() as c:
            cfg_set(c, "econ_log_channel_id", "111000", "t")
            cfg_set(c, "tax_alert_channel_id", "222000", "t")
            cfg_set(c, "tax_alert_settle_seconds", "0", "t")
            for lvl, role in (("AUDITOR", "66"), ("MINISTER", "77"), ("BANKER", "88")):
                c.execute("INSERT INTO role_permissions(level,role_id) VALUES(?,?)", (lvl, role))
        for attr, uid, name, role in (("auditor", 401, "aud", 66), ("minister", 402, "min", 77), ("banker", 403, "bnk", 88)):
            u = TC.discord.User(uid, name)
            u.roles = [TC.discord.Role(role)]
            setattr(self, attr, u)
        self.pnw.taxrecs = []

    def scan(self):
        r = self.run_async(self.svc.scanner.scan())
        self.run_async(post_outcomes(self.svc, r.outcomes))
        return r

    def count(self, table):
        with self.db.read() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_tax_records_arrive_through_the_tax_feed_with_their_bracket(self):
        self.scan()                                                         # baseline
        self.pnw.taxrecs = [self.tax_rec(8001, 1, 1000.0, "2026-10-02 14:00:02", tax_id=3),
                            self.tax_rec(8002, 2, 2000.0, "2026-10-02 14:00:03", tax_id=4)]
        r = self.scan()
        self.assertEqual(r.tax_seen, 2)
        self.assertEqual(self.count("tax_records"), 2)
        with self.db.read() as c:
            rows = {x["nation_id"]: x["tax_id"] for x in c.execute("SELECT nation_id, tax_id FROM tax_records")}
        self.assertEqual(rows, {1: 3, 2: 4})                                # each member's own bracket, nothing set by hand

    def test_a_tax_feed_record_without_a_tax_id_is_never_credited_as_a_deposit(self):
        self.scan()
        before = self.pnw.recs[:]
        odd = self.tax_rec(8101, 1, 5000.0, "2026-10-02 16:00:01", tax_id=0)   # PnW forgot the id on this one
        self.pnw.taxrecs = [odd]
        r = self.scan()
        self.assertEqual({o.record_id: o.kind for o in r.outcomes}[8101], "TAX")
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {})         # a member's tax is NOT their deposit
            row = c.execute("SELECT classification, status FROM pnw_records WHERE id=8101").fetchone()
        self.assertEqual((row["classification"], row["status"]), ("TAX", "NO_CREDIT"))
        self.assertEqual(self.pnw.recs, before)

    def test_a_record_listed_in_both_feeds_is_counted_once_as_tax(self):
        self.scan()
        t = self.tax_rec(8201, 1, 100.0, "2026-10-02 18:00:01")
        self.pnw.recs.append(dict(t))
        self.pnw.taxrecs = [t]
        self.scan()
        self.assertEqual(self.count("tax_records"), 1)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT classification FROM pnw_records WHERE id=8201").fetchone()[0], "TAX")
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {})

    def test_the_stored_raw_record_is_exactly_what_pnw_sent(self):
        self.scan()
        self.pnw.taxrecs = [self.tax_rec(8301, 1, 100.0, "2026-10-02 18:00:01")]
        self.scan()
        with self.db.read() as c:
            raw = c.execute("SELECT raw_json FROM pnw_records WHERE id=8301").fetchone()[0]
        self.assertNotIn("_taxrec", raw)

    def test_old_history_from_the_first_tax_feed_read_is_not_announced(self):
        self.scan()                                                         # an existing install: baseline already done
        with self.db.tx() as c:                                             # ...that has never read the tax feed (just upgraded)
            c.execute("DELETE FROM system_state WHERE key='tax_feed_started'")
        self.pnw.taxrecs = [self.tax_rec(8400 + i, 1, 100.0, f"2026-09-{20 + i:02d} 10:00:01") for i in range(6)]
        self.scan()
        self.assertEqual(self.count("tax_records"), 6)
        self.assertEqual(self.tax_chan.sent, [])                            # six old turns, zero messages
        with self.db.read() as c:
            self.assertEqual(L.get_state(c, "tax_feed_started"), "1")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tax_turns WHERE alerted_at='backfill'").fetchone()[0], 6)
        self.pnw.taxrecs.append(self.tax_rec(8500, 1, 1000.0, "2026-10-02 20:00:02"))   # a genuinely new turn
        self.scan()
        self.assertEqual(len(self.tax_chan.sent), 1)                        # announced exactly once

    def test_a_broken_tax_feed_never_stops_deposits_and_is_reported(self):
        self.pnw.taxrecs_error = "Cannot query field \"taxrecs\" on type \"Alliance\"."
        self.scan()
        self.pnw.recs.append(rec(8601, 1, {"money": 500.0}, ""))
        r = self.scan()
        self.assertTrue(r.ok)
        self.assertIn("taxrecs", r.tax_error)
        with self.db.read() as c:
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE")["money"], 50000)   # the deposit was still credited
            self.assertIn("taxrecs", L.get_state(c, "last_tax_fetch_error"))
            self.assertNotEqual(L.get_state(c, "tax_feed_started"), "1")          # not "started" until it really works
        out = self.call(self.tax, "sync", self.banker).text()
        self.assertIn("tax feed could not be read", out)

    def test_tax_sync_reports_what_pnw_returned(self):
        self.scan()
        self.assertIn("returned no tax records", self.call(self.tax, "sync", self.banker).text())
        self.pnw.taxrecs = [self.tax_rec(8701, 1, 100.0, "2026-10-02 22:00:01")]
        out = self.call(self.tax, "sync", self.banker).text()
        self.assertIn("returned 1 tax record(s), 1 of them new", out)

    def test_profile_reads_the_current_bracket_from_pnw_without_any_setup(self):
        self.give("bank_view_tax", 77)
        self.pnw.nation_tax_ids = {1: 7}
        out = self.call(self.tax, "profile", self.minister, "alpha").text()
        self.assertIn("read live from PnW", out)
        self.assertIn("#7", out)

    def test_per_member_per_turn_contribution_is_visible(self):
        self.give("bank_view_tax", 77)
        self.scan()
        self.pnw.taxrecs = [self.tax_rec(8801, 1, 1000.0, "2026-10-02 14:00:02"),
                            self.tax_rec(8802, 1, 1200.0, "2026-10-02 16:00:02")]
        self.scan()
        out = self.call(self.tax, "profile", self.minister, "alpha").text()
        self.assertIn("2026-10-02 16:00 UTC", out)                          # one line per turn, with the turn time
        self.assertIn("2026-10-02 14:00 UTC", out)
        self.assertIn("$1,000.00", out)
        self.assertIn("$1,200.00", out)

    def test_tax_feed_records_whose_sender_is_not_typed_as_a_nation_are_still_tax(self):
        """The real-world bug: PnW's tax records failed the 'sender is a nation' check and went to ECON review."""
        self.scan()
        odd = []
        for i, st in enumerate((0, None, 2, 3)):
            r = self.tax_rec(8900 + i, 1 + i % 2, 1000.0, "2026-10-02 14:00:0%d" % i, tax_id=0)
            r["sender_type"] = st
            r["receiver_type"] = None
            odd.append(r)
        self.pnw.taxrecs = odd
        r = self.scan()
        self.assertEqual({o.kind for o in r.outcomes if o.record_id}, {"TAX"})
        self.assertEqual(self.count("tax_records"), 4)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pnw_records WHERE status='AWAITING_REVIEW'").fetchone()[0], 0)
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {})
        self.assertNotIn("needs ECON review", self.econ_log.text())

    def test_tax_records_already_stuck_in_review_are_corrected_without_flooding_the_channel(self):
        from tunbank import bankrec as B
        self.scan()
        stuck = [self.tax_rec(8950 + i, 1 + i % 3, 500.0, "2026-10-01 10:00:0%d" % i) for i in range(5)]
        with self.db.tx() as c:                                  # exactly what the earlier version stored
            for r in stuck:
                r2 = dict(r, sender_type=0)
                B.insert_record(c, B.normalize(r2), direction="IN", classification="REVIEW", status="AWAITING_REVIEW")
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pnw_records WHERE status='AWAITING_REVIEW'").fetchone()[0], 5)
        self.pnw.taxrecs = [dict(r, sender_type=0) for r in stuck]
        self.scan()
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pnw_records WHERE status='AWAITING_REVIEW'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pnw_records WHERE classification='TAX' AND status='NO_CREDIT'").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='TAX_RECLASSIFIED'").fetchone()[0], 1)
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {})
        self.assertEqual(self.count("tax_records"), 5)
        self.assertEqual(self.tax_chan.sent, [])                  # old turns: stored, not announced
        self.scan()                                               # a second scan changes nothing
        self.assertEqual(self.count("tax_records"), 5)

    def test_one_summary_per_turn_for_all_nations_together(self):
        self.give("bank_view_tax", 77)
        self.scan()
        self.pnw.taxrecs = [dict(self.tax_rec(9000 + i, 1 + i % 2, 1000.0 * (i + 1), "2026-10-03 12:00:0%d" % i), sender_type=0)
                            for i in range(6)]
        self.scan()
        self.assertEqual(len(self.tax_chan.sent), 1)              # six nations, ONE message
        text = self.tax_chan.text()
        self.assertIn("Turn Complete", text)
        self.assertIn("12:00 UTC", text)
        self.assertIn("$21,000.00", text)                         # 1+2+3+4+5+6 thousand, all nations added together
        self.assertNotIn("Alpha", text)                           # totals only: no per-member list

    def test_the_real_client_asks_for_taxrecs_and_marks_what_it_gets(self):
        class S:
            class main:
                alliance_id = 123
                name = "main"
        client = PnWClient.__new__(PnWClient)
        client.s = S
        seen = {}

        async def fake_query(q, variables, bank=None):
            seen["q"] = q
            return {"alliances": {"data": [{"id": 123, "taxrecs": [{"id": 1, "money": 5, "tax_id": 0}]}]}}
        client.query = fake_query
        out = asyncio.run(client.fetch_taxrecs())
        self.assertIn("taxrecs{", seen["q"])
        self.assertTrue(out[0]["_taxrec"])

        async def none_query(q, variables, bank=None):
            return {"alliances": {"data": [{"id": 123, "taxrecs": None}]}}
        client.query = none_query
        with self.assertRaises(PnWRejected):
            asyncio.run(client.fetch_taxrecs())


if __name__ == "__main__":
    unittest.main()
