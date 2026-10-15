"""Human-friendly view of the bank settings (/bankset config).

The raw settings in config.DEFAULTS are codes ("1", "0", "STAFF", "86400"). This module gives every setting a plain label,
a group, a type, and turns stored values into readable text ("✅ On", "2 minutes", "$2,000,000,000", "#econ-log").
It also validates what an admin types, so a typo can never be stored as a setting.
Nothing here changes what is stored or how a setting behaves: the stored value is exactly what the rest of the bot reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import money as M
from .config import DEFAULTS, cfg_get


@dataclass
class S:
    group: str
    label: str
    kind: str                     # toggle secs mins count pct usd times channel text choice amounts nations words hidden
    help: str = ""
    zero: str = ""                # what a stored 0 means in words ("Off", "No limit")
    choices: dict = field(default_factory=dict)      # stored value -> readable label
    example: str = ""


G_ALERTS = "Alerts & channels"
G_MEMBERS = "Member features"
G_SAFETY = "Safety & reconciliation"
G_EXT = "External withdrawals"
G_OFF = "Offshore"
G_LOANS = "Loans, tags & grants"
G_PRICE = "Market prices"
G_TRADE = "Trade monitor"
G_SYS = "Backups & scanning"
G_ICONS = "Resource icons"
GROUPS = [G_ALERTS, G_MEMBERS, G_SAFETY, G_EXT, G_OFF, G_LOANS, G_PRICE, G_TRADE, G_SYS, G_ICONS]

LEVELS = {"BANKER": "Banker and above", "MINISTER": "Minister and above", "ADMIN": "Admins only"}

SETTINGS: dict = {
    # --- alerts & channels
    "econ_log_channel_id": S(G_ALERTS, "ECON log channel", "channel", "Private channel where every financial alert is posted"),
    "config_audit_channel_id": S(G_ALERTS, "Security audit channel", "channel", "Private channel for the configuration / security audit log"),
    "trade_alert_channel_id": S(G_ALERTS, "Trade alert channel", "channel", "Trade-rule alerts (blank = the ECON log)"),
    "tax_alert_channel_id": S(G_ALERTS, "Tax summary channel", "channel", "One summary per tax turn (blank = the ECON log)"),
    "panel_channel_id": S(G_ALERTS, "TUN Bank panel channel", "channel", "Where the member panel is posted"),
    "panel_message_id": S(G_ALERTS, "Panel message", "hidden", "Set automatically"),
    "dm_members": S(G_ALERTS, "Direct messages to members", "toggle", "DM members when their account changes"),
    "tax_alert_settle_seconds": S(G_ALERTS, "Wait before the tax summary", "secs", "Time after the last tax record of a turn before the summary posts"),
    # --- member features
    "self_withdraw_enabled": S(G_MEMBERS, "Members can withdraw", "toggle", "Allow /bank withdrawself"),
    "member_send_enabled": S(G_MEMBERS, "Members can send funds", "toggle", "Allow members to send their available funds to another nation"),
    "member_deposit_enabled": S(G_MEMBERS, "Deposits from Discord", "toggle", "Members who linked their own API key may start deposits from Discord"),
    "conversion_enabled": S(G_MEMBERS, "Resource conversion", "toggle", "Members may convert resources inside their balance (ledger only)"),
    "conversion_max_value": S(G_MEMBERS, "Largest single conversion", "usd", "In $ of market value", zero="No cap"),
    "net_worth_withdraw_limit": S(G_MEMBERS, "Withdraw limit = net deposits", "toggle", "A member's withdrawal may not be worth more than their net deposits"),
    "net_worth_include_locked": S(G_MEMBERS, "Locked funds count as deposits", "toggle", "Locked balances count towards net deposit worth"),
    "econ_locked_withdraw_enabled": S(G_MEMBERS, "Staff can use locked funds", "toggle", "Staff may withdraw from a member's LOCKED funds"),
    "require_alliance_member_deposit": S(G_MEMBERS, "Only alliance members can deposit", "toggle", "Only credit deposits from current alliance members"),
    "approval_threshold_value": S(G_MEMBERS, "Second approver above", "usd", "ECON withdrawals worth more than this need a 2nd approver", zero="Off"),
    "approval_expiry_minutes": S(G_MEMBERS, "Approval request lifetime", "mins", "How long an approval request stays open"),
    "confirm_timeout_seconds": S(G_MEMBERS, "Confirmation screen lifetime", "secs", "How long a confirmation screen stays valid"),
    # --- safety & reconciliation
    "reconcile_gate_enabled": S(G_SAFETY, "Reconciliation blocks banking", "toggle",
                                "ON: known deficits/ownership shortfalls block banking. OFF: still reported, banking continues (other safeguards stay on). Change with /bankset gate"),
    "auto_emergency_lock": S(G_SAFETY, "Automatic emergency lock", "toggle", "A CRITICAL integrity finding triggers the EMERGENCY LOCK"),
    "recon_net_tolerance": S(G_SAFETY, "Net shortfall tolerated", "usd", "Overall shortfall (in $) tolerated before reconciliation is required", zero="None"),
    "reconcile_interval_seconds": S(G_SAFETY, "Reconcile every", "secs", "How often automatic reconciliation runs"),
    "stuck_tx_minutes": S(G_SAFETY, "Transaction considered stuck after", "mins", "In-flight transactions older than this need review"),
    "stale_sync_minutes": S(G_SAFETY, "Warn if PnW not scanned for", "mins", "Warn if there was no successful PnW scan for this long"),
    "large_credit_value": S(G_SAFETY, "Large deposit warning", "usd", "Deposits worth more than this raise a WARNING"),
    "mass_change_nations": S(G_SAFETY, "Mass-change warning", "count", "Warn if this many accounts are adjusted within 1 hour"),
    # --- external withdrawals
    "external_withdrawal_mode": S(G_EXT, "Deduct external withdrawals", "choice",
                                  "What to do when money leaves the offshore for a member outside TUN Bank",
                                  choices={"auto": "Automatically (only when confident)", "review": "Ask ECON first (review only)", "off": "Off (warning only)"}),
    "external_withdrawal_exclude_words": S(G_EXT, "Never auto-deduct if the note has", "words", "Comma-separated words that mark grants/payments/expenses"),
    "external_withdrawal_exempt_nations": S(G_EXT, "Never auto-deduct these nations", "nations", "Nation ids that receive alliance expense payments"),
    # --- offshore
    "offshore_access": S(G_OFF, "Who may use /bank offshore", "choice", "",
                         choices={"STAFF": "Staff (Bankers and above)", "ADMIN": "Admins only", "MEMBERS": "All members"}),
    "offshore_keep_in_main": S(G_OFF, "Keep in the main bank", "amounts", "Amounts that must stay in the MAIN bank", example="money=100m"),
    # --- loans, tags & grants
    "tag_ignore": S(G_LOANS, "Donation note", "text", "Deposit note that marks an alliance donation"),
    "tag_loan": S(G_LOANS, "Loan repayment note", "text", "Deposit note that marks a loan repayment"),
    "system_tags": S(G_LOANS, "System-transaction notes", "text", "Comma-separated notes that mark a system transaction, not a member deposit"),
    "loan_auto_recovery": S(G_LOANS, "Auto-recover overdue loans", "toggle", "An OVERDUE loan is settled automatically against the member's cash deposit"),
    "opening_import_allowed": S(G_LOANS, "Opening-balance import", "toggle", "Allow the spreadsheet opening-balance import"),
    "grant_min_level": S(G_LOANS, "Who may give grants", "choice", "Lowest staff level that may give a grant", choices=LEVELS),
    "excess_per_city": S(G_LOANS, "Most to keep per city", "amounts", "Most a nation should keep per city; limit = amount × cities", example="money=5m food=10k"),
    "excess_holdings": S(G_LOANS, "Old fixed limits", "hidden", "Legacy, unused"),
    # --- prices
    "price_ttl_seconds": S(G_PRICE, "Price cache time", "secs", "How long market prices are cached"),
    "price_stale_seconds": S(G_PRICE, "Prices considered stale after", "secs", "Older prices are flagged STALE"),
    "price_change_warn_pct": S(G_PRICE, "Warn on price jump over", "pct", "Warn when a price moves more than this between snapshots"),
    # --- trade monitor
    "trade_monitor_enabled": S(G_TRADE, "Trade monitor", "toggle", "Watch members' trades and alert when a rule is broken"),
    "trade_poll_seconds": S(G_TRADE, "Check for new trades every", "secs", ""),
    "trade_price_enabled": S(G_TRADE, "Price rule", "toggle", "Alert on trades far from the market price"),
    "trade_price_upper": S(G_TRADE, "Alert when price is at least", "times", "× the market price"),
    "trade_price_lower": S(G_TRADE, "Alert when price is at most", "times", "1 / this × the market price"),
    "trade_price_resources": S(G_TRADE, "Resources the price rule watches", "text", "* = all, or e.g. food,steel"),
    "trade_price_min_value": S(G_TRADE, "Ignore trades worth less than", "usd", "", zero="Watch everything"),
    "trade_nationalist_enabled": S(G_TRADE, "Nationalist rule", "toggle", "Alert when a Nationalist sells a resource they are barred from selling"),
    "trade_nationalist_scope": S(G_TRADE, "Nationalist rule applies to", "choice", "",
                                 choices={"ALL": "Every sale", "GLOBAL": "Global market sales only"}),
    "trade_embargo_enabled": S(G_TRADE, "Embargo rule", "toggle", "Alert on trades with members of embargoed alliances"),
    # --- backups & scanning
    "backup_keep_daily": S(G_SYS, "Daily backups kept", "count", "How many daily backups to keep"),
    "scan_interval_seconds": S(G_SYS, "Scan PnW bank every", "secs", "How often PnW bank records are scanned"),
}
for _r in M.RESOURCES:
    SETTINGS[f"icon_{_r}"] = S(G_ICONS, f"{M.LABELS[_r]} icon", "icon", "Change with /bankset seticon")

_missing = set(DEFAULTS) - set(SETTINGS)
assert not _missing, f"settings without a friendly description: {sorted(_missing)}"


# ------------------------------------------------------------------------------------------------ showing values
def _duration(seconds: int) -> str:
    if seconds == 0:
        return "0 seconds"
    parts = []
    for name, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " ".join(parts[:2])


def _num(v: str):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def human(key: str, value: str) -> str:
    s = SETTINGS[key]
    v = (value or "").strip()
    k = s.kind
    if k == "toggle":
        return "✅ On" if v in ("1", "true", "True", "yes") else "❌ Off"
    if k == "channel":
        return f"<#{v}>" if v.isdigit() else "Not set"
    if k == "choice":
        return s.choices.get(v, f"{v} (unrecognised)" if v else "Not set")
    if k in ("secs", "mins", "usd", "count", "pct", "times"):
        n = _num(v)
        if n is None:
            return f"{v} (not a number)"
        if n == 0 and s.zero:
            return s.zero
        return {"secs": lambda: _duration(n), "mins": lambda: _duration(n * 60), "usd": lambda: f"${n:,}",
                "count": lambda: f"{n:,}", "pct": lambda: f"{n}%", "times": lambda: f"{n}×"}[k]()
    if k == "amounts":
        if not v:
            return "None"
        try:
            return " · ".join(f"{M.LABELS[r]} {M.fmt_units(r, a)}" for r, a in M.parse_amounts(v).items())
        except M.AmountError:
            return f"{v} (unreadable)"
    if k == "nations":
        return ", ".join(f"#{x}" for x in v.replace(" ", "").split(",") if x) or "None"
    if k == "words":
        return ", ".join(x.strip() for x in v.split(",") if x.strip()) or "None"
    if k == "icon":
        return v or "Default"
    return v if v else "None"


# ------------------------------------------------------------------------------------------------ typing values
def _duration_in(text: str, unit: str) -> int:
    t = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", t)
    if not m:
        raise ValueError("Give a number of " + ("seconds" if unit == "s" else "minutes") + ", or a time like `90s`, `5m`, `2h`, `1d`.")
    n, u = float(m.group(1)), m.group(2) or unit
    secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    return int(secs if unit == "s" else secs / 60)


def parse_value(key: str, text: str) -> str:
    """Turn what an admin typed into the value to store, or raise ValueError with a friendly explanation."""
    s = SETTINGS[key]
    t = (text or "").strip()
    k = s.kind
    if k == "toggle":
        if t.lower() in ("1", "on", "yes", "true", "enable", "enabled", "y"):
            return "1"
        if t.lower() in ("0", "off", "no", "false", "disable", "disabled", "n"):
            return "0"
        raise ValueError("Use **on** or **off**.")
    if k == "channel":
        if t.lower() in ("", "none", "clear", "off", "default"):
            return ""
        m = re.fullmatch(r"<#(\d{5,25})>|(\d{5,25})", t)
        if not m:
            raise ValueError("Give a channel: mention it (#channel), paste its ID, or type `none` to clear it.")
        return m.group(1) or m.group(2)
    if k == "choice":
        for stored, label in s.choices.items():
            if t.lower() in (stored.lower(), label.lower()):
                return stored
        raise ValueError("Choose one of: " + ", ".join(f"**{stored}** ({label})" for stored, label in s.choices.items()) + ".")
    if k == "secs":
        return str(_duration_in(t, "s"))
    if k == "mins":
        return str(_duration_in(t, "m"))
    if k in ("count", "pct", "times"):
        n = _num(t.replace(",", ""))
        if n is None or n < 0 or not re.fullmatch(r"[\d,]+(\.\d+)?", t):
            raise ValueError("Give a whole number (0 or more).")
        return str(n)
    if k == "usd":
        try:
            units = M.parse_one(t.replace("$", "")) if t not in ("0", "") else 0
        except M.AmountError:
            raise ValueError("Give an amount in dollars, like `5000000`, `5m` or `2b`.") from None
        return str(int(units) // 100)
    if k == "amounts":
        if t.lower() in ("", "none", "clear"):
            return ""
        try:
            M.parse_amounts(t)
        except M.AmountError as exc:
            raise ValueError(f"Couldn't read that: {exc}. Example: `{s.example or 'money=5m food=10k'}`") from None
        return t
    if k == "nations":
        if t.lower() in ("", "none", "clear"):
            return ""
        ids = [x for x in re.split(r"[,\s]+", t) if x]
        if not all(x.isdigit() for x in ids):
            raise ValueError("Give nation ids separated by commas, e.g. `1234, 5678` (or `none`).")
        return ",".join(ids)
    if k == "words":
        return ",".join(w.strip().lower() for w in t.split(",") if w.strip())
    if k == "icon":
        from . import icons
        if t.lower() in ("", "default"):
            return ""
        if not icons.valid_emoji(t):
            raise ValueError("Use /bankset seticon with a normal emoji or a custom emoji like `<:oil:123456789012345678>`.")
        return t
    if k == "hidden":
        return t
    if not t and key in ("tag_ignore", "tag_loan"):
        raise ValueError("This note can't be empty.")
    return "" if t.lower() in ("none", "clear") else t


def find_key(text: str) -> str | None:
    """Accept the raw key, the label, or an unambiguous part of the label."""
    t = (text or "").strip().lower()
    if t in DEFAULTS:
        return t
    labels = {k: s.label.lower() for k, s in SETTINGS.items() if s.kind != "hidden"}
    exact = [k for k, lab in labels.items() if lab == t]
    if len(exact) == 1:
        return exact[0]
    part = [k for k, lab in labels.items() if t and t in lab]
    return part[0] if len(part) == 1 else None


# ------------------------------------------------------------------------------------------------ cards
def grouped() -> dict:
    out = {g: [] for g in GROUPS}
    for k, s in SETTINGS.items():
        if s.kind != "hidden":
            out[s.group].append(k)
    return out


def changed_keys(conn) -> set:
    return {k for k in DEFAULTS if cfg_get(conn, k) != DEFAULTS[k][0]}


def render_group(conn, group: str):
    from . import alerts as A

    keys = grouped()[group]
    changed = changed_keys(conn)
    c = A.Card(f"⚙️ Settings · {group}", "Change one with `/bankset config` → `setting` and `value`. ✏️ = changed from the default.", A.BLUE)
    lines = []
    for k in keys:
        s = SETTINGS[k]
        mark = " ✏️" if k in changed else ""
        lines.append(f"**{s.label}**{mark}\n{human(k, cfg_get(conn, k))}" + (f"\n*{s.help}*" if s.help and s.kind != "icon" else ""))
    # keep every field under Discord's limit
    chunk, size, part = [], 0, 1
    for ln in lines:
        if size + len(ln) > 900 and chunk:
            c.add(group if part == 1 else f"{group} (cont.)", "\n\n".join(chunk))
            chunk, size, part = [], 0, part + 1
        chunk.append(ln)
        size += len(ln) + 2
    if chunk:
        c.add(group if part == 1 else f"{group} (cont.)", "\n\n".join(chunk))
    return c


def render_overview(conn):
    from . import alerts as A

    changed = changed_keys(conn)
    c = A.Card("⚙️ Bank settings", "Plain-language view of every setting. Pick a page with the buttons, or run "
               "`/bankset config category:<name>`. To change one: `/bankset config setting:<name> value:<new value>`.", A.BLUE)
    for g, keys in grouped().items():
        n = len([k for k in keys if k in changed])
        c.add(g, f"{len(keys)} settings" + (f" · ✏️ {n} changed" if n else " · all default"), True)
    c.add("Examples", "`/bankset config setting:Members can withdraw value:off`\n`/bankset config setting:ECON log channel value:#econ-log`\n"
          "`/bankset config setting:Approval request lifetime value:2h`", False)
    return c
