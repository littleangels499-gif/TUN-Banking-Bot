"""/transfer raws and /grant warchest|city|infrastructure|land: calculations (golden values), funding sources, failure handling,
duplicate protection and the audit trail, through the real commands, scanner and withdrawal pipeline.

NOTE: the calculation tests check that the code does what the DOCUMENTED rules in pnw_rules.py say. They cannot prove those
rules match the live game; that is what the previews + /grantset live switch are for."""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402
from test_core import ALLIANCE  # noqa: E402

from tunbank import disburse as DB  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank import pnw_rules as PR  # noqa: E402
from tunbank.config import cfg_set  # noqa: E402

Choice = TC.discord.app_commands.Choice
ALLI, MEM_A, MEM_L = Choice("Alliance funds", "ALLIANCE"), Choice("avail", "MEMBER_AVAILABLE"), Choice("locked", "MEMBER_LOCKED")
RES = list(M.RESOURCES)
ZERO_B = {k: 0 for k in PR.ALL_BUILDINGS}


def city(infra=1000, land=500, cid=1, **buildings):
    return {"id": cid, "infrastructure": infra, "land": land, "buildings": {**ZERO_B, **buildings}}


def nation(nid, n_cities=12, *, alliance=ALLIANCE, stock="default", cities="auto", **kw):
    if stock == "default":
        stock = {r: 0 for r in RES}
    if cities == "auto":
        cities = [city(cid=i + 1) for i in range(n_cities)]
    d = {"id": nid, "name": f"N{nid}", "alliance_id": alliance, "num_cities": n_cities, "population": 100000, "policy": None,
         "military": {u: 0 for u in PR.MILITARY_CASH_UPKEEP}, "stock": stock,
         "projects": {p: False for p in ("urban_planning", "advanced_urban_planning", "metropolitan_planning",
                                         "center_for_civil_engineering", "advanced_engineering_corps")}, "cities": cities}
    d.update(kw)
    return d


def stock(**kw):
    s = {r: 0 for r in RES}
    s.update(kw)
    return s


# ====================================================================================== calculations
class TestWarchestRules(unittest.TestCase):
    PER = M.parse_amounts("food=1000 steel=100 aluminum=100")

    def amounts(self, c):
        return {r: M.units_to_float(a) for r, a in c.amounts.items()}

    def test_spec_example_ratio_1_and_2_for_a_12_city_nation(self):
        n = nation(1, 12)
        self.assertEqual(self.amounts(PR.compute_warchest(n, self.PER, "1")), {"food": 12000, "steel": 1200, "aluminum": 1200})
        self.assertEqual(self.amounts(PR.compute_warchest(n, self.PER, "2")), {"food": 24000, "steel": 2400, "aluminum": 2400})

    def test_decimal_ratios(self):
        n = nation(1, 12)
        for ratio, food in (("1.1", 13200), ("1.5", 18000), ("2.5", 30000), (1.5, 18000)):
            self.assertEqual(self.amounts(PR.compute_warchest(n, self.PER, ratio))["food"], food, ratio)

    def test_only_missing_deducts_the_verified_stockpile_and_never_goes_negative(self):
        n = nation(1, 12, stock=stock(food=5000, steel=9999, aluminum=0))
        c = PR.compute_warchest(n, self.PER, 1, only_missing=True)
        self.assertEqual(self.amounts(c), {"food": 7000, "aluminum": 1200})                # steel is already covered: not sent
        self.assertTrue(c.sendable)

    def test_only_missing_without_a_verified_stockpile_is_flagged_not_guessed(self):
        c = PR.compute_warchest(nation(1, 12, stock=None), self.PER, 1, only_missing=True)
        self.assertFalse(c.sendable)
        self.assertIn("stockpile", c.review[0])

    def test_bad_ratio_unknown_cities_or_no_config_are_flagged(self):
        n = nation(1, 12)
        for ratio in (0, -1, 11):
            self.assertFalse(PR.compute_warchest(n, self.PER, ratio).sendable, ratio)
        self.assertFalse(PR.compute_warchest(nation(1, 0), self.PER, 1).sendable)
        self.assertFalse(PR.compute_warchest(n, {}, 1).sendable)


