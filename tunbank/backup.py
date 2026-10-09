"""Backups and restores.

Restore is done in two safe steps so nobody edits a live database:
  1. An admin uploads a backup file with /bankset restorestage. It is fully validated
     and parked next to the database as `restore_pending.db`.
  2. On the next start-up the bot validates it again, copies the CURRENT database
     to backups/pre-restore-<time>.db, swaps the files, and starts normally.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from . import ledger as L
from .db import MIGRATIONS_DIR
from .util import now_iso

PENDING_NAME = "restore_pending.db"
REQUIRED_TABLES = {"ledger_entries", "balances", "pnw_records", "schema_migrations", "audit_log",
                   "transactions", "system_state"}


class BackupError(Exception):
    pass


def validate_backup(path: Path) -> dict:
    """Open a backup read-only and prove it is a healthy TUN Bank database."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < 4096:
        raise BackupError("That file is missing or too small to be a database.")
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise BackupError("That file is not an SQLite database.")
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The database file is damaged (integrity check failed).")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise BackupError("This is not a TUN Bank database (missing: " + ", ".join(sorted(missing)) + ").")
        known = {int(f.name[:3]) for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")}
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        if versions - known:
            raise BackupError("This backup is from a NEWER version of the bot than the one running. "
                              "Update the bot first, then restore.")
        chain = L.verify_chain(conn, "ledger_entries")
        achain = L.verify_chain(conn, "audit_log")
        if not chain["ok"] or not achain["ok"]:
            raise BackupError("The backup's own history checks failed (ledger or audit chain is broken). "
                              "Do not restore it; ask for help.")
        return {"ledger_entries": chain["count"], "audit_entries": achain["count"],
                "migrations": sorted(versions),
                "created": L.get_state(conn, "database_created_at")}
    finally:
        conn.close()


def stage_restore(data_dir: Path, source: Path) -> dict:
    info = validate_backup(source)
    target = Path(data_dir) / PENDING_NAME
    tmp = target.with_suffix(".partial")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return info


def apply_pending_restore(data_dir: Path, db_path: Path, backup_dir: Path) -> str | None:
    """Run at start-up BEFORE the database is opened. Returns a message if a restore happened."""
    pending = Path(data_dir) / PENDING_NAME
    if not pending.exists():
        return None
    try:
        validate_backup(pending)
    except BackupError as exc:
        bad = pending.with_name("restore_REJECTED.db")
        os.replace(pending, bad)
        return f"A staged restore was REJECTED and NOT applied ({exc}). The file was kept as {bad.name}."
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "")
    if db_path.exists():
        safety = backup_dir / f"pre-restore-{stamp}.db"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(safety))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.replace(pending, db_path)
    return (f"Restore applied. The previous database was saved as backups/pre-restore-{stamp}.db. "
            "Run /ledger reconcile and check /bank review for anything that happened after the backup.")
