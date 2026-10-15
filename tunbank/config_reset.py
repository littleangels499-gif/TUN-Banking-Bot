"""Reset CONFIGURATION to defaults. This is NOT a data wipe.

Only the tables listed in SCOPES are touched. Every table that holds money, members, links, member API keys, transactions,
PnW records, loans, ledgers, the audit history or the bank's safety state is PROTECTED: before and after the reset the row
counts of all of them are compared inside the same database transaction, and if even one differs the whole reset is
rolled back. The previous values are written to the configuration audit log before they are removed.
"""
from __future__ import annotations

from . import configaudit as CA
from . import ledger as L
from .config import DEFAULTS

RESET_PHRASE = "RESET CONFIGURATION"

SETTINGS_ONLY = ("bank_config",)
ALL_CONFIG = ("bank_config", "role_permissions", "role_flags", "bankers", "limits", "trade_embargoes", "trade_nationalists")
SCOPES = {"settings": SETTINGS_ONLY, "all": ALL_CONFIG}

DESCRIPTION = {
    "bank_config": "bank settings (channels, switches, rules, icons) go back to their defaults",
    "role_permissions": "which Discord roles are Auditor / Banker / Minister / Admin",
    "role_flags": "which roles may see alliance holdings and tax information",
    "bankers": "individually named bankers",
    "limits": "withdrawal limits (global, per role, per nation)",
    "trade_embargoes": "the trade-monitor embargo list",
    "trade_nationalists": "the trade-monitor nationalist list",
}

# Everything below must be byte-for-byte unchanged by a reset. (Counted by rows; the ledger and audit also keep their hash chains.)
PROTECTED = (
    "members", "nation_link_history", "nation_names", "member_credentials", "balances", "ledger_entries", "locks", "adjustments",
    "adjustment_items", "balance_transfers", "balance_transfer_lines", "external_withdrawals", "conversions", "import_batches",
    "opening_balance_rows", "deposit_resets", "deposit_reset_items", "transactions", "tx_items", "pnw_records", "member_deposits",
    "deposit_intents", "grants", "bulk_batches", "bulk_items", "offshore_transfers", "offshore_entries", "offshore_alliances",
    "offshore_balances", "offshore_corrections", "loans", "loan_events", "loan_recoveries", "imported_loans", "audit_log",
    "config_audit", "integrity_events", "approval_requests", "reconciliation_runs", "member_notifications", "alert_log",
    "price_snapshots", "system_state", "tax_records", "tax_turns", "tax_brackets", "tax_exemptions", "trade_alerts", "trade_seen",
)


# The audit trails are append-only: a reset adds entries to them (it must, to record itself) but must never remove one.
APPEND_ONLY = ("audit_log", "config_audit")


class ResetError(L.LedgerError):
    pass


def _count(conn, table: str):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:  # noqa: BLE001 - table does not exist in this schema version
        return None


def preview(conn, scope: str) -> dict:
    """What a reset would change, and what it will never touch."""
    if scope not in SCOPES:
        raise ResetError("Unknown scope.")
    tables = SCOPES[scope]
    changed_settings = 0
    if "bank_config" in tables:
        from .config import cfg_get
        changed_settings = sum(1 for k in DEFAULTS if cfg_get(conn, k) != DEFAULTS[k][0])
    rows = {t: (changed_settings if t == "bank_config" else _count(conn, t) or 0) for t in tables}
    return {"will_reset": rows, "protected": {t: _count(conn, t) for t in PROTECTED if _count(conn, t) is not None}}


def reset(conn, *, scope: str, actor: str, reason: str) -> dict:
    """Run inside the caller's transaction. Raises (so the caller's transaction rolls back) if anything protected would change."""
    if scope not in SCOPES:
        raise ResetError("Unknown scope.")
    if not (reason or "").strip():
        raise ResetError("A reason is required.")
    before = {t: _count(conn, t) for t in PROTECTED}
    cfg_chain_before = L.verify_chain(conn, "ledger_entries")["head_hash"]
    done = {}
    tables = SCOPES[scope]
    if "bank_config" in tables:
        from .config import cfg_get
        n = 0
        for k, (default, _desc) in DEFAULTS.items():
            cur = cfg_get(conn, k)
            if cur != default:
                CA.record(conn, actor=actor, setting=k, previous=cur, new=default, target="configuration reset",
                          category=CA.category_for_setting(k), action="CONFIG_RESET")
                n += 1
        conn.execute("DELETE FROM bank_config")
        done["bank_config"] = n
    removed_detail = {}
    for t in tables:
        if t == "bank_config":
            continue
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
        if rows:
            removed_detail[t] = rows[:200]
            CA.record(conn, actor=actor, setting=f"reset:{t}", previous=f"{len(rows)} entries", new="0 entries",
                      target=DESCRIPTION[t], category="PERMISSION" if t in ("role_permissions", "role_flags", "bankers") else "CONFIG",
                      action="CONFIG_RESET", only_if_changed=False)
        conn.execute(f"DELETE FROM {t}")
        done[t] = len(rows)
    after = {t: _count(conn, t) for t in PROTECTED}
    bad = [t for t in PROTECTED if (after[t] < before[t] if t in APPEND_ONLY else after[t] != before[t])]
    if bad or L.verify_chain(conn, "ledger_entries")["head_hash"] != cfg_chain_before:
        diff = sorted(bad)
        raise ResetError("SAFETY STOP: the reset would have changed protected records (" + ", ".join(diff or ["ledger"]) + "). Nothing was changed.")
    L.audit(conn, actor, "CONFIG_RESET", scope, {"scope": scope, "reason": reason.strip(), "reset": done, "removed": removed_detail})
    return done