class TestRawsRules(unittest.TestCase):
    def base(self, **kw):
        n = nation(1, 1, cities=[city(1000, 500, coal_power=2, steel_mill=1, farm=1)], population=100000, stock=stock(money=400000))
        n["military"].update(soldiers=10000)
        n.update(kw)
        return n

    def test_golden_two_days_from_the_nations_actual_buildings(self):
        # per day: 2 coal plants power 1000 infra = 12 coal; steel mill 3 coal + 3 iron; food 100000/1000 + 10000/500 = 120;
        # cash 2*1200 + 4000 + 300 + 10000*1.25 = 19,200
        c = PR.compute_raws(self.base(), 2)
        self.assertTrue(c.sendable, c.review)
        got = {r: M.units_to_float(a) for r, a in c.requirement.items()}
        self.assertEqual(got, {"coal": 30, "iron": 6, "food": 240, "money": 38400})
        self.assertEqual(c.amounts, c.requirement)

    def test_requirements_depend_on_the_nation_not_a_fixed_list(self):
        plain = PR.compute_raws(nation(1, 1, cities=[city(500, 500, wind_power=2)]), 1, include_cash=False)
        self.assertNotIn("coal", plain.requirement)
        self.assertNotIn("iron", plain.requirement)
        nuclear = PR.compute_raws(nation(1, 1, cities=[city(2000, 500, nuclear_power=1)]), 1, include_cash=False)
        self.assertEqual(M.units_to_float(nuclear.requirement["uranium"]), 5.0)                # 2000 infra * 0.0024 = 4.8, rounded UP to a whole unit
        refiner = PR.compute_raws(nation(1, 1, cities=[city(500, 500, oil_power=1, oil_refinery=2, munitions_factory=1)]), 1, include_cash=False)
        self.assertEqual({r: M.units_to_float(a) for r, a in refiner.requirement.items() if r != "food"},
                         {"oil": 12.0, "lead": 6.0})                                           # 6 oil to power + 2*3 refinery; lead for munitions

    def test_decimal_days_round_up_to_a_whole_unit(self):
        c = PR.compute_raws(self.base(), "1.5", include_cash=False)
        self.assertEqual(M.units_to_float(c.requirement["coal"]), 23)                          # 15/day * 1.5 = 22.5 -> 23 (never short)

    def test_an_underpowered_city_has_idle_factories(self):
        n = self.base(cities=[city(1000, 500, coal_power=1, steel_mill=1)])                    # 1 plant powers 500 of 1000 infra
        c = PR.compute_raws(n, 1, include_cash=False)
        self.assertEqual(M.units_to_float(c.requirement["coal"]), 6.0)                         # only the plant's own fuel
        self.assertNotIn("iron", c.requirement)
        self.assertTrue(any("not fully powered" in a for a in c.assumptions))

    def test_no_cash_option(self):
        c = PR.compute_raws(self.base(), 1, include_cash=False)
        self.assertNotIn("money", c.requirement)

    def test_incomplete_data_flags_the_nation_instead_of_guessing(self):
        cases = {
            "population": self.base(population=None),
            "military": self.base(military=None),
            "city data": self.base(cities=[]),
            "buildings": self.base(cities=[{"id": 1, "infrastructure": 1000, "land": 500, "buildings": None}]),
            "building keys": self.base(cities=[{"id": 1, "infrastructure": 1000, "land": 500, "buildings": {"farm": 1}}]),
        }
        for label, n in cases.items():
            c = PR.compute_raws(n, 1)
            self.assertFalse(c.sendable, label)
            self.assertTrue(c.review, label)

    def test_only_missing_deducts_stock_and_requires_a_verified_stockpile(self):
        c = PR.compute_raws(self.base(stock=stock(coal=10, food=1000, money=0)), 1, only_missing=True)
        self.assertEqual(M.units_to_float(c.amounts["coal"]), 5)                               # 15 needed - 10 held
        self.assertNotIn("food", c.amounts)
        self.assertFalse(PR.compute_raws(self.base(stock=None), 1, only_missing=True).sendable)

    def test_daily_cash_rule_only_ever_reduces_cash(self):
        n = self.base(stock=stock(money=400000))
        c = PR.compute_raws(n, 1)
        full = c.amounts["money"]
        PR.apply_daily_cash_rule(c, n, 500000)                                                  # room: 500,000 - 400,000
        self.assertEqual(c.amounts["money"], full)                                              # raws cash is $19,200 < the $100,000 of room: unchanged
        tight = self.base(stock=stock(money=495000))
        c1 = PR.compute_raws(tight, 1)
        PR.apply_daily_cash_rule(c1, tight, 500000)
        self.assertEqual(M.units_to_float(c1.amounts["money"]), 5000)                           # only $5,000 of room left: capped, never raised
        rich = self.base(stock=stock(money=900000))
        c2 = PR.compute_raws(rich, 1)
        PR.apply_daily_cash_rule(c2, rich, 500000)
        self.assertNotIn("money", c2.amounts)                                                   # already above the threshold: no cash
        c3 = PR.compute_raws(n, 1)
        PR.apply_daily_cash_rule(c3, n, 0)
        self.assertFalse(c3.sendable)                                                           # not configured -> flagged
        c4 = PR.compute_raws(self.base(stock=None), 1)
        PR.apply_daily_cash_rule(c4, self.base(stock=None), 500000)
        self.assertFalse(c4.sendable)


