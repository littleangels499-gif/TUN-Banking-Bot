"""TUN Bank - start here.  Run with:  python main.py"""
import logging
import sys

from tunbank.config import ConfigError, load_settings
from tunbank.db import Database, DatabaseError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("tunbank")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\nSETUP PROBLEM:\n{exc}\n")
        return 1

    log.info("Database file: %s", settings.db_path)
    try:
        from tunbank.backup import apply_pending_restore

        msg = apply_pending_restore(settings.data_dir, settings.db_path, settings.backup_dir)
        if msg:
            log.warning(msg)
    except Exception as exc:  # noqa: BLE001
        print(f"\nRESTORE PROBLEM: {exc}\n")
        return 1
    try:
        db = Database(settings.db_path)
        if not db.integrity_ok():
            print("\nThe database failed its integrity check. NOT starting, to protect your data.\n"
                  "Restore a backup (see docs/06_BACKUP_RESTORE.md).\n")
            return 1
        applied = db.migrate(backup_dir=settings.backup_dir)
        if applied:
            log.info("Database upgraded: %s", ", ".join(applied))
    except DatabaseError as exc:
        print(f"\nDATABASE PROBLEM:\n{exc}\n")
        return 1

    from tunbank.bot import TunBankBot  # imported late so setup errors show first

    bot = TunBankBot(settings, db)
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
