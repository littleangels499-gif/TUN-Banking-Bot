"""Rehearse an update on a COPY of your real database before you deploy it.

    1. In Discord run /bankset backup and save the file (this is your production data).
    2. python scripts/rehearse_upgrade.py path\\to\\that_backup.db

It copies the file to a temporary folder, applies every pending migration exactly as the live bot would, then proves nothing
changed: integrity, both hash chains, balances = ledger, every old ledger/audit entry still present, settings/roles/limits
identical. Your original file is never modified. Prints PASS or FAIL. Never prints a secret.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tunbank import backup as BK  # noqa: E402
from tunbank.db import Database, DatabaseError  # noqa: E402


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 2
    src = Path(argv[0])
    if not src.is_file():
        print(f"File not found: {src}")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="tunbank-rehearsal-"))
    before, work = tmp / "before.db", tmp / "tunbank.db"
    shutil.copy(src, before)
    shutil.copy(src, work)
    try:
        check = BK.validate_backup(work)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: that file is not a healthy TUN Bank database: {exc}")
        return 1
    print(f"Database looks healthy ({check.get('ledger_entries', '?')} ledger entries). Applying migrations to the COPY...")
    db = Database(work)
    try:
        applied = db.migrate(backup_dir=tmp / "bk")
    except DatabaseError as exc:
        print(f"FAIL: a migration failed (and was rolled back): {exc}")
        return 1
    print("Applied:", ", ".join(applied) if applied else "nothing (already up to date)")
    db.backup_to(tmp / "after.db")
    v = BK.verdict(before, tmp / "after.db")
    new = v["new"]
    problems = list(v["reasons"])
    if new["integrity_check"] != "ok":
        problems.append("integrity check failed")
    if not (new["ledger_chain_ok"] and new["audit_chain_ok"]):
        problems.append("a hash chain is broken")
    if new["balances_not_matching_ledger"]:
        problems.append("balances do not match the ledger")
    print(f"ledger entries {v['old']['ledger_entries']} -> {new['ledger_entries']} · audit entries {v['old']['audit_entries']} -> {new['audit_entries']}")
    print("RESULT:", "PASS - the upgrade is safe for this data" if not problems else "FAIL - " + "; ".join(problems))
    print(f"(your original file was not modified; scratch copy: {tmp})")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
