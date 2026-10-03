"""Resource and status icons.

Defaults are standard Unicode emoji (they work everywhere, nothing to upload).
Admins can replace any of them with a custom server emoji WITHOUT touching code:

    /bankset seticon resource:oil emoji:<:oil:123456789012345678>

(To get that text: type \\:oil: in Discord and send it - Discord shows the full <:name:id> code.)
Settings are stored in the database, so updates never reset them.
"""
from __future__ import annotations

DEFAULT_RESOURCE_ICONS = {
    "money": "💵", "food": "🍞", "coal": "⚫", "oil": "🛢️", "uranium": "☢️", "iron": "🔩",
    "bauxite": "🟤", "lead": "🔘", "gasoline": "⛽", "munitions": "💣", "steel": "🏗️",
    "aluminum": "🥫",
}

STATUS = {
    "ok": "✅", "warn": "⚠️", "bad": "⛔", "lock": "🔒", "unlock": "🔓", "wait": "⏳",
    "money": "💰", "value": "💹", "bank": "🏦", "member": "👤", "alliance": "🛡️",
    "tax": "🧾", "deposit": "📥", "withdraw": "📤", "chart": "📊", "audit": "🔎",
    "freeze": "🧊", "info": "ℹ️", "refresh": "🔄", "key": "🔑", "time": "🕒",
}

_current: dict = dict(DEFAULT_RESOURCE_ICONS)


def resource(res: str) -> str:
    return _current.get(res) or DEFAULT_RESOURCE_ICONS.get(res, "▪️")


def status(name: str) -> str:
    return STATUS.get(name, "")


def load(overrides: dict) -> None:
    """Replace the active icons. `overrides` is {resource: emoji}; blank values fall back to defaults."""
    _current.clear()
    _current.update(DEFAULT_RESOURCE_ICONS)
    for res, emoji in overrides.items():
        if res in DEFAULT_RESOURCE_ICONS and emoji and emoji.strip():
            _current[res] = emoji.strip()


def load_from_db(conn) -> None:
    from .config import cfg_get

    load({res: cfg_get(conn, f"icon_{res}") for res in DEFAULT_RESOURCE_ICONS})


def valid_emoji(text: str) -> bool:
    """Accept a Unicode emoji or a Discord custom emoji code like <:name:123> / <a:name:123>."""
    import re

    t = text.strip()
    if not t or len(t) > 60:
        return False
    if re.fullmatch(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>", t):
        return True
    return not t.isascii() and " " not in t and not t.startswith("<")


def plain(res: str) -> str:
    """Icon text for charts/files (custom emoji can't be drawn there)."""
    icon = resource(res)
    return "" if icon.startswith("<") else icon
