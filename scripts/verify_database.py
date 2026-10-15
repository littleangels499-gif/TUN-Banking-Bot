"""Verify a TUN Bank database file, or prove two of them hold the same data (use this BEFORE switching Railway accounts).

    python scripts/verify_database.py backup.db
    python scripts/verify_database.py old_backup.db --compare database_on_new_railway.db
    python scripts/verify_database.py backup.db --credentials      # needs CREDENTIAL_ENCRYPTION_KEY in the environment / .env

It only READS the file(s). It never prints a secret (only counts). Exit code 0 = everything checked out.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tunbank import backup as BK  # noqa: E402
from tunbank.db import MIGRATIONS_DIR  # noqa: E402


def show(path: Path) -> tuple[dict, bool]:
    info = BK.inventory(path)
    known = sorted(int(f.name[:3]) for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    problems = []
    if info["integrity_check"] != "ok":
        problems.append("SQLite integrity check failed")
    if not info["ledger_chain_ok"] or not info["audit_chain_ok"]:
        problems.append("ledger or audit hash chain is BROKEN")
    if info["balances_not_matching_ledger"]:
        problems.append(f"{info['balances_not_matching_ledger']} balance(s) do not equal the sum of their ledger entries")
    newer = set(info["migrations"]) - set(known)
    if newer:
        problems.append(f"database is from a NEWER bot version (migrations {sorted(newer)})")
    behind = sorted(set(known) - set(info["migrations"]))
    print(f"\n=== {path.name} ===")
    print(f"integrity check : {info['integrity_check']}")
    print(f"ledger history  : {info['ledger_entries']} entries, chain {'OK' if info['ledger_chain_ok'] else 'BROKEN'}")
    print(f"audit history   : {info['audit_entries']} entries, chain {'OK' if info['audit_chain_ok'] else 'BROKEN'}")
    print(f"balances        : {info['balance_rows']} rows, {info['balances_not_matching_ledger']} not matching the ledger")
    print(f"migrations      : up to {max(info['migrations']) if info['migrations'] else 0}"
          + (f"  (this code also has {behind}; they apply automatically at the next start)" if behind else "  (current)"))
    for group, tables in info["counts"].items():
        print(f"\n{group}")
        for t, n in tables.items():
            print(f"  {t:24} {n}")
    print(f"\nFINGERPRINT     : {info['fingerprint']}")
    print("RESULT          :", "PASS" if not problems else "FAIL - " + "; ".join(problems))
    return info, not problems


def main(argv: list) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    path = Path(argv[0])
    ok = True
    info, good = show(path)
    ok &= good
    if "--compare" in argv:
        other = Path(argv[argv.index("--compare") + 1])
        info2, good2 = show(other)
        ok &= good2
        v = BK.verdict(path, other)
        print("\n=== COMPARISON (old -> new) ===")
        if v["strict"]:
            print("IDENTICAL: the two databases hold exactly the same data. Safe to switch.")
        elif v["compatible"]:
            print("COMPATIBLE: the new database contains everything the old one did (every ledger and audit entry unchanged,")
            print("settings, roles, bankers and limits identical, no table smaller). The differences are newer activity only.")
            print("Safe to switch IF you compared against a backup made after the old bot was stopped.")
        else:
            ok = False
            print("DIFFERENT - do NOT switch yet:")
            for r in v["reasons"]:
                print("  -", r)
    if "--credentials" in argv:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        r = BK.check_credentials(path, os.environ.get("CREDENTIAL_ENCRYPTION_KEY", ""))
        print("\n=== MEMBER API KEYS ===")
        print(f"stored: {r['stored']} · readable with this CREDENTIAL_ENCRYPTION_KEY: {r['readable']} · unreadable: {r['unreadable']}")
        if r["stored"] and not r["key_configured"]:
            print("CREDENTIAL_ENCRYPTION_KEY is not set (or invalid) here: stored keys cannot be used.")
            ok = False
        elif r["unreadable"]:
            print("Some stored keys cannot be read: the encryption key is NOT the one that was used. Use the original key,")
            print("or members must run /nation setkey again.")
            ok = False
        else:
            print("OK: every stored key is readable.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
