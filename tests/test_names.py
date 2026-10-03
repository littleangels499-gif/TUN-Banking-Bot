"""Catches 'forgot to import something' mistakes in code paths the other tests don't reach."""
import builtins
import importlib
import pathlib
import symtable
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fake_discord  # noqa: E402

fake_discord.install()


def walk(table, mod, path, problems):
    for sym in table.get_symbols():
        if sym.is_referenced() and sym.is_global() and not sym.is_assigned():
            n = sym.get_name()
            if not hasattr(mod, n) and not hasattr(builtins, n):
                problems.append(f"{mod.__name__}:{path or '<module>'} uses undefined name '{n}'")
    for child in table.get_children():
        walk(child, mod, f"{path}.{child.get_name()}" if path else child.get_name(), problems)


class TestNames(unittest.TestCase):
    def test_no_undefined_names(self):
        problems = []
        for f in sorted(pathlib.Path(__file__).resolve().parent.parent.glob("tunbank/*.py")):
            mod = importlib.import_module(f"tunbank.{f.stem}")
            walk(symtable.symtable(f.read_text(), str(f), "exec"), mod, "", problems)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