class TestPurchaseRules(unittest.TestCase):
    def test_city_golden_values(self):
        self.assertEqual(PR.city_price(12), 68425000)                                           # 50000*11^3 + 150000*12 + 75000
        c = PR.compute_city(nation(1, 12), 1)
        self.assertEqual(M.units_to_float(c.amounts["money"]), 68425000)
        n = nation(1, 12)
        n["projects"].update(urban_planning=True, advanced_urban_planning=True, metropolitan_planning=True)
        self.assertEqual(M.units_to_float(PR.compute_city(n, 1).amounts["money"]), 58161250)  # -15%
        two = PR.compute_city(nation(1, 12), 2)
        self.assertEqual(M.units_to_float(two.amounts["money"]), 68425000 + int(PR.city_price(13)))

    def test_unknown_projects_make_it_an_estimate_not_exact(self):
        n = nation(1, 12)
        n["projects"]["urban_planning"] = None
        c = PR.compute_city(n, 1)
        self.assertTrue(c.estimate)
        self.assertTrue(any("ESTIMATE" in a for a in c.assumptions))
        self.assertFalse(PR.compute_city(nation(1, 12), 1).estimate)

    def test_city_input_validation(self):
        self.assertFalse(PR.compute_city(nation(1, 0), 1).sendable)
        self.assertFalse(PR.compute_city(nation(1, 12), 0).sendable)
        self.assertFalse(PR.compute_city(nation(1, 12), 11).sendable)

    def test_infrastructure_priced_from_each_cities_own_level(self):
        n = nation(1, 3, cities=[city(1000, cid=1), city(1500, cid=2), city(2000, cid=3)])
        c = PR.compute_infra(n, 1500)
        expect = sum(0.0 + (abs(x - 10) ** 2.2 / 710 + 300) for x in range(1000, 1500))        # city 1 only; the others are at/above 1500
        self.assertAlmostEqual(M.units_to_float(c.amounts["money"]), expect, delta=1)
        self.assertEqual(sum(1 for ln in c.lines if "already at or above" in ln), 2)           # not identical per city, not city-count based
        a = PR.compute_infra(nation(1, 2, cities=[city(1000, cid=1), city(1000, cid=2)]), 1500)
        b = PR.compute_infra(nation(1, 2, cities=[city(1000, cid=1), city(1400, cid=2)]), 1500)
        self.assertGreater(a.amounts["money"], b.amounts["money"])

    def test_infrastructure_and_land_discounts_and_nothing_to_grant(self):
        n = nation(1, 1, cities=[city(1000, 100, cid=1)])
        full = PR.compute_infra(n, 1100).amounts["money"]
        n["projects"].update(center_for_civil_engineering=True)
        self.assertAlmostEqual(PR.compute_infra(n, 1100).amounts["money"], full * 0.95, delta=200)
        done = PR.compute_infra(nation(1, 1, cities=[city(3000, cid=1)]), 1500)
        self.assertEqual(done.amounts, {})
        self.assertTrue(done.sendable)

    def test_land_priced_acre_by_acre_from_each_cities_land(self):
        n = nation(1, 2, cities=[city(1000, 500, cid=1), city(1000, 700, cid=2)])
        c = PR.compute_land(n, 800)
        expect = sum(0.002 * (x - 20) ** 2 + 50 for x in range(500, 800)) + sum(0.002 * (x - 20) ** 2 + 50 for x in range(700, 800))
        self.assertAlmostEqual(M.units_to_float(c.amounts["money"]), expect, delta=1)

    def test_missing_city_data_is_flagged(self):
        n = nation(1, 2, cities=[city(None, 500, cid=1), city(1000, 500, cid=2)])
        self.assertFalse(PR.compute_infra(n, 1500).sendable)
        self.assertFalse(PR.compute_land(nation(1, 3, cities=[city(cid=1)]), 800).sendable)          # PnW returned 1 of 3 cities

    def test_rules_sheet_names_its_version(self):
        self.assertIn(PR.RULES_VERSION, PR.sheet())
        self.assertIn("UNVERIFIED", PR.RULES_VERSION)


# ====================================================================================== the engine, through real commands
class Base(TC.Cmd):
    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000, "food": 50000, "steel": 5000, "aluminum": 5000})            # nation 1 owns real deposited funds
        with self.db.tx() as c:
            L.ensure_member(c, 2, "Beta")
        self.pnw.holdings = {r: 10 ** 13 for r in RES}                                              # the alliance owns a great deal beyond members
        self.nations = {1: nation(1, 12), 2: nation(2, 12), 3: nation(3, 6)}
        self.detail_calls = 0

        async def fetch(ids):
            self.detail_calls += 1
            return {i: json.loads(json.dumps(self.nations[i])) for i in ids if i in self.nations}
        self.pnw.fetch_nations_detail = fetch
        self.set_live(*DB.KINDS)

    def set_live(self, *kinds):
        with self.db.tx() as c:
            cfg_set(c, "disburse_live_kinds", ",".join(kinds), "t")

    def cfg(self, key, value):
        with self.db.tx() as c:
            cfg_set(c, key, value, "t")

    def make_minister(self, flags=()):
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO role_permissions(level,role_id) VALUES('MINISTER','77')")
            for f in flags:
                c.execute("INSERT OR IGNORE INTO role_flags(flag,role_id) VALUES(?, '77')", (f,))
        self.econ2.roles = [TC.discord.Role(77)]

    def warchest(self, who, nations, *, source=ALLI, **kw):
        return self.call(self.grant, "warchest", who, nations, source, **kw)

    def rows(self, sql, *a):
        with self.db.read() as c:
            return [dict(r) for r in c.execute(sql, a)]

    def bal(self, nid, bucket="AVAILABLE"):
        with self.db.read() as c:
            return L.get_balances(c, nid, bucket)

    def batches(self):
        return self.rows("SELECT * FROM disbursement_batches ORDER BY id")

    def items(self, batch=1):
        return self.rows("SELECT * FROM disbursement_items WHERE batch_id=? ORDER BY seq", batch)


