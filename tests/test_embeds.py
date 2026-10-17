"""Discord rejects an embed whose total text is over 6000 characters (a live /bank holdings crash). Cards must be split, never fail."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_discord as FD  # noqa: E402
import test_commands as TC  # noqa: E402

from tunbank import alerts as A  # noqa: E402
from tunbank import icons  # noqa: E402
from tunbank.ui import reply  # noqa: E402

RES = ["money", "food", "coal", "oil", "uranium", "iron", "bauxite", "lead", "gasoline", "munitions", "steel", "aluminum"]


def monster(n=20, size=1000):
    c = A.Card("Big card", "d" * 300)
    for i in range(n):
        c.add(f"Field {i}", "x" * size, i % 2 == 0)
    return c


class TestSplit(unittest.TestCase):
    def test_a_small_card_is_untouched(self):
        c = A.Card("Small", "hello")
        c.add("a", "b")
        self.assertEqual(A.split_card(c), [c])

    def test_a_huge_card_is_split_without_losing_anything(self):
        c = monster()
        parts = A.split_card(c)
        self.assertGreater(len(parts), 2)
        self.assertEqual([f for p in parts for f in p.fields], c.fields)                 # every field, in order
        for p in parts:
            self.assertLessEqual(A.card_chars(p), 6000)
            self.assertLessEqual(len(p.fields), 24)
        self.assertEqual(parts[0].description, c.description)
        self.assertIn("part 1 of", parts[0].footer)

    def test_to_embed_can_never_produce_an_invalid_embed(self):
        for card in (monster(), monster(24, 1000), monster(30, 100)):
            FD.check_embed(A.to_embed(card))                                              # would raise like Discord's 400

    def test_a_huge_card_is_valid_as_every_part(self):
        for p in A.split_card(monster(24, 1000)):
            FD.check_embed(A.to_embed(p))


class TestSending(TC.Cmd):
    def test_reply_sends_every_part(self):
        i = FD.FakeInteraction(self.admin)
        self.run_async(reply(i, card=monster()))
        embeds = [m["embed"] for m in i.sent if m.get("embed")]
        self.assertGreater(len(embeds), 2)
        self.assertEqual(sum(len(e.fields) for e in embeds), 20)

    def test_confirmation_shows_every_part_before_asking_to_confirm(self):
        from tunbank.ui import confirm
        j = FD.FakeInteraction(self.admin)
        j.auto_confirm = True
        ok = self.run_async(confirm(self.svc, j, monster()))
        self.assertTrue(ok)
        msgs = [m for m in j.sent if m.get("embed")]
        self.assertGreater(len(msgs), 2)
        self.assertTrue(all(m.get("view") is None for m in msgs[:-1]))                    # only the LAST part carries the buttons
        self.assertIsNotNone(msgs[-1].get("view"))


class TestVaultCard(TC.OffshoreCmd):
    def test_the_vault_never_fails_even_with_custom_icons_and_every_resource(self):
        self.fund_alice({r: 98_765 for r in RES})
        self.pnw.holdings = {r: 987_654_321_000 for r in RES}
        self.pnw.off_holdings = {r: 876_543_210_000 for r in RES}
        icons.load({r: f"<:pnw_{r}:123456789012345678>" for r in RES})
        self.addCleanup(lambda: icons.load({}))
        i = self.call(self.bank, "holdings", self.admin)                                  # the fake now enforces Discord's real 6000 limit
        embeds = [m["embed"] for m in i.sent if m.get("embed")]
        self.assertTrue(embeds)
        for e in embeds:
            FD.check_embed(e)

    def test_the_vault_is_split_when_it_really_is_too_big(self):
        self.fund_alice({r: 98_765 for r in RES})
        self.pnw.holdings = {r: 987_654_321_000 for r in RES}
        self.pnw.off_holdings = {r: 876_543_210_000 for r in RES}
        import tunbank.alerts as AL
        original = AL.EMBED_BUDGET
        AL.EMBED_BUDGET = 1500                                                            # force the multi-message path with this data
        self.addCleanup(lambda: setattr(AL, "EMBED_BUDGET", original))
        i = self.call(self.bank, "holdings", self.admin)
        embeds = [m["embed"] for m in i.sent if m.get("embed")]
        self.assertGreater(len(embeds), 1)
        self.assertEqual(sum(1 for m in i.sent if m.get("view") is not None), 1)          # buttons only once, on the first message


# the harness classes would re-run their whole inherited suites here; they already run in test_commands
for _cls, _base in ((TestSending, TC.Cmd), (TestVaultCard, TC.OffshoreCmd)):
    for _name in dir(_base):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)
del _cls, _base, _name                                                                     # a leftover test class here would be collected and re-run

if __name__ == "__main__":
    unittest.main()
