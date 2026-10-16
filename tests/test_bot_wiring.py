"""Static guard: bot.py must never use a `self.<attribute>` that nothing defines.

A background loop once called `self.pnw`, which does not exist (the client lives at `self.svc.pnw`), so nation names never
refreshed and the same error was logged forever. Only genuine discord.py attributes may be used without being defined here.
"""
import ast
import unittest
from pathlib import Path

DISCORD_OWN = {"add_view", "tree", "user", "wait_until_ready", "get_channel", "get_guild", "get_user", "fetch_channel",
               "fetch_user", "guilds", "loop", "http", "is_closed", "wait_for", "change_presence", "latency", "application_id",
               "tree_cls", "setup_hook", "close", "start", "run", "fetch_application_emojis"}


class TestBotWiring(unittest.TestCase):
    def test_every_self_attribute_in_bot_is_defined(self):
        tree = ast.parse((Path(__file__).resolve().parent.parent / "tunbank" / "bot.py").read_text())
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            defined = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            defined |= {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
            used, assigned = set(), set()
            for n in ast.walk(cls):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    (assigned if isinstance(n.ctx, ast.Store) else used).add(n.attr)
            undefined = used - assigned - defined - DISCORD_OWN
            self.assertEqual(undefined, set(), f"{cls.name} uses attributes nothing defines: {sorted(undefined)}")


class TestSecretsNeverPrint(unittest.TestCase):
    """A failing test once printed live bank keys because the settings objects showed their secrets in repr()."""

    def test_no_secret_appears_in_repr_str_or_format(self):
        import os
        import tempfile

        from tunbank.config import BankAccess, load_settings
        secrets = {"DISCORD_TOKEN": "tok-SECRET-111", "PNW_API_KEY": "pnwkey-SECRET-222", "PNW_BOT_KEY": "botkey-SECRET-333",
                   "PNW_BOT_KEY_API_KEY": "botapi-SECRET-444", "OFFSHORE_API_KEY": "offapi-SECRET-555", "OFFSHORE_BOT_KEY": "offbot-SECRET-666",
                   "MAIN_BOT_KEY": "mainbot-SECRET-777", "MAIN_BOT_API_KEY": "mainapi-SECRET-888", "CREDENTIAL_ENCRYPTION_KEY": "credkey-SECRET-999"}
        env = {"OWNER_DISCORD_IDS": "1", "ALLIANCE_ID": "10", "OFFSHORE_ALLIANCE_ID": "20", "DATA_DIR": tempfile.mkdtemp(), **secrets}
        import dotenv
        saved, old_load = dict(os.environ), dotenv.load_dotenv
        try:
            dotenv.load_dotenv = lambda *a, **k: False
            os.environ.clear()
            os.environ.update(env)
            s = load_settings()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            dotenv.load_dotenv = old_load
        shown = " ".join([repr(s), str(s), f"{s}", repr(s.offshore), repr(s.main), repr(s.banks), repr(BankAccess("x", 1, "k1-SECRET", "k2-SECRET", "k3-SECRET"))])
        for name, value in list(secrets.items()) + [("k1", "k1-SECRET"), ("k2", "k2-SECRET"), ("k3", "k3-SECRET")]:
            self.assertNotIn(value, shown, f"{name} would be printed")


if __name__ == "__main__":
    unittest.main()
