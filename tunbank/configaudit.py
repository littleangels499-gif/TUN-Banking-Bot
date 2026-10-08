"""The configuration / security audit log.

Every change to a setting that affects the money system, every permission change, and every security-sensitive admin
action is written here AT THE MOMENT it happens (inside the same database transaction), and then posted to the private
audit channel. Entries cannot be edited or deleted. If the channel can't be reached, the entry still exists and is posted
later: a change is never silent.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import hashlib

from .util import now_iso

# Who/what triggered the change (set when a command, button or form starts running; the low-level code reads it).
ACTION: contextvars.ContextVar = contextvars.ContextVar("tun_action", default="bot")

TITLES = {
    "CONFIG": "⚙️ Configuration Changed", "FINANCIAL": "⚙️ Financial Setting Changed", "CHANNEL": "⚙️ Log Channel Changed",
    "ICON": "⚙️ Display Setting Changed", "PERMISSION": "🔑 Permission Changed", "LIMIT": "📏 Limit Changed",
    "LINK": "🔗 Nation Link Changed", "ACCOUNTING": "🧮 Manual Balance Adjustment", "SECURITY": "🛡️ Security Action",
    "CLASSIFICATION": "🏷️ Manual Classification", "IMPORT": "📥 Opening Balances / Migration", "POLICY": "🧾 Tax Policy Changed",
    "ENVIRONMENT": "🖥️ Startup Configuration Changed", "CREDENTIAL": "🔐 Member Credential Changed", "RESTORE": "♻️ Database Restore", "RESET": "🧨 Deposit Reset", "TRADE": "🔎 Trade Monitor Changed",
}
FINANCIAL_KEYS = {"approval_threshold_value", "offshore_access", "offshore_keep_in_main", "grant_min_level", "large_credit_value",
                  "self_withdraw_enabled", "econ_locked_withdraw_enabled", "require_alliance_member_deposit", "opening_import_allowed",
                  "net_worth_withdraw_limit", "net_worth_include_locked", "conversion_enabled", "conversion_max_value", 
                  "member_deposit_enabled", "system_tags", "tag_ignore", "tag_loan", "tag_deposit"}


def amounts_text(d: dict) -> str:
    from . import money as M
    return "; ".join(f"{M.LABELS[r]} {M.fmt_units(r, d[r])}" for r in M.RESOURCES if d.get(r)) or "none"


def category_for_setting(key: str) -> str:
    if key.startswith("icon_"):
        return "ICON"
    if key.endswith("_channel_id"):
        return "CHANNEL"
    if key.startswith("trade_"):
        return "TRADE"
    return "FINANCIAL" if key in FINANCIAL_KEYS else "CONFIG"


def label_for(interaction) -> str:
    """'/bankset config' for a slash command; the caller adds button/form context otherwise."""
    cmd = getattr(interaction, "command", None)
    name = getattr(cmd, "qualified_name", None)
    return f"/{name}" if name else ACTION.get()


def record(conn, *, actor, setting: str, previous, new, target=None, category: str = "CONFIG", action: str | None = None,
           only_if_changed: bool = True) -> int:
    """Write one audit entry (inside the caller's transaction). Returns the entry id, or 0 if nothing actually changed."""
    prev = None if previous is None else str(previous)[:400]
    nw = None if new is None else str(new)[:400]
    if only_if_changed and prev == nw:
        return 0
    from . import ledger as L

    cur = conn.execute(
        "INSERT INTO config_audit(ts,actor_id,action,category,setting,previous,new,target) VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), str(actor), (action or ACTION.get())[:120], category, setting[:120], prev, nw, None if target is None else str(target)[:200]))
    L.audit(conn, actor, "CONFIG_AUDIT", f"{category}:{setting}", {"previous": prev, "new": nw, "target": target, "action": action or ACTION.get()})
    return cur.lastrowid


def card_for(row):
    from .alerts import Card, ORANGE

    ts = row["ts"]
    try:
        when = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        when = ts
    actor = row["actor_id"]
    who = f"<@{actor}> ({actor})" if str(actor).isdigit() else str(actor)
    c = Card(TITLES.get(row["category"], "⚙️ Configuration Changed"), color=ORANGE, kind="CONFIG_AUDIT")
    c.add("Administrator", who, True)
    c.add("Discord ID", f"`{actor}`", True)
    c.add("Setting", f"`{row['setting']}`", True)
    c.add("Previous", f"`{row['previous'] if row['previous'] not in (None, '') else '—'}`", True)
    c.add("New", f"`{row['new'] if row['new'] not in (None, '') else '—'}`", True)
    c.add("Action", row["action"], True)
    if row["target"]:
        c.add("Affected", row["target"], False)
    c.add("Time", when, True)
    c.footer = f"Audit entry #{row['id']} · permanent record · TUN Bank"
    return c


# ---------------------------------------------------------------- startup (.env / Railway) changes
def env_snapshot(settings) -> dict:
    """Non-secret picture of the configuration that comes from .env / Railway. Keys appear only as a short fingerprint,
    so a rotated key is noticed without the key itself ever being stored."""
    def fp(v):
        return "not set" if not v else "set:" + hashlib.sha256(v.encode()).hexdigest()[:8]
    off = settings.offshore
    return {
        "main_alliance_id": settings.alliance_id,
        "offshore_alliance_id": off.alliance_id if off else "none",
        "offshore_nation_id": settings.offshore_nation_id or "none",
        "payout_bank": settings.payout.name,
        "main_can_send": "yes" if settings.main.bot_key else "no",
        "alliance_receiver_type": settings.alliance_receiver_type,
        "pnw_read_key": fp(settings.pnw_api_key),
        "offshore_api_key": fp(off.api_key if off else ""),
        "offshore_bot_key": fp(off.bot_key if off else ""),
        "main_bot_key": fp(settings.main.bot_key or ""),
        "credential_encryption": "on" if settings.credential_key else "off",
    }


def check_env_changes(conn, settings) -> int:
    """Compare today's startup configuration with the last one and log every difference. Returns how many."""
    import json
    from . import ledger as L

    now = env_snapshot(settings)
    raw = L.get_state(conn, "env_snapshot")
    L.set_state(conn, "env_snapshot", json.dumps(now, sort_keys=True))
    if not raw:
        return 0
    old = json.loads(raw)
    n = 0
    for k in sorted(set(old) | set(now)):
        if old.get(k) != now.get(k):
            record(conn, actor="system:startup", action="bot started with different .env / Railway variables", setting=k,
                   previous=old.get(k), new=now.get(k), category="ENVIRONMENT")
            n += 1
    return n
