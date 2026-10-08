"""Formatting helpers. Quantities and Current Market Value are ALWAYS shown together.

Everything here produces Discord-markdown text with resource icons (see icons.py).
"""
from __future__ import annotations

from . import icons
from . import money as M
from .valuation import Valuation

NONE = "_None_"


def dollars(cents: int | None) -> str:
    if cents is None:
        return "unavailable"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def compact_number(units: int) -> str:
    """12,345,678 -> 12.3M (for tight spaces)."""
    v = abs(units) / M.SCALE
    sign = "-" if units < 0 else ""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= limit:
            return f"{sign}{v / limit:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{sign}{v:,.2f}".rstrip("0").rstrip(".")


def compact_money(cents: int) -> str:
    """609_000_000_00 -> $6.09B (for reports)."""
    v, sign = abs(cents) / 100, "-" if cents < 0 else ""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= limit:
            return f"{sign}${v / limit:.2f}{suffix}"
    return f"{sign}${v:,.2f}"


def amount_lines(amounts: dict) -> str:
    """One resource per line with its icon:  💵 **Cash** — $1,000,000.00"""
    lines = [f"{icons.resource(r)} **{M.LABELS[r]}** — {M.fmt_units(r, amounts[r])}"
             for r in M.RESOURCES if amounts.get(r)]
    return "\n".join(lines) if lines else NONE


def value_line(v: Valuation | None) -> str:
    """The 'Current Market Value' line with honest warnings."""
    ic = icons.status("value")
    if v is None or v.total_cents is None:
        return f"{ic} **Current Market Value:** unavailable (no price data)"
    txt = f"{ic} **Current Market Value:** {dollars(v.total_cents)}"
    if not v.complete:
        txt = (f"{ic} **Current Market Value:** at least {dollars(v.total_cents)} "
               f"(no price for: {', '.join(v.missing)})")
    flags = []
    if v.stale:
        flags.append(f"{icons.status('warn')} STALE PRICES")
    if v.suspicious:
        flags.append(f"{icons.status('warn')} PRICES UNUSUAL")
    if v.as_of and not (set(v.parts) <= {"money"}):
        flags.append(f"prices as of {v.as_of}")
    if flags:
        txt += "\n-# " + " · ".join(flags)
    return txt


def amounts_with_value(amounts: dict, v: Valuation | None) -> str:
    """Quantities + combined value in one block."""
    return amount_lines(amounts) + "\n" + value_line(v)


def account_lines(accounts: dict) -> str:
    """Format {'available': {...}, 'locked': {...}} for before/after displays."""
    a = amount_lines(accounts.get("available", {}))
    l = amount_lines(accounts.get("locked", {}))
    return f"{icons.status('money')} **Available**\n{a}\n{icons.status('lock')} **Locked**\n{l}"


def short_amounts(amounts: dict) -> str:
    """One-line compact version for lists:  💵 1.2M · ⚫ 5K"""
    parts = [f"{icons.resource(r)} {compact_number(amounts[r])}" for r in M.RESOURCES if amounts.get(r)]
    return " · ".join(parts) if parts else "—"


def signed_lines(deltas: dict) -> str:
    out = []
    for r in M.RESOURCES:
        d = deltas.get(r)
        if d:
            out.append(f"{icons.resource(r)} **{M.LABELS[r]}** — {'+' if d > 0 else '−'}{M.fmt_units(r, abs(d))}")
    return "\n".join(out) or NONE


def bar(fraction: float, width: int = 12) -> str:
    """Text progress bar:  ▰▰▰▰▱▱▱▱"""
    fraction = max(0.0, min(1.0, fraction))
    full = round(fraction * width)
    return "▰" * full + "▱" * (width - full)


def composition(amounts: dict, valuation: Valuation | None, top: int = 6) -> str:
    """Share of total market value per resource, as bars. Needs prices; empty if unavailable."""
    if valuation is None or not valuation.total_cents:
        return ""
    rows = sorted(((r, v) for r, v in valuation.parts.items() if v), key=lambda kv: -kv[1])[:top]
    total = valuation.total_cents or 1
    return "\n".join(f"{icons.resource(r)} {bar(v / total, 10)} {v / total * 100:.0f}%" for r, v in rows)


TX_ICON = {"COMPLETED": "✅", "FAILED": "⛔", "CANCELLED": "⛔", "CONFIRMED": "⏳",
           "RECONCILIATION_REQUIRED": "⚠️", "PENDING": "⏳", "AWAITING_REVIEW": "⚠️"}
SOURCE_LABEL = {"ALLIANCE": "🛡️ Alliance funds", "MEMBER_AVAILABLE": "💰 Member available",
                "MEMBER_LOCKED": "🔒 Member locked"}


def tx_line(r, with_actor: bool = False) -> str:
    """Two-line, easy-to-scan transaction entry."""
    ic = TX_ICON.get(r["status"], "⏳")
    top = f"{ic} **#{r['id']}** · {r['status'].replace('_', ' ').title()} · {dollars(r['value_cents'])}"
    who = f" · by <@{r['actor_discord_id']}>" if with_actor else ""
    sub = (f"-# {SOURCE_LABEL.get(r['funding_source'], r['funding_source'])} → nation [#{r['dest_nation_id']}] · "
           f"PnW {r['pnw_record_id'] or '—'} · {r['created_at'][:16].replace('T', ' ')}{who}")
    return top + "\n" + sub
