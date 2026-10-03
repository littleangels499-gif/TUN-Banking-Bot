"""Checks your setup in plain English. Run:  python scripts/check_setup.py"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ok = True


def say(good, text):
    global ok
    ok = ok and good
    print(("  OK   " if good else "  FIX  ") + text)


print("\nChecking your TUN Bank setup...\n")
say(sys.version_info >= (3, 10), f"Python version {sys.version.split()[0]} (need 3.10 or newer)")
for mod, pip in (("discord", "discord.py"), ("aiohttp", "aiohttp"), ("dotenv", "python-dotenv"), ("openpyxl", "openpyxl")):
    try:
        importlib.import_module(mod)
        say(True, f"{pip} is installed")
    except ImportError:
        say(False, f"{pip} is NOT installed. Run:  pip install -r requirements.txt")

env = ROOT / ".env"
say(env.exists(), ".env file exists" if env.exists() else ".env file is missing (copy .env.example to .env and fill it in)")
try:
    from tunbank.config import ConfigError, load_settings

    s = load_settings()
    say(True, "All required settings are filled in")
    say(True, f"The database will be stored at: {s.db_path}")
except Exception as exc:  # noqa: BLE001
    say(False, str(exc))
print("\n" + ("Everything looks good. Start the bot with:  python main.py" if ok else
              "Fix the items marked FIX above, then run this check again.") + "\n")
