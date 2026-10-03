"""Make a safe backup copy of the database on YOUR computer.  Run:  python scripts/backup_local.py"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tunbank.config import ConfigError, load_settings  # noqa: E402
from tunbank.db import Database  # noqa: E402

try:
    s = load_settings()
except ConfigError as exc:
    sys.exit(f"\n{exc}\n")
if not s.db_path.exists():
    sys.exit(f"\nNo database found at {s.db_path}\n")
db = Database(s.db_path)
dest = s.backup_dir / f"manual-{datetime.utcnow():%Y%m%d-%H%M%S}.db"
db.backup_to(dest)
db.close()
print(f"\nBackup saved: {dest}\nCopy it somewhere safe (another drive or cloud storage).\n")
