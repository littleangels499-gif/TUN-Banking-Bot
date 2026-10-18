"""The /help screen is a Discord View: at most 5 buttons per row and rows 0-4. Adding a command category once pushed an admin's
help over that limit and every button crashed ("item would not fit at row 2 (6 > 5 width)"). Every state is built here."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_discord as FD  # noqa: E402
import test_commands as TC  # noqa: E402

from tunbank import cmds_help, perms  # noqa: E402
from tunbank.ui import levels  # noqa: E402


class Base(TC.Cmd):
    def real_tree(self):
        """The command tree of the REAL bot (every group registered), not the harness's tiny one that only has /help and /prices."""
        if not hasattr(self, "_real"):
            from tunbank import bot as botmod
            self._real = botmod.TunBankBot(self.settings, self.db)
            self.run_async(self._real.setup_hook())
        return self._real.tree

    def all_levels(self):
        return {"AUDITOR", "BANKER", "MINISTER", "ADMIN"} | {"FLAG:" + f for f in perms.FLAGS}

    def quick(self, lv):
        out = [("My dashboard", "🏦", None), ("Market prices", "💹", None)]
        if "FLAG:bank_view_alliance_holdings" in lv:
            out.append(("Vault", "🏛️", None))
        if "AUDITOR" in lv:
            out.append(("Integrity", "🛡️", None))
        return out

    def check_every_state(self, lv):
        view = cmds_help.HelpView(1, self.real_tree(), lv, self.quick(lv))
        states = 0
        for key, cards in view.pages.items():
            for idx in range(len(cards)):
                view.current, view.index = key, idx
                view._build()                                              # raises exactly like Discord when a row overflows
                rows = {}
                for c in view.children:
                    if hasattr(c, "row") and c.row is not None:
                        rows[c.row] = rows.get(c.row, 0) + 1
                self.assertTrue(all(0 <= r <= 4 for r in rows), rows)
                self.assertTrue(all(n <= 5 for n in rows.values()), rows)
                labels = [c.label for c in view.children if hasattr(c, "label")]
                self.assertEqual(len(labels), len(set(labels)), "duplicate button")
                states += 1
        return view, states


for _n in dir(TC.Cmd):
    if _n.startswith("test_"):
        setattr(Base, _n, None)
del _n


class TestHelpLayout(Base):
    def test_every_category_and_page_builds_for_a_full_admin(self):
        view, states = self.check_every_state(self.all_levels())
        self.assertGreater(len(view.pages), 9)                              # the case that used to overflow: more than 8 category buttons
        self.assertGreater(states, len(view.pages))                         # includes multi-page categories

    def test_there_is_room_for_every_category(self):
        # beyond MAX_CATEGORIES the buttons would not fit in Discord's 5 rows: a new category then needs a different layout (a dropdown)
        self.assertLessEqual(len(cmds_help.CATEGORIES), cmds_help.HelpView.MAX_CATEGORIES)

    def test_every_permission_level_builds(self):
        for lv in (set(), {"AUDITOR"}, {"AUDITOR", "BANKER"}, {"AUDITOR", "BANKER", "MINISTER"}, self.all_levels()):
            self.check_every_state(lv)

    def test_the_real_admin_user_through_the_real_help_command(self):
        i = self.call_help()
        self.assertTrue(i.sent)

    def call_help(self):
        i = FD.FakeInteraction(self.admin)
        self.run_async(self.real_tree().top["help"].callback(i))
        return i

    def test_every_button_of_the_admins_help_can_be_pressed(self):
        i = self.call_help()
        view = i.sent[-1]["view"]
        for label in [c.label for c in view.children if hasattr(c, "label")]:
            if label in ("Vault", "Integrity", "My dashboard", "Market prices"):
                continue                                                       # these run other commands, covered elsewhere
            j = FD.FakeInteraction(self.admin)
            self.run_async(view.handlers[label](j))                            # a click must never raise
            view = j.edited["view"]


if __name__ == "__main__":
    unittest.main()