for _n in dir(TC.Cmd):
    if _n.startswith("test_"):
        setattr(Base, _n, None)
del _n


class TestAllianceFunded(Base):
    def test_warchest_sends_exactly_the_calculated_amounts_and_records_everything(self):
        i = self.warchest(self.admin, "1, 2")
        self.assertEqual(self.pnw.withdraw_calls, 2)
        b = self.batches()
        self.assertEqual((len(b), b[0]["kind"], b[0]["funding_source"], b[0]["status"]), (1, "WARCHEST_GRANT", "ALLIANCE", "COMPLETED"))
        self.assertIn("ALLIANCE-OWNED", b[0]["source_description"])
        self.assertEqual(json.loads(b[0]["totals_json"]), {"food": 2400000, "steel": 240000, "aluminum": 240000})          # 2 nations x (12000, 1200, 1200)
        for it in self.items():
            self.assertEqual((it["status"], it["kind"]), ("COMPLETED", "WARCHEST_GRANT"))
            self.assertEqual(json.loads(it["amounts_json"]), {"food": 1200000, "steel": 120000, "aluminum": 120000})
            self.assertIsNotNone(it["tx_id"])
            self.assertIsNotNone(it["pnw_record_id"])
            calc = json.loads(it["calculation_json"])
            self.assertTrue(calc["lines"])
            self.assertEqual(calc["inputs"]["ratio"], "1")
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions WHERE status='COMPLETED'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='DISBURSEMENT_CREATED'").fetchone()[0], 1)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"] and L.verify_chain(c, "audit_log")["ok"])
        self.assertIn("ALLIANCE-OWNED", i.text())

    def test_member_deposits_are_untouched_when_the_alliance_pays(self):
        before = self.bal(1)
        self.warchest(self.admin, "1")
        self.assertEqual(self.bal(1), before)                                                         # alliance money, never the member's

    def test_pnw_note_carries_the_tx_tag_and_the_ignore_note_but_never_the_classification(self):
        self.warchest(self.admin, "1")
        note = self.pnw.recs[-1]["note"]
        self.assertIn("TUN-TX", note)
        self.assertIn("#ignore", note.lower())
        self.assertNotIn("WARCHEST_GRANT", note)
        self.cfg("disburse_ignore_tag", "0")
        self.warchest(self.admin, "2", note="second")
        self.assertNotIn("#ignore", self.pnw.recs[-1]["note"].lower())

    def test_our_own_sends_are_not_mistaken_for_external_withdrawals(self):
        self.warchest(self.admin, "1")
        before = self.bal(1)
        for _ in range(3):
            self.run_async(self.svc.scanner.scan())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM external_withdrawals").fetchone()[0], 0)
        self.assertEqual(self.bal(1), before)

    def test_only_alliance_funds_the_alliance_owned_amount_is_what_counts(self):
        # the bank holds exactly what members own: the alliance owns nothing, so nothing can be spent from "alliance funds"
        self.pnw.holdings = {"money": 100000, "food": 5000000, "steel": 500000, "aluminum": 500000}
        i = self.warchest(self.admin, "1")
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertEqual(self.batches(), [])
        self.assertIn("blocked", i.text().lower())

    def test_nothing_is_sent_at_all_when_the_source_cannot_cover_everyone(self):
        # alliance-owned food: 20,000 - enough for one nation (12,000) but not two (24,000)
        self.pnw.holdings = {"money": 100000, "food": 5000000 + 2000000, "steel": 10 ** 12, "aluminum": 10 ** 12}
        i = self.warchest(self.admin, "1, 2")
        self.assertEqual(self.pnw.withdraw_calls, 0)                                                  # not even the first one: no partial transfer
        self.assertEqual(self.batches(), [])
        self.assertIn("short", i.text().lower())

    def test_preview_only_until_an_admin_switches_a_kind_on(self):
        self.set_live()
        i = self.warchest(self.admin, "1")
        self.assertIn("PREVIEW ONLY", i.text())
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.set_live("WARCHEST_GRANT")
        self.warchest(self.admin, "1", note="now live")
        self.assertEqual(self.pnw.withdraw_calls, 1)

    def test_a_cancelled_confirmation_sends_nothing(self):
        self.call(self.grant, "warchest", self.admin, "1", ALLI, auto_confirm=False)
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))

    def test_the_preview_states_the_source_and_attaches_the_full_calculation(self):
        i = self.warchest(self.admin, "1")
        text = i.text()
        self.assertIn("FUNDING SOURCE", text)
        self.assertIn("ALLIANCE-OWNED", text)
        files = [m["file"] for m in i.sent if m.get("file") is not None]
        self.assertTrue(files)
        body = files[0].fp.getvalue().decode()
        for must in ("Requirement", "To send", "From: ALLIANCE-OWNED", "per city x 12 cities x ratio 1"):
            self.assertIn(must, body)

    def test_emergency_lock_blocks_the_whole_operation(self):
        with self.db.tx() as c:
            L.set_state(c, "emergency_lock", "1")
        self.warchest(self.admin, "1, 2")
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))


