"""Restore a backup on YOUR computer. STOP the bot first.
Run:  python scripts/restore_local.py path\\to\\backup.db"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tunbank import backup as BK  # noqa: E402
from tunbank.config import ConfigError, load_settings  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("\nUsage: python scripts/restore_local.py path/to/backup.db\n")
try:
    s = load_settings()
    info = BK.stage_restore(s.data_dir, Path(sys.argv[1]))
except (ConfigError, BK.BackupError) as exc:
    sys.exit(f"\nNot restored: {exc}\n")
print(f"\nBackup is healthy ({info['ledger_entries']} ledger entries). It will be applied the next time "
      "you start the bot (python main.py). Your current database is saved first in data/backups/.\n")
