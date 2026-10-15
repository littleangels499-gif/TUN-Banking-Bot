"""Readable /bankset config, /bankset resetconfig, and the migration verification tools."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_commands as TC  # noqa: E402

from tunbank import backup as BK  # noqa: E402
from tunbank import config_reset as CRST  # noqa: E402
from tunbank import config_view as CV  # noqa: E402
from tunbank import credentials as CRED  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank.config import DEFAULTS, cfg_bool, cfg_get, cfg_set  # noqa: E402

Choice = TC.discord.app_commands.Choice


class Base(TC.Cmd):
    """Gives these tests the real bot fixture without re-running the whole Cmd suite (it already runs in test_commands)."""


for _name in dir(TC.Cmd):
    if _name.startswith("test_"):
        setattr(Base, _name, None)


class TestSettingsLayer(unittest.TestCase):
    def test_every_setting_has_a_friendly_description_and_displays(self):
        for key, (default, _desc) in DEFAULTS.items():
            self.assertIn(key, CV.SETTINGS)
            self.assertTrue(CV.human(key, default))
            self.assertNotEqual(CV.SETTINGS[key].label, key)

    def test_values_are_readable_not_codes(self):
        self.assertEqual(CV.human("dm_members", "1"), "✅ On")
        self.assertEqual(CV.human("dm_members", "0"), "❌ Off")
        self.assertEqual(CV.human("scan_interval_seconds", "120"), "2 minutes")
        self.assertEqual(CV.human("large_credit_value", "2000000000"), "$2,000,000,000")
        self.assertEqual(CV.human("offshore_access", "STAFF"), "Staff (Bankers and above)")
        self.assertEqual(CV.human("econ_log_channel_id", ""), "Not set")
        self.assertEqual(CV.human("econ_log_channel_id", "123456"), "<#123456>")
        self.assertEqual(CV.human("approval_threshold_value", "0"), "Off")
        self.assertEqual(CV.human("trade_price_upper", "10"), "10×")

    def test_input_is_validated_and_typos_are_refused(self):
        self.assertEqual(CV.parse_value("dm_members", "off"), "0")
        self.assertEqual(CV.parse_value("approval_expiry_minutes", "2h"), "120")
        self.assertEqual(CV.parse_value("price_stale_seconds", "5m"), "300")
        self.assertEqual(CV.parse_value("approval_threshold_value", "5m"), "5000000")
        self.assertEqual(CV.parse_value("econ_log_channel_id", "<#123456789>"), "123456789")
        self.assertEqual(CV.parse_value("grant_min_level", "admin"), "ADMIN")
        self.assertEqual(CV.parse_value("external_withdrawal_exempt_nations", "12, 34"), "12,34")
        for key, bad in (("dm_members", "banana"), ("approval_expiry_minutes", "soon"), ("grant_min_level", "king"),
                         ("econ_log_channel_id", "abc"), ("offshore_keep_in_main", "money=lots"), ("scan_interval_seconds", "-5")):
            with self.assertRaises(ValueError, msg=f"{key}={bad}"):
                CV.parse_value(key, bad)

    def test_a_setting_can_be_found_by_label_or_code(self):
        self.assertEqual(CV.find_key("Members can withdraw"), "self_withdraw_enabled")
        self.assertEqual(CV.find_key("self_withdraw_enabled"), "self_withdraw_enabled")
        self.assertEqual(CV.find_key("members can with"), "self_withdraw_enabled")
        self.assertIsNone(CV.find_key("nonsense"))


class TestConfigCommand(Base):
    def test_overview_and_pages_use_labels_not_raw_codes(self):
        i = self.call(self.bankset, "config", self.admin)
        self.assertIn("Bank settings", i.text())
        self.assertIn("settings", i.text())
        page = self.call(self.bankset, "config", self.admin, "", "", Choice("Member features", "Member features")).text()
        self.assertIn("Members can withdraw", page)
        self.assertIn("✅ On", page)
        self.assertNotIn("self_withdraw_enabled", page)
        self.assertNotIn("`1`", page)

    def test_change_by_label_shows_before_and_after(self):
        i = self.call(self.bankset, "config", self.admin, "Members can withdraw", "off")
        self.assertIn("✅ On", i.text())
        self.assertIn("❌ Off", i.text())
        with self.db.read() as c:
            self.assertFalse(cfg_bool(c, "self_withdraw_enabled"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='CONFIG_CHANGED'").fetchone()[0], 1)

    def test_bad_value_changes_nothing_and_explains(self):
        i = self.call(self.bankset, "config", self.admin, "dm_members", "banana")
        self.assertIn("Not changed", i.text())
        self.assertIn("on", i.text().lower())
        with self.db.read() as c:
            self.assertTrue(cfg_bool(c, "dm_members"))

    def test_unknown_setting_and_gate_redirect(self):
        self.assertIn("don't know", self.call(self.bankset, "config", self.admin, "zzz", "1").text())
        i = self.call(self.bankset, "config", self.admin, "reconcile_gate_enabled", "off")
        self.assertIn("/bankset gate", i.text())
        with self.db.read() as c:
            self.assertTrue(L.reconcile_gate_enabled(c))

    def test_non_admin_cannot_use_it(self):
        self.assertIn("Admin", self.call(self.bankset, "config", self.alice, "dm_members", "off").text())


class TestResetConfig(Base):
    PHRASE = CRST.RESET_PHRASE

    def setUp(self):
        super().setUp()
        self.fund_alice({"money": 1000})
        with self.db.tx() as c:
            cfg_set(c, "econ_log_channel_id", "111", "t")
            cfg_set(c, "dm_members", "0", "t")
            c.execute("INSERT INTO bankers(discord_id,added_by,added_at) VALUES('55','x','x')")
            c.execute("INSERT INTO limits(scope,scope_id,per_tx_cents,daily_cents) VALUES('GLOBAL','*',100,200)")
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('MINISTER','77')")

    def protected(self):
        with self.db.read() as c:
            return {t: CRST._count(c, t) for t in CRST.PROTECTED}, c.execute(
                "SELECT nation_id,bucket,resource,amount FROM balances ORDER BY 1,2,3").fetchall()

    def reset(self, who, scope, phrase=None, reason="starting over"):
        return self.call(self.bankset, "resetconfig", who, Choice(scope, scope), reason, phrase or self.PHRASE)

    def test_the_server_owner_may_reset_even_without_an_admin_role(self):
        import fake_discord as FD
        cls = getattr(FD, "FakeInteraction", None) or getattr(FD.discord, "Interaction")
        cls.guild = type("G", (), {"owner_id": self.alice.id})()
        try:
            i = self.reset(self.alice, "settings")
        finally:
            del cls.guild
        self.assertIn("Configuration reset", i.text())
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "econ_log_channel_id"), "")

    def test_a_full_reset_is_refused_when_it_would_lock_everyone_out(self):
        self.settings.owner_ids.clear()
        with self.db.tx() as c:
            c.execute("INSERT INTO role_permissions(level,role_id) VALUES('ADMIN','99')")
        self.admin.roles = [TC.discord.Role(99)]
        i = self.reset(self.admin, "all")
        self.assertIn("nobody able to administer", i.text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM role_permissions").fetchone()[0], 2)

    def test_wrong_phrase_or_non_admin_changes_nothing(self):
        before = self.protected()
        self.assertIn("Nothing was changed", self.reset(self.admin, "settings", "yes please").text())
        self.assertIn("Admin", self.reset(self.alice, "settings").text())
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "econ_log_channel_id"), "111")
        self.assertEqual(self.protected(), before)

    def test_cancelled_confirmation_changes_nothing(self):
        i = self.call(self.bankset, "resetconfig", self.admin, Choice("settings", "settings"), "x", self.PHRASE, auto_confirm=False)
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "econ_log_channel_id"), "111")

    def test_settings_only_reset_keeps_roles_bankers_limits_and_all_money(self):
        before_prot, before_bal = self.protected()
        i = self.reset(self.admin, "settings")
        self.assertIn("Configuration reset", i.text())
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "econ_log_channel_id"), "")
            self.assertTrue(cfg_bool(c, "dm_members"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bank_config").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bankers").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM limits").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM role_permissions").fetchone()[0], 1)
        after_prot, after_bal = self.protected()
        for t in CRST.PROTECTED:
            if t in CRST.APPEND_ONLY:
                self.assertGreaterEqual(after_prot[t], before_prot[t], t)
            else:
                self.assertEqual(after_prot[t], before_prot[t], t)
        self.assertEqual(after_bal, before_bal)
        self.assertTrue(list(self.settings.backup_dir.glob("pre-config-reset-*.db")))      # safety backup was taken first

    def test_full_reset_clears_config_tables_only_and_records_what_it_removed(self):
        before_prot, before_bal = self.protected()
        self.reset(self.admin, "all")
        with self.db.read() as c:
            for t in ("bank_config", "bankers", "limits", "role_permissions"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0, t)
            self.assertTrue(L.verify_chain(c, "ledger_entries")["ok"])
            self.assertTrue(L.verify_chain(c, "audit_log")["ok"])
            a = c.execute("SELECT details_json FROM audit_log WHERE action='CONFIG_RESET'").fetchone()
            self.assertIsNotNone(a)
            self.assertIn("77", a["details_json"])                                          # the removed role mapping is recoverable
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM config_audit WHERE action='CONFIG_RESET'").fetchone()[0], 2)
        after_prot, after_bal = self.protected()
        self.assertEqual(after_bal, before_bal)
        for t in ("members", "ledger_entries", "balances", "pnw_records", "member_credentials", "loans", "transactions"):
            self.assertEqual(after_prot[t], before_prot[t], t)

    def test_system_state_such_as_the_emergency_lock_is_not_reset(self):
        with self.db.tx() as c:
            L.set_state(c, "emergency_lock", "1")
            L.set_state(c, "emergency_reason", "testing")
        self.reset(self.admin, "all")
        with self.db.read() as c:
            self.assertEqual(L.get_state(c, "emergency_lock"), "1")

    def test_if_anything_protected_would_change_the_whole_reset_is_rolled_back(self):
        CRST.SCOPES["bad"] = ("bank_config", "members")
        CRST.DESCRIPTION["members"] = "members (test)"
        try:
            with self.assertRaises(CRST.ResetError):
                with self.db.tx() as c:
                    CRST.reset(c, scope="bad", actor="1", reason="x")
        finally:
            del CRST.SCOPES["bad"]
            del CRST.DESCRIPTION["members"]
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "econ_log_channel_id"), "111")                       # even the settings part was rolled back
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)


class FakeEmoji:
    def __init__(self, name, eid):
        self.name, self.id, self.animated = name, eid, False


class TestIconPack(Base):
    def setUp(self):
        super().setUp()
        from tunbank import icons
        self.icons = icons
        self.addCleanup(lambda: icons.load({}))
        self.emojis = [FakeEmoji("pnw_money", 111111111111111111), FakeEmoji("pnw_oil", 222222222222222222),
                       FakeEmoji("pnw_bogus", 3), FakeEmoji("unrelated", 4)]

        class Bot:
            async def fetch_application_emojis(inner):
                if getattr(self, "offline", False):
                    raise RuntimeError("offline")
                return self.emojis
        self.svc.alerts.bot = Bot()

    def test_pack_is_found_by_name_and_ignores_everything_else(self):
        found = self.icons.pack_from(self.emojis)
        self.assertEqual(found, {"money": "<:pnw_money:111111111111111111>", "oil": "<:pnw_oil:222222222222222222>"})

    def test_apply_uses_the_pack_and_falls_back_to_defaults_for_the_rest(self):
        i = self.call(self.bankset, "iconpack", self.admin, Choice("apply", "apply"))
        self.assertIn("Icons updated", i.text())
        self.assertEqual(self.icons.resource("money"), "<:pnw_money:111111111111111111>")
        self.assertEqual(self.icons.resource("food"), self.icons.DEFAULT_RESOURCE_ICONS["food"])      # no pnw_food uploaded: default
        self.assertIn("Food", i.text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='ICON_PACK'").fetchone()[0], 1)

    def test_revert_restores_the_defaults(self):
        self.call(self.bankset, "iconpack", self.admin, Choice("apply", "apply"))
        self.call(self.bankset, "iconpack", self.admin, Choice("revert", "revert"))
        self.assertEqual(self.icons.resource("money"), self.icons.DEFAULT_RESOURCE_ICONS["money"])
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "icon_money"), "")

    def test_nothing_uploaded_or_discord_unreachable_changes_nothing(self):
        self.emojis = []
        self.assertIn("No PnW icons", self.call(self.bankset, "iconpack", self.admin, Choice("apply", "apply")).text())
        self.emojis, self.offline = [FakeEmoji("pnw_money", 111111111111111111)], True
        self.assertIn("couldn't read", self.call(self.bankset, "iconpack", self.admin, Choice("apply", "apply")).text())
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "icon_money"), "")

    def test_a_deleted_pack_emoji_is_detected_so_the_default_can_be_used(self):
        configured = {"money": "<:pnw_money:111111111111111111>", "oil": "<:pnw_oil:999999999999999999>", "food": "", "iron": "🔩"}
        self.assertEqual(self.icons.stale_pack_icons(configured, self.emojis), ["oil"])

    def test_non_admin_cannot_change_icons(self):
        self.assertIn("Admin", self.call(self.bankset, "iconpack", self.alice, Choice("apply", "apply")).text())


class TestMigrationVerification(Base):
    def backup(self, name):
        dest = Path(tempfile.mkdtemp()) / name
        self.db.backup_to(dest)
        return dest

    def test_a_copy_is_proven_identical_and_a_change_is_detected(self):
        self.fund_alice({"money": 1000})
        a, b = self.backup("a.db"), self.backup("b.db")
        ia, ib = BK.inventory(a), BK.inventory(b)
        self.assertEqual(ia["integrity_check"], "ok")
        self.assertTrue(ia["ledger_chain_ok"] and ia["audit_chain_ok"])
        self.assertEqual(ia["balances_not_matching_ledger"], 0)
        self.assertEqual(ia["fingerprint"], ib["fingerprint"])
        self.assertEqual(BK.compare(ia, ib), [])
        with self.db.tx() as c:
            cfg_set(c, "dm_members", "0", "t")
        ic = BK.inventory(self.backup("c.db"))
        self.assertNotEqual(ia["fingerprint"], ic["fingerprint"])
        self.assertTrue(any("bank_config" in d or "audit" in d for d in BK.compare(ia, ic)))

    def test_stored_member_keys_need_the_same_encryption_key(self):
        from cryptography.fernet import Fernet
        key, other = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self.fund_alice({"money": 100})
        with self.db.tx() as c:
            CRED.save(c, CRED.Crypto(key), nation_id=1, discord_id="900", api_key="abcdefghijklmnop1234", verified=True)
        path = self.backup("k.db")
        good = BK.check_credentials(path, key)
        bad = BK.check_credentials(path, other)
        none = BK.check_credentials(path, "")
        self.assertEqual((good["stored"], good["readable"], good["unreadable"]), (1, 1, 0))
        self.assertEqual((bad["readable"], bad["unreadable"]), (0, 1))
        self.assertFalse(none["key_configured"])
        self.assertNotIn("abcdefghijklmnop1234", repr(good) + repr(bad) + repr(BK.inventory(path)))     # never exposes a key

    def test_verdict_strict_compatible_or_different(self):
        self.fund_alice({"money": 1000})
        old = self.backup("old.db")
        same = self.backup("same.db")
        v = BK.verdict(old, same)
        self.assertTrue(v["strict"] and v["compatible"])
        self.pnw.recs.append(TC.rec(901, 1, {"money": 5}))                      # newer activity happens after the move
        self.pnw.holdings = {"money": 150000 + 500}
        self.run_async(self.svc.scanner.scan())
        newer = self.backup("newer.db")
        v = BK.verdict(old, newer)
        self.assertFalse(v["strict"])
        self.assertTrue(v["compatible"], v["reasons"])                          # old history intact, only appended to
        self.assertEqual(BK.history_preserved(old, newer), {"ledger_entries": True, "audit_log": True, "config_audit": True})
        with self.db.tx() as c:
            cfg_set(c, "dm_members", "0", "t")                                  # a setting changed -> not a faithful copy
        v = BK.verdict(old, self.backup("changed.db"))
        self.assertFalse(v["compatible"])
        self.assertTrue(any("bank_config" in r for r in v["reasons"]))

    def test_an_unrelated_database_is_never_called_compatible(self):
        self.fund_alice({"money": 1000})
        old = self.backup("a.db")
        other = Path(tempfile.mkdtemp()) / "b.db"
        from tunbank.db import Database
        fresh = Database(Path(tempfile.mkdtemp()) / "fresh.db")
        fresh.migrate(backup_dir=Path(tempfile.mkdtemp()))
        fresh.backup_to(other)
        v = BK.verdict(old, other)
        self.assertFalse(v["compatible"])
        self.assertTrue(any("smaller" in r or "only" in r or "NOT contained" in r for r in v["reasons"]))

    def test_verify_script_runs_and_reports(self):
        import subprocess
        self.fund_alice({"money": 100})
        a, b = self.backup("s1.db"), self.backup("s2.db")
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "verify_database.py"), str(a),
                            "--compare", str(b)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("IDENTICAL", r.stdout)
        self.assertIn("RESULT          : PASS", r.stdout)



class TestGuildLock(unittest.TestCase):
    def test_commands_from_other_servers_are_refused_when_a_home_server_is_set(self):
        from types import SimpleNamespace as NS

        from tunbank.ui import guild_allowed
        home = NS(guild_id=555)
        self.assertTrue(guild_allowed(home, NS(guild_id=555)))
        self.assertFalse(guild_allowed(home, NS(guild_id=999)))
        self.assertFalse(guild_allowed(home, NS(guild_id=None)))               # DMs / unknown
        self.assertTrue(guild_allowed(NS(guild_id=None), NS(guild_id=999)))     # no GUILD_ID configured: unchanged behaviour


if __name__ == "__main__":
    unittest.main()