class TestFailureHandling(Base):
    def fail_on_call(self, n, mode="reject"):
        original = self.pnw.bank_withdraw
        state = {"n": 0}

        async def wrapped(*a, **k):
            state["n"] += 1
            if state["n"] == n:
                self.pnw.withdraw_mode = mode
            else:
                self.pnw.withdraw_mode = "ok"
            return await original(*a, **k)
        self.pnw.bank_withdraw = wrapped

    def test_a_failed_recipient_is_reported_and_the_batch_never_looks_fully_successful(self):
        self.fail_on_call(2)
        i = self.warchest(self.admin, "1, 2")
        b = self.batches()[0]
        self.assertEqual(b["status"], "PARTIAL")
        st = {it["nation_id"]: it["status"] for it in self.items()}
        self.assertEqual(st, {1: "COMPLETED", 2: "FAILED"})
        self.assertIn("Not everything succeeded", i.text())
        with self.db.read() as c:                                                                      # the failed transfer changed no ledger and holds nothing
            self.assertEqual(c.execute("SELECT status FROM transactions WHERE id=?", (self.items()[1]["tx_id"],)).fetchone()[0], "FAILED")
            self.assertEqual(L.holds(c, None, "ALLIANCE"), {})

    def test_an_uncertain_transfer_stops_the_batch_and_is_never_re_sent(self):
        self.fail_on_call(1, "timeout_lost")
        self.warchest(self.admin, "1, 2")
        self.assertEqual(self.pnw.withdraw_calls, 1)                                                   # stopped: recipient 2 was not even attempted
        st = {it["nation_id"]: it["status"] for it in self.items()}
        self.assertEqual((st[1], st[2]), ("UNCERTAIN", "PENDING"))
        self.assertEqual(self.batches()[0]["status"], "HALTED")                                        # work is still waiting: not "complete"
        self.assertIsNotNone(self.items()[0]["tx_id"])                                                 # the uncertain transfer keeps its transaction
        self.fail_on_call(0)                                                                           # PnW is healthy again
        self.call(self.grantset, "resume", self.admin, 1)
        sent_to = lambda: [r["receiver_id"] for r in self.pnw.recs if r["sender_type"] == 2]          # noqa: E731
        self.assertEqual(sent_to(), [])                                                                # the existing safeguard blocks sending while a
        self.assertEqual({it["nation_id"]: it["status"] for it in self.items()}[2], "PENDING")         # transfer is unconfirmed: still waiting, not lost
        # ECON checks PnW, finds nothing was sent, and marks the transfer failed: only then may the rest go
        tx_id = self.items()[0]["tx_id"]
        with self.db.tx() as c:                                                                        # the bank refuses to call a transfer failed within
            c.execute("UPDATE transactions SET attempted_at='2020-01-01T00:00:00Z' WHERE id=?", (tx_id,))   # 5 minutes of the attempt: let time pass
        self.call(self.bank, "resolvetx", self.admin, tx_id, Choice("mark_failed", "mark_failed"))
        self.call(self.grantset, "resume", self.admin, 1)
        self.assertEqual(sent_to().count(1), 0)                                                        # never blindly re-sent to the uncertain recipient
        self.assertEqual(sent_to().count(2), 1)                                                        # the untouched one is sent exactly once
        self.call(self.grantset, "resume", self.admin, 1)
        self.assertEqual(sent_to().count(2), 1)                                                        # resuming again changes nothing
        st = {it["nation_id"]: it["status"] for it in self.items()}
        self.assertEqual((st[1], st[2]), ("FAILED", "COMPLETED"))
        self.assertEqual(self.batches()[0]["status"], "PARTIAL")

    def test_flagged_nations_are_left_out_and_listed_not_silently_skipped(self):
        self.nations[2] = nation(2, 12, cities=[city(None, 500, cid=i + 1) for i in range(12)])       # PnW gave no infrastructure levels
        self.nations[3] = nation(3, 6, alliance=999)                                                   # not in this alliance
        i = self.call(self.grant, "infrastructure", self.admin, "1, 2, 3", ALLI, target_infrastructure=1500)
        st = {it["nation_id"]: it["status"] for it in self.items()}
        self.assertEqual(st, {1: "COMPLETED", 2: "REVIEW", 3: "REVIEW"})
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertEqual(self.batches()[0]["status"], "PARTIAL")
        self.assertIn("review", i.text().lower())
        self.assertIn("not currently in this alliance", self.items()[2]["message"])

    def test_pnw_unreadable_means_nothing_is_calculated_or_sent(self):
        from tunbank.pnw import PnWRejected

        async def boom(ids):
            raise PnWRejected("Cannot query field \"population\" on type \"Nation\"")
        self.pnw.fetch_nations_detail = boom
        i = self.warchest(self.admin, "1")
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.assertIn("nothing was calculated or sent", i.text())

    def test_nations_missing_from_pnw_are_flagged(self):
        del self.nations[2]
        self.warchest(self.admin, "1, 2")
        st = {it["nation_id"]: it["status"] for it in self.items()}
        self.assertEqual(st, {1: "COMPLETED", 2: "REVIEW"})
        self.assertIn("did not return", self.items()[1]["message"])


