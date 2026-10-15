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


# ---------------------------------------------------------------------------------------------------------------------
# Inventory and fingerprint: prove a copy of the database (a backup, or the database on a new Railway account) holds the
# same data as the original, WITHOUT printing any secret.
# ---------------------------------------------------------------------------------------------------------------------
# What is counted, grouped by what it means to the alliance.
INVENTORY_TABLES = {
    "Members and links": ["members", "nation_link_history", "nation_names", "member_credentials"],
    "Balances and ledger": ["balances", "ledger_entries", "locks", "adjustments", "balance_transfers", "external_withdrawals",
                            "conversions", "import_batches", "opening_balance_rows", "deposit_resets"],
    "Transactions and PnW records": ["transactions", "pnw_records", "member_deposits", "grants", "bulk_batches",
                                     "offshore_transfers", "offshore_entries", "offshore_alliances"],
    "Loans": ["loans", "loan_events", "loan_recoveries", "imported_loans"],
    "Audit and history": ["audit_log", "config_audit", "integrity_events", "approval_requests", "reconciliation_runs",
                          "member_notifications", "alert_log"],
    "Settings and permissions": ["bank_config", "role_permissions", "role_flags", "bankers", "limits", "trade_embargoes",
                                 "trade_nationalists", "tax_brackets", "tax_exemptions"],
}
# Tables whose full CONTENT is hashed (so a single changed value is noticed, not just a different row count).
DIGEST_TABLES = ["members", "balances", "bank_config", "role_permissions", "role_flags", "bankers", "limits", "loans",
                 "nation_link_history", "member_credentials", "offshore_alliances", "offshore_balances"]


def _digest(conn, table: str) -> str | None:
    import hashlib
    import json

    try:
        rows = [tuple((v.hex() if isinstance(v, (bytes, bytearray)) else v) for v in r) for r in conn.execute(f"SELECT * FROM {table}")]
    except sqlite3.Error:
        return None
    rows.sort(key=repr)
    return hashlib.sha256(json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()


def inventory(path: Path) -> dict:
    """Open a database file read-only and describe everything that matters. Never includes a secret."""
    import hashlib
    import json

    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for group, tables in INVENTORY_TABLES.items():
            counts[group] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t in existing}
        chain, achain = L.verify_chain(conn, "ledger_entries"), L.verify_chain(conn, "audit_log")
        bal = conn.execute("SELECT COUNT(*) c FROM balances").fetchone()["c"] if "balances" in existing else 0
        mismatch = 0
        if {"balances", "ledger_entries"} <= existing:
            sums = {(r["nation_id"], r["bucket"], r["resource"]): r["s"] for r in conn.execute(
                "SELECT nation_id, bucket, resource, SUM(delta) s FROM ledger_entries GROUP BY 1,2,3") if r["s"]}
            have = {(r["nation_id"], r["bucket"], r["resource"]): r["amount"] for r in conn.execute("SELECT * FROM balances") if r["amount"]}
            mismatch = len(set(sums.items()) ^ set(have.items()))
        digests = {t: _digest(conn, t) for t in DIGEST_TABLES if t in existing}
        info = {
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "migrations": sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations")) if "schema_migrations" in existing else [],
            "counts": counts, "balance_rows": bal, "balances_not_matching_ledger": mismatch,
            "ledger_chain_ok": chain["ok"], "ledger_head": chain.get("head_hash"), "ledger_entries": chain["count"],
            "audit_chain_ok": achain["ok"], "audit_head": achain.get("head_hash"), "audit_entries": achain["count"],
            "digests": digests,
        }
        info["fingerprint"] = hashlib.sha256(json.dumps(
            {k: info[k] for k in ("migrations", "counts", "balance_rows", "ledger_head", "audit_head", "digests")},
            sort_keys=True, default=str).encode()).hexdigest()
        return info
    finally:
        conn.close()


