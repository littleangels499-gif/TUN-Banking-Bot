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


if __name__ == "__main__":
    unittest.main()