class TestDuplicateProtection(Base):
    def test_the_same_command_twice_never_pays_twice(self):
        self.warchest(self.admin, "1, 2")
        self.assertEqual(self.pnw.withdraw_calls, 2)
        i = self.warchest(self.admin, "1, 2")
        self.assertEqual(self.pnw.withdraw_calls, 2)
        self.assertIn("already created", i.text())
        self.assertEqual(len(self.batches()), 1)
        self.warchest(self.admin, "1, 2", note="a genuinely new request")
        self.assertEqual(self.pnw.withdraw_calls, 4)

    def test_one_confirmed_request_can_only_ever_create_one_batch(self):
        calcs = [PR.compute_warchest(nation(1, 12), M.parse_amounts("food=1000"), 1)]
        with self.db.tx() as c:
            first = DB.create_batch(c, kind="WARCHEST_GRANT", calcs=calcs, source=DB.Source("ALLIANCE", description="x"), actor="1", note="n",
                                    params={}, rules_version="v", paying_bank="main", val=None, snap=None, approval_id=None, request_key="same-key")
            again = DB.create_batch(c, kind="WARCHEST_GRANT", calcs=calcs, source=DB.Source("ALLIANCE", description="x"), actor="1", note="n2",
                                    params={}, rules_version="v", paying_bank="main", val=None, snap=None, approval_id=None, request_key="same-key")
        self.assertIsNotNone(first)
        self.assertIsNone(again)

    def test_data_that_changes_between_preview_and_confirmation_sends_nothing(self):
        state = {"n": 0}

        async def fetch(ids):
            state["n"] += 1
            n = 12 if state["n"] == 1 else 13                                                           # the nation bought a city meanwhile
            return {i: nation(i, n) for i in ids}
        self.pnw.fetch_nations_detail = fetch
        i = self.warchest(self.admin, "1")
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.assertIn("changed since the preview", i.text())

    def test_the_database_forbids_rewriting_what_was_calculated_or_deleting_it(self):
        import sqlite3
        self.warchest(self.admin, "1")
        with self.db.tx() as c:
            for sql in ("UPDATE disbursement_items SET amounts_json='{}'", "UPDATE disbursement_items SET calculation_json='x'",
                        "UPDATE disbursement_items SET nation_id=2", "UPDATE disbursement_batches SET totals_json='{}'",
                        "UPDATE disbursement_batches SET funding_source='MEMBER_AVAILABLE'", "DELETE FROM disbursement_items",
                        "DELETE FROM disbursement_batches"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    c.execute(sql)
            c.execute("UPDATE disbursement_items SET message='ok'")                                      # the outcome itself may be updated


class TestMemberSources(Base):
    def test_member_funded_debits_each_recipients_own_available_and_never_carries_the_ignore_note(self):
        before = self.bal(1)
        self.call(self.grant, "warchest", self.admin, "1", MEM_A, ratio=0.1)                              # 1,200 food, 120 steel, 120 aluminum
        after = self.bal(1)
        self.assertEqual(before["food"] - after["food"], 120000)
        self.assertEqual(before["steel"] - after["steel"], 12000)
        b = self.batches()[0]
        self.assertEqual(b["funding_source"], "MEMBER_AVAILABLE")
        self.assertIn("MEMBER funds", b["source_description"])
        self.assertIn("not alliance money", b["source_description"])
        self.assertNotIn("#ignore", self.pnw.recs[-1]["note"].lower())
        self.assertEqual(self.items()[0]["source_member_id"], 1)

    def test_an_insufficient_member_balance_blocks_everything(self):
        before = self.bal(1)
        i = self.call(self.grant, "warchest", self.admin, "1", MEM_A, ratio=5)                            # 60,000 food > the 50,000 held
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.assertEqual(self.bal(1), before)
        self.assertIn("blocked", i.text().lower())

    def test_a_recipient_without_a_tun_account_cannot_be_the_source(self):
        i = self.call(self.grant, "warchest", self.admin, "3", MEM_A)
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.assertIn("no TUN Bank account", i.text())

    def test_ordinary_econ_cannot_spend_members_money_without_the_separate_permission(self):
        self.make_minister()
        i = self.call(self.grant, "warchest", self.econ2, "1", MEM_A, ratio=0.1)
        self.assertIn("disburse_member_available", i.text())
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.make_minister(["disburse_member_available"])
        self.call(self.grant, "warchest", self.econ2, "1", MEM_A, ratio=0.1)
        self.assertEqual(self.pnw.withdraw_calls, 1)
        # alliance funds need only the ordinary ECON level
        self.call(self.grant, "warchest", self.econ2, "2", ALLI, ratio=0.1)
        self.assertEqual(self.pnw.withdraw_calls, 2)

    def test_non_econ_cannot_use_any_of_these_commands(self):
        self.assertIn("permission", self.warchest(self.alice, "1").text().lower())
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_locked_funds_need_their_own_permission_the_setting_and_one_named_lock(self):
        self.call(self.bank, "reserve", self.admin, 1, "food=20000 steel=2000 aluminum=2000", "WARCHEST", "war prep")
        lock_id = self.rows("SELECT id FROM locks")[0]["id"]
        self.make_minister(["disburse_member_locked"])
        i = self.call(self.grant, "warchest", self.econ2, "1", MEM_L, lock_id=lock_id, ratio=0.1)
        self.assertIn("switched off", i.text())                                                           # econ_locked_withdraw_enabled is off
        self.cfg("econ_locked_withdraw_enabled", "1")
        i = self.call(self.grant, "warchest", self.econ2, "1, 2", MEM_L, lock_id=lock_id, ratio=0.1)
        self.assertEqual(self.pnw.withdraw_calls, 0)                                                      # locked funds pay ONE recipient only
        i = self.call(self.grant, "warchest", self.econ2, "2", MEM_L, lock_id=lock_id, ratio=0.1)
        self.assertEqual(self.pnw.withdraw_calls, 0)                                                      # that lock is nation 1's, not nation 2's
        before = self.bal(1, "LOCKED")
        i = self.call(self.grant, "warchest", self.econ2, "1", MEM_L, lock_id=lock_id, ratio=0.1)
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertEqual(before["food"] - self.bal(1, "LOCKED")["food"], 120000)
        self.assertEqual(self.batches()[-1]["funding_source"], "MEMBER_LOCKED")

    def test_a_locked_source_without_a_lock_id_is_refused(self):
        self.cfg("econ_locked_withdraw_enabled", "1")
        self.make_minister(["disburse_member_locked"])
        self.assertIn("lock_id", self.call(self.grant, "warchest", self.econ2, "1", MEM_L).text())

    def test_naming_a_member_with_alliance_funds_is_refused(self):
        self.assertIn("matching source", self.call(self.grant, "warchest", self.admin, "1", ALLI, source_member="2").text())


class TestApprovals(Base):
    def test_large_disbursements_need_a_second_person_and_the_approval_is_bound_to_the_plan(self):
        self.cfg("approval_threshold_value", "1000")
        self.make_minister()
        i = self.warchest(self.admin, "1")
        self.assertIn("Approval request", i.text())
        self.assertEqual((self.pnw.withdraw_calls, self.batches()), (0, []))
        self.assertIn("different staff member", self.call(self.bank, "approve", self.admin, 1).text())
        self.assertIn("Approved", self.call(self.bank, "approve", self.econ2, 1).text())
        self.warchest(self.admin, "1")
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertIsNotNone(self.batches()[0]["approval_id"])
        # the approval was for nation 1 only: a different plan needs its own
        i = self.warchest(self.admin, "1, 2", note="different plan")
        self.assertIn("Approval request", i.text())
        self.assertEqual(self.pnw.withdraw_calls, 1)


class TestOtherCommands(Base):
    def test_raws_end_to_end_with_the_calculated_amounts(self):
        self.nations[1] = nation(1, 1, cities=[city(1000, 500, coal_power=2, steel_mill=1, farm=1)], population=100000)
        self.nations[1]["military"]["soldiers"] = 10000
        i = self.call(self.xfer, "raws", self.admin, "1", 2.0, ALLI)
        self.assertEqual(self.pnw.withdraw_calls, 1)
        it = self.items()[0]
        self.assertEqual(it["kind"], "RAWS")
        self.assertEqual(json.loads(it["amounts_json"]), {"coal": 3000, "iron": 600, "food": 24000, "money": 3840000})
        self.assertEqual(self.batches()[0]["kind"], "RAWS")

    def test_raws_no_cash_only_missing_and_the_daily_cash_rule(self):
        self.nations[1] = nation(1, 1, cities=[city(1000, 500, coal_power=2, steel_mill=1, farm=1)], population=100000,
                                 stock=stock(coal=10, money=400000))
        self.nations[1]["military"]["soldiers"] = 10000
        self.cfg("daily_cash_threshold", "410000")
        self.call(self.xfer, "raws", self.admin, "1", 1.0, ALLI, no_cash=True, only_missing=True, note="a")
        a = json.loads(self.items(1)[0]["amounts_json"])
        self.assertNotIn("money", a)
        self.assertEqual(a["coal"], 500)                                                                   # 15 - 10 held = 5
        self.call(self.xfer, "raws", self.admin, "1", 1.0, ALLI, no_daily_cash=True, note="b")
        b = json.loads(self.items(2)[0]["amounts_json"])
        self.assertEqual(b["money"], 1000000)                                                              # capped at 410,000 - 400,000 = $10,000

    def test_raws_validates_days(self):
        for days in (0, -1, 31):
            self.assertIn("Days must", self.call(self.xfer, "raws", self.admin, "1", float(days), ALLI).text())
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_city_grant_with_cap_and_single_nation_rule(self):
        self.call(self.grant, "city", self.admin, "1", 1, ALLI)
        self.assertEqual(json.loads(self.items()[0]["amounts_json"]), {"money": 6842500000})
        self.assertEqual(self.batches()[0]["kind"], "CITY_GRANT")
        self.assertIn("ONE nation", self.call(self.grant, "city", self.admin, "1, 2", 1, ALLI).text())
        self.cfg("grant_city_max_usd", "1000000")
        i = self.call(self.grant, "city", self.admin, "2", 1, ALLI, note="capped")
        self.assertEqual(len(self.batches()), 1)                                                           # over the cap: flagged, nothing to send
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertIn("cap", i.text())

    def test_infrastructure_and_land_use_the_configured_standard_policy(self):
        self.nations[1] = nation(1, 2, cities=[city(1000, 500, cid=1), city(1000, 500, cid=2)])
        self.assertIn("No sensible infrastructure target", self.call(self.grant, "infrastructure", self.admin, "1", ALLI).text())
        self.call(self.grantset, "infra", self.admin, 1100)
        self.call(self.grant, "infrastructure", self.admin, "1", ALLI)
        it = self.items(1)[0]
        self.assertEqual(it["kind"], "INFRA_GRANT")
        self.assertEqual(json.loads(it["calculation_json"])["inputs"]["target"], 1100)
        self.call(self.grantset, "land", self.admin, 600)
        self.call(self.grant, "land", self.admin, "1", ALLI)
        self.assertEqual(self.items(2)[0]["kind"], "LAND_GRANT")
        self.assertEqual(self.items(2)[0]["status"], "COMPLETED")

    def test_warchest_needs_a_configuration_and_ecn_can_change_it(self):
        self.cfg("warchest_per_city", "")
        self.assertIn("/grantset warchest", self.warchest(self.admin, "1").text())
        self.make_minister()
        self.call(self.grantset, "warchest", self.econ2, "food=2000 steel=50")
        self.warchest(self.admin, "1")
        self.assertEqual(json.loads(self.items()[0]["amounts_json"]), {"food": 2400000, "steel": 60000})
        self.assertIn("Not changed", self.call(self.grantset, "warchest", self.admin, "food=lots").text())
        self.assertIn("permission", self.call(self.grantset, "warchest", self.alice, "food=1").text().lower())

    def test_alliance_keyword_and_recipient_limit(self):
        self.pnw.members = {1: "Alpha", 2: "Beta", 3: "Gamma"}
        self.cfg("disburse_max_nations", "2")
        i = self.warchest(self.admin, "alliance")
        self.assertIn("at most 2", i.text())
        self.assertEqual(self.pnw.withdraw_calls, 0)
        self.cfg("disburse_max_nations", "50")
        self.warchest(self.admin, "alliance")
        self.assertEqual(self.pnw.withdraw_calls, 3)


class TestGrantsetAdmin(Base):
    def test_live_switch_is_admin_only_confirmed_and_audited(self):
        self.set_live()
        self.make_minister()
        self.assertIn("Admin", self.call(self.grantset, "live", self.econ2, Choice("x", "WARCHEST_GRANT"), Choice("on", "on")).text())
        self.call(self.grantset, "live", self.admin, Choice("x", "WARCHEST_GRANT"), Choice("on", "on"), auto_confirm=False)
        with self.db.read() as c:
            self.assertFalse(DB.is_live(c, "WARCHEST_GRANT"))
        self.call(self.grantset, "live", self.admin, Choice("x", "WARCHEST_GRANT"), Choice("on", "on"))
        with self.db.read() as c:
            self.assertTrue(DB.is_live(c, "WARCHEST_GRANT"))
            self.assertFalse(DB.is_live(c, "RAWS"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='DISBURSE_LIVE'").fetchone()[0], 1)
        self.call(self.grantset, "live", self.admin, Choice("x", "WARCHEST_GRANT"), Choice("off", "off"))
        with self.db.read() as c:
            self.assertFalse(DB.is_live(c, "WARCHEST_GRANT"))

    def test_everything_defaults_to_preview_only(self):
        from tunbank.config import DEFAULTS
        self.assertEqual(DEFAULTS["disburse_live_kinds"][0], "")

    def test_show_rules_and_batch_views(self):
        self.warchest(self.admin, "1")
        self.assertIn("Allowed to SEND", self.call(self.grantset, "show", self.admin).text())
        self.assertIn("member tax accounts", self.call(self.grantset, "show", self.admin).text())
        i = self.call(self.grantset, "rules", self.admin)
        self.assertIn("not verified", i.text())
        self.assertTrue(any(m.get("file") is not None for m in i.sent))
        self.assertIn("Warchest grant #1", self.call(self.grantset, "batch", self.admin, 1).text())
        self.assertIn("does not exist", self.call(self.grantset, "batch", self.admin, 99).text())

    def test_the_new_permission_flags_exist_and_are_assignable(self):
        from tunbank import perms
        self.assertIn("disburse_member_available", perms.FLAGS)
        self.assertIn("disburse_member_locked", perms.FLAGS)


class TestSourceDescription(unittest.TestCase):
    def test_signature_changes_when_anything_to_be_sent_changes(self):
        a = PR.compute_warchest(nation(1, 12), M.parse_amounts("food=1000"), 1)
        b = PR.compute_warchest(nation(1, 13), M.parse_amounts("food=1000"), 1)
        self.assertNotEqual(DB.signature([a]), DB.signature([b]))
        self.assertEqual(DB.signature([a]), DB.signature([PR.compute_warchest(nation(1, 12), M.parse_amounts("food=1000"), 1)]))


if __name__ == "__main__":
    unittest.main()