def compare(a: dict, b: dict) -> list:
    """Human-readable list of every difference between two inventories (empty = identical data)."""
    diffs = []
    for g, tables in a["counts"].items():
        for t, n in tables.items():
            m = b["counts"].get(g, {}).get(t)
            if m != n:
                diffs.append(f"{t}: {n} rows vs {m}")
    for k in ("ledger_head", "audit_head"):
        if a[k] != b[k]:
            diffs.append(f"{k} differs (the history is not identical)")
    for t, d in a["digests"].items():
        if b["digests"].get(t) != d:
            diffs.append(f"{t}: contents differ")
    return diffs


def check_credentials(path: Path, key: str | None) -> dict:
    """Can every stored member API key be decrypted with this CREDENTIAL_ENCRYPTION_KEY? Counts only; nothing is printed."""
    from .credentials import Crypto, CredentialError

    crypto = Crypto(key)
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        try:
            rows = conn.execute("SELECT key_enc FROM member_credentials").fetchall()
        except sqlite3.Error:
            return {"stored": 0, "readable": 0, "unreadable": 0, "key_configured": crypto.enabled}
        ok = bad = 0
        for (blob,) in rows:
            try:
                crypto.decrypt(blob)
                ok += 1
            except CredentialError:
                bad += 1
        return {"stored": len(rows), "readable": ok, "unreadable": bad, "key_configured": crypto.enabled}
    finally:
        conn.close()


def _chain_tail(conn, table: str):
    row = conn.execute(f"SELECT id, entry_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return (row["id"], row["entry_hash"]) if row else (0, None)


def history_preserved(old: Path, new: Path) -> dict:
    """Is every ledger / audit entry of `old` still present, byte-for-byte, in `new` (new entries may only have been appended)?"""
    out = {}
    o = sqlite3.connect(f"file:{old}?mode=ro&immutable=1", uri=True)
    n = sqlite3.connect(f"file:{new}?mode=ro&immutable=1", uri=True)
    o.row_factory = n.row_factory = sqlite3.Row
    try:
        for table in ("ledger_entries", "audit_log", "config_audit"):
            try:
                oid, ohash = _chain_tail(o, table) if table != "config_audit" else (o.execute("SELECT COUNT(*) FROM config_audit").fetchone()[0], None)
            except sqlite3.Error:
                continue
            if table == "config_audit":
                out[table] = n.execute("SELECT COUNT(*) FROM config_audit").fetchone()[0] >= oid
            elif oid == 0:
                out[table] = True
            else:
                row = n.execute(f"SELECT entry_hash FROM {table} WHERE id=?", (oid,)).fetchone()
                out[table] = bool(row) and row["entry_hash"] == ohash
        return out
    finally:
        o.close()
        n.close()


CONFIG_DIGESTS = ("bank_config", "role_permissions", "role_flags", "bankers", "limits", "offshore_alliances")


def verdict(old: Path, new: Path) -> dict:
    """STRICT = identical data. COMPATIBLE = the new database holds everything the old one did (history intact, settings and
    permissions identical, no table smaller); the only differences are newer activity. Anything else = DIFFERENT."""
    a, b = inventory(old), inventory(new)
    strict = a["fingerprint"] == b["fingerprint"]
    reasons = []
    hp = history_preserved(old, new)
    for t, ok in hp.items():
        if not ok:
            reasons.append(f"{t}: the old history is NOT contained, unchanged, in the new database")
    for t in CONFIG_DIGESTS:
        if a["digests"].get(t) != b["digests"].get(t):
            reasons.append(f"{t}: settings/permissions differ")
    for g, tables in a["counts"].items():
        for t, n_old in tables.items():
            n_new = b["counts"].get(g, {}).get(t)
            if n_new is None or n_new < n_old:
                reasons.append(f"{t}: {n_old} rows in the old database but only {n_new} in the new one")
    return {"strict": strict, "compatible": not reasons, "reasons": reasons, "old": a, "new": b}
