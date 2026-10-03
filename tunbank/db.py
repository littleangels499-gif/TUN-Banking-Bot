"""SQLite database access, safe migrations and backups."""
from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

from .util import now_iso, sha256_bytes

log = logging.getLogger("tunbank.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DB_FILENAME = "tunbank.db"


class DatabaseError(Exception):
    pass


class Database:
    """One shared SQLite connection, guarded by a lock.

    - `with db.tx() as conn:` runs everything inside ONE all-or-nothing
      transaction (BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any error).
    - `with db.read() as conn:` is for read-only queries.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=30
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")  # safety over speed
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextlib.contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    @contextlib.contextmanager
    def read(self):
        with self._lock:
            yield self.conn

    def close(self):
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()

    # ------------------------------------------------------------ backups
    def backup_to(self, dest: str | os.PathLike) -> Path:
        """Make a consistent copy of the live database (safe while the bot runs)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        if tmp.exists():
            tmp.unlink()
        with self._lock:
            target = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        check = sqlite3.connect(str(tmp))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            tmp.unlink(missing_ok=True)
            raise DatabaseError(f"Backup failed its integrity check: {result}")
        os.replace(tmp, dest)
        return dest

    # --------------------------------------------------------- migrations
    def migrate(self, backup_dir: str | os.PathLike | None = None) -> list[str]:
        """Apply any new migration files. Takes a backup first if there is
        existing data. Returns the names of migrations applied."""
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                " checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {
                r["version"]: r
                for r in self.conn.execute("SELECT * FROM schema_migrations")
            }
            files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
            pending = []
            for f in files:
                version = int(f.name[:3])
                text = f.read_bytes()
                checksum = sha256_bytes(text)
                if version in applied:
                    if applied[version]["checksum"] != checksum:
                        raise DatabaseError(
                            f"Migration {f.name} was edited after it was applied. "
                            "Never edit old migration files - add a new one instead."
                        )
                else:
                    pending.append((version, f, text, checksum))
            for v in applied:
                if not any(int(f.name[:3]) == v for f in files):
                    raise DatabaseError(
                        f"The database has migration {v} but that file is missing "
                        "from this version of the bot. Do NOT run an older bot version "
                        "against a newer database."
                    )
            if not pending:
                return []

            if applied and backup_dir:
                stamp = now_iso().replace(":", "").replace("-", "")
                bpath = Path(backup_dir) / f"pre-migration-{stamp}.db"
                self.backup_to(bpath)
                log.info("Pre-migration backup written to %s", bpath)

            done = []
            for version, f, text, checksum in pending:
                sql = text.decode("utf-8")
                script = (
                    "BEGIN IMMEDIATE;\n" + sql + "\n"
                    f"INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                    f"VALUES({version}, '{f.name}', '{checksum}', '{now_iso()}');\n"
                    "COMMIT;"
                )
                try:
                    self.conn.executescript(script)
                except sqlite3.Error as exc:
                    with contextlib.suppress(sqlite3.Error):
                        self.conn.execute("ROLLBACK")
                    raise DatabaseError(
                        f"Migration {f.name} failed and was rolled back: {exc}"
                    ) from exc
                done.append(f.name)
                log.info("Applied migration %s", f.name)
            return done

    def integrity_ok(self) -> bool:
        with self._lock:
            return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def prune_backups(backup_dir: Path, keep_daily: int = 30, keep_premigration: int = 10):
    """Delete old automatic backups, keeping the newest ones."""
    for pattern, keep in (("daily-*.db", keep_daily), ("pre-migration-*.db", keep_premigration)):
        files = sorted(backup_dir.glob(pattern))
        for old in files[: max(0, len(files) - keep)]:
            old.unlink(missing_ok=True)


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
