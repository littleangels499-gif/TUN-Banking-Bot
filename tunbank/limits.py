"""Transfer limits, measured in Current Market Value (cents)."""
from __future__ import annotations

import datetime as dt

from . import ledger as L
from . import money as M
from .config import cfg_bool
from .fmt import dollars
from .ledger import LedgerError
from .valuation import value_amounts
from .util import ISO_FMT, now_iso, utcnow


class LimitExceeded(LedgerError):
    pass


def set_limit(conn, scope: str, scope_id: str, *, per_tx_cents=None, daily_cents=None, actor: str):
    """Set (or clear with 0) a limit. Only the fields you pass are changed."""
    row = conn.execute("SELECT * FROM limits WHERE scope=? AND scope_id=?", (scope, scope_id)).fetchone()
    per_tx = row["per_tx_cents"] if row else None
    daily = row["daily_cents"] if row else None
    old_text = f"per-transfer {dollars(per_tx) if per_tx else 'none'}, daily {dollars(daily) if daily else 'none'}"
    if per_tx_cents is not None:
        per_tx = per_tx_cents or None
    if daily_cents is not None:
        daily = daily_cents or None
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting=f"limit:{scope}", previous=old_text,
              new=f"per-transfer {dollars(per_tx) if per_tx else 'none'}, daily {dollars(daily) if daily else 'none'}",
              target={"GLOBAL": "everyone (self-withdrawals)", "ROLE": f"role <@&{scope_id}>", "NATION": f"nation [#{scope_id}]"}.get(scope, scope_id),
              category="LIMIT")
    conn.execute(
        "INSERT INTO limits(scope,scope_id,per_tx_cents,daily_cents,updated_by,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET per_tx_cents=excluded.per_tx_cents, "
        "daily_cents=excluded.daily_cents, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (scope, scope_id, per_tx, daily, str(actor), now_iso()))


def list_limits(conn):
    return conn.execute("SELECT * FROM limits ORDER BY scope, scope_id").fetchall()


def _used_last_24h(conn, column: str, value) -> int:
    since = (utcnow() - dt.timedelta(hours=24)).strftime(ISO_FMT)
    r = conn.execute(
        f"SELECT COALESCE(SUM(value_cents),0) s FROM transactions WHERE {column}=? AND created_at>=? "
        "AND status IN ('CONFIRMED','COMPLETED','RECONCILIATION_REQUIRED')", (value, since)).fetchone()
    return r["s"]


class NetWorthExceeded(LimitExceeded):
    pass


def net_worth(conn, nation_id: int, snapshot) -> tuple:
    """(amounts, Valuation) of a member's NET deposit: available (+ locked) minus what in-flight withdrawals already
    promised. Negative resources stay negative and reduce the total. Uses the same price snapshot as everything else."""
    amounts = L.get_balances(conn, nation_id, "AVAILABLE")
    amounts = M.sub(amounts, L.holds(conn, nation_id, "MEMBER_AVAILABLE"))
    if cfg_bool(conn, "net_worth_include_locked"):
        amounts = M.add(amounts, L.get_balances(conn, nation_id, "LOCKED"))
        amounts = M.sub(amounts, L.holds(conn, nation_id, "MEMBER_LOCKED"))
    return amounts, value_amounts(amounts, snapshot)


def check_net_worth(conn, *, tx_type: str, nation_id: int, amounts: dict, snapshot) -> None:
    """A member's own withdrawal may not be worth more than their net deposit worth (current market prices).

    Applies to WITHDRAW_SELF only, so ECON / alliance-funded payments are never restricted by it. If any needed
    price is missing or unreliable the withdrawal is refused: resources are never silently valued at zero."""
    if tx_type != "WITHDRAW_SELF" or not cfg_bool(conn, "net_worth_withdraw_limit"):
        return
    net_amounts, net_val = net_worth(conn, nation_id, snapshot)
    req_val = value_amounts(amounts, snapshot)
    if not (net_val.usable_for_limits and req_val.usable_for_limits) or net_val.total_cents is None \
            or req_val.total_cents is None:
        missing = sorted(set(net_val.missing) | set(req_val.missing))
        why = f" (no price for: {', '.join(missing)})" if missing else " (prices look unreliable)"
        raise NetWorthExceeded("Withdrawal rejected. Current market prices can't be verified right now" + why
                               + ", so your net deposit worth can't be calculated. Please try again shortly or ask ECON.")
    net_c, req_c = net_val.total_cents, req_val.total_cents
    if req_c > net_c:
        raise NetWorthExceeded(
            "Withdrawal rejected.\n\n"
            f"Current net deposit worth: {dollars(net_c)}\n"
            f"Requested withdrawal value: {dollars(req_c)}\n"
            f"Maximum allowed: {dollars(max(net_c, 0))}")


def check(conn, *, is_self: bool, nation_id, actor_id, actor_role_ids, amounts: dict, valuation, skip_per_tx: bool = False):
    """Raise LimitExceeded if this withdrawal breaks a configured limit."""
    applicable = []  # (label, per_tx, daily, usage_column, usage_value)
    if is_self:
        for scope, sid, label in (("GLOBAL", "*", "global"), ("NATION", str(nation_id), "your nation")):
            r = conn.execute("SELECT * FROM limits WHERE scope=? AND scope_id=?", (scope, sid)).fetchone()
            if r:
                applicable.append((label, r["per_tx_cents"], r["daily_cents"], "member_nation_id", nation_id))
    else:
        best = None
        for rid in actor_role_ids:
            r = conn.execute("SELECT * FROM limits WHERE scope='ROLE' AND scope_id=?", (str(rid),)).fetchone()
            if r:
                best = r if best is None else max(
                    (best, r), key=lambda x: (x["per_tx_cents"] or 10**30))
        if best:
            applicable.append(("your role", best["per_tx_cents"], best["daily_cents"],
                               "actor_discord_id", str(actor_id)))
    if not applicable:
        return
    only_cash = set(amounts) <= {"money"}
    for label, per_tx, daily, col, val in applicable:
        if not (per_tx or daily):
            continue
        if not only_cash and not valuation.usable_for_limits:
            raise LimitExceeded(
                "A transfer limit applies but the Current Market Value cannot be verified "
                "(prices missing/stale/unusual). Please try again shortly or ask ECON.")
        total = valuation.total_cents or 0
        if per_tx and not skip_per_tx and total > per_tx:
            raise LimitExceeded(f"This is worth {dollars(total)}, above the per-transfer limit of "
                                f"{dollars(per_tx)} for {label}.")
        if daily:
            used = _used_last_24h(conn, col, val)
            if used + total > daily:
                raise LimitExceeded(
                    f"Daily limit for {label} is {dollars(daily)}; {dollars(used)} already used in "
                    f"the last 24h, this transfer is {dollars(total)}.")
