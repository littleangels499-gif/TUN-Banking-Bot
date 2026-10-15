"""Offline start-up check. Builds the REAL bot with dummy credentials in a temporary folder (no network, no .env, no real
database), applies every database migration, runs the bot's real start-up (command registration) and validates every slash
command with discord.py's own rules. Run it after an update, before you restart the live bot:

    python scripts/check_startup.py

It never reads your .env or your database and never contacts Discord or PnW. Exit code 0 = OK.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NEEDED = {"bank": {"credit", "adjust", "review", "withdrawself", "deposit"},
          "bankset": {"config", "gate", "resetconfig", "iconpack", "backup", "icons"}}


async def run() -> int:
    tmp = tempfile.mkdtemp(prefix="tunbank-check-")
    # dummy, throw-away values: nothing here is a real secret
    os.environ.update({"DISCORD_TOKEN": "dummy.token.value", "OWNER_DISCORD_IDS": "1", "ALLIANCE_ID": "10", "PNW_API_KEY": "dummykey1234567",
                       "PNW_BOT_KEY": "dummybot1234567", "DATA_DIR": tmp})
    for k in ("OFFSHORE_ALLIANCE_ID", "OFFSHORE_API_KEY", "OFFSHORE_BOT_KEY", "GUILD_ID", "RAILWAY_VOLUME_MOUNT_PATH"):
        os.environ.pop(k, None)
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False                      # never read a real .env
    from tunbank.bot import TunBankBot
    from tunbank.config import load_settings
    from tunbank.db import MIGRATIONS_DIR, Database

    settings = load_settings()
    db = Database(settings.db_path)
    applied = db.migrate(backup_dir=settings.backup_dir)
    expected = sorted(f.name for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    assert applied == expected, f"fresh database did not apply every migration: {applied}"
    assert db.integrity_ok()
    print(f"OK  database: {len(applied)} migrations applied to a fresh database, integrity ok")

    bot = TunBankBot(settings, db)
    synced = []

    async def fake_sync(*a, **k):                                   # what Discord would receive, built by discord.py's own code
        try:
            payload = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]
        except TypeError:
            payload = [c.to_dict() for c in bot.tree.get_commands()]
        synced.extend(payload)
        return []
    bot.tree.sync = fake_sync
    await bot.setup_hook()
    print(f"OK  start-up: commands registered and accepted by discord.py ({len(synced)} top-level commands)")

    problems = []
    if len(synced) > 100:
        problems.append("more than 100 top-level commands")
    names = {}
    for c in synced:
        subs = c.get("options") or []
        names[c["name"]] = {s["name"] for s in subs}
        if len(subs) > 25:
            problems.append(f"/{c['name']} has {len(subs)} subcommands (Discord allows 25)")
    for group, need in NEEDED.items():
        missing = need - names.get(group, set())
        if missing:
            problems.append(f"/{group} is missing: {sorted(missing)}")
    print("    " + " · ".join(f"/{g}: {len(n)}" for g, n in sorted(names.items()) if n))
    await bot.close()
    if problems:
        print("FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print("OK  every expected command is present; no group is over Discord's limit")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
