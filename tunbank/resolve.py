"""Turn whatever a person types into a nation id.

Accepted for every "nation" field:
  * a nation id           123456   #123456
  * a nation link         https://politicsandwar.com/nation/id=123456
  * a nation name         "Kingdom of Foo"  (any capitalisation; ANY nation in Politics & War, not only ours)
  * a Discord mention     @Alice  (picked from the Discord list)
  * a Discord username    alice   (only if that person linked their nation)

Safety rule: money commands NEVER guess. Names must match exactly (ignoring capitals);
if it is unclear you get suggestions instead of a silent best guess.
"""
from __future__ import annotations

import asyncio
import re

import discord
from discord import app_commands

from . import identity
from .pnw import PnWRejected, PnWUncertain


class ResolveError(Exception):
    """Message is written for the person typing the command."""


_ID = re.compile(r"[#\[\(\s]*(\d{1,9})[\]\)\s]*")
_URL = re.compile(r"politicsandwar\.com/nation/id=(\d+)", re.I)
_ANY_ID = re.compile(r"[?&/]id=(\d+)", re.I)
_MENTION = re.compile(r"<@!?(\d+)>")


def _exact(conn, roster: dict, key: str) -> dict:
    found = {}
    for r in conn.execute("SELECT nation_id, nation_name FROM members WHERE lower(nation_name)=? "
                          "OR lower(discord_name)=?", (key, key)):
        found[r["nation_id"]] = r["nation_name"] or ""
    for nid, name in roster.items():
        if (name or "").lower() == key:
            found[nid] = name
    return found


def _similar(conn, roster: dict, key: str, limit: int = 5) -> list:
    found = {}
    like = f"%{key}%"
    for r in conn.execute("SELECT nation_id, nation_name FROM members WHERE lower(nation_name) LIKE ? "
                          "OR lower(discord_name) LIKE ? LIMIT 20", (like, like)):
        found[r["nation_id"]] = r["nation_name"] or ""
    for nid, name in roster.items():
        if key in (name or "").lower():
            found[nid] = name
    return sorted(found.items(), key=lambda kv: kv[1].lower())[:limit]


async def resolve(svc, text: str) -> int:
    t = str(text if text is not None else "").strip()
    if not t:
        raise ResolveError("Please tell me which nation: an id, a nation link, a nation name, or @someone.")
    m = _ID.fullmatch(t)
    if m:
        return int(m.group(1))
    m = _URL.search(t) or (_ANY_ID.search(t) if "politicsandwar" in t.lower() else None)
    if m:
        return int(m.group(1))
    m = _MENTION.fullmatch(t)
    if m:
        with svc.db.read() as conn:
            r = conn.execute("SELECT nation_id FROM members WHERE discord_id=?", (m.group(1),)).fetchone()
        if r:
            return r["nation_id"]
        raise ResolveError(f"<@{m.group(1)}> has not linked a nation with `/nation link` yet.")
    key = t.lstrip("@").strip().lower()

    roster = dict(getattr(svc.scanner, "roster", {}) or {})
    if not roster:
        try:
            roster = await svc.pnw.fetch_alliance_members()
            svc.scanner.roster = dict(roster)
        except Exception:  # noqa: BLE001 - PnW down: local data only
            roster = {}
    with svc.db.read() as conn:
        found = _exact(conn, roster, key)
    for nid, nm in identity.exact(key).items():          # any nation we have ever seen, in any alliance
        found.setdefault(nid, nm)
    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        raise ResolveError(f"More than one nation matches '{t}': " + ", ".join(identity.label(i, n) for i, n in sorted(found.items())[:6])
                           + ". Please use the nation id.")

    # Not known locally: ask Politics & War about EVERY nation, not only ours.
    api_error, close = None, []
    try:
        rows = await identity.find_by_name(svc.pnw, t)
    except (PnWRejected, PnWUncertain) as exc:
        rows, api_error = [], str(exc)
    exact = [r for r in rows if (r.get("nation_name") or "").lower() == key]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if len(exact) > 1:
        opts = ", ".join(f"{identity.label(r['id'], r.get('nation_name'))}" + (f" · {r['alliance']['name']}" if (r.get('alliance') or {}).get('name') else "")
                         for r in exact[:6])
        raise ResolveError(f"More than one nation is called '{t}': {opts}. Please use the nation id so I don't pick the wrong one.")
    close = [(int(r["id"]), r.get("nation_name") or "") for r in rows]
    with svc.db.read() as conn:
        local = _similar(conn, roster, key)
    for nid, nm in list(local) + identity.suggest(key):
        if all(nid != c[0] for c in close):
            close.append((nid, nm))
    if api_error:
        msg = (f"I couldn't search Politics & War for '{t}' just now ({api_error}), and I don't know a nation with that name yet. "
               "Try the nation id or link, or try again shortly.")
    else:
        msg = f"I couldn't find any nation called '{t}' in Politics & War (I searched every nation, not only ours)."
    if close:
        msg += " Did you mean: " + ", ".join(f"**{n}** [#{i}]" for i, n in close[:5]) + "?"
    else:
        msg += " Check the spelling, or use the nation id or link."
    raise ResolveError(msg)


def label(conn, nation_id: int) -> str:
    """'Igala [#123456]' for any nation. Unknown -> 'Unknown Nation (ID: 123456)'."""
    hint = None
    if conn is not None and not identity.name(nation_id):
        r = conn.execute("SELECT nation_name FROM members WHERE nation_id=?", (nation_id,)).fetchone()
        hint = r["nation_name"] if r else None
    return identity.label(nation_id, hint)


# ------------------------------------------------------------ autocomplete
def make_autocomplete(svc):
    async def nation_autocomplete(interaction: discord.Interaction, current: str):
        key = (current or "").strip().lstrip("@").lower()
        roster = dict(getattr(svc.scanner, "roster", {}) or {})
        found = {}
        with svc.db.read() as conn:
            for r in conn.execute("SELECT nation_id, nation_name, discord_name FROM members"):
                hay = f"{(r['nation_name'] or '').lower()} {(r['discord_name'] or '').lower()} {r['nation_id']}"
                if not key or key in hay:
                    extra = f" (@{r['discord_name']})" if r["discord_name"] else ""
                    found[r["nation_id"]] = f"{r['nation_name'] or 'Nation'}{extra}"
        for nid, name in roster.items():
            if nid not in found and (not key or key in (name or "").lower() or key in str(nid)):
                found[nid] = name or "Nation"
        for nid, name in identity.suggest(key, 25):                      # every nation the bot has ever seen
            found.setdefault(nid, name)
        if len(found) < 5 and len(key) >= 3 and not key.isdigit():       # nobody known: ask PnW (fast, with a time limit)
            try:
                rows = await asyncio.wait_for(identity.find_by_name(svc.pnw, key), timeout=2.0)
                for r in rows:
                    found.setdefault(int(r["id"]), r.get("nation_name") or "Nation")
            except (asyncio.TimeoutError, PnWRejected, PnWUncertain, Exception):  # noqa: BLE001 - suggestions are best-effort
                pass
        items = sorted(found.items(), key=lambda kv: (not kv[1].lower().startswith(key), kv[1].lower()))[:25]
        return [app_commands.Choice(name=f"{name} [#{nid}]"[:100], value=str(nid)) for nid, name in items]

    return nation_autocomplete


def attach(svc, command, *params):
    """Give a command's nation field a live suggestion list."""
    cb = make_autocomplete(svc)
    for p in params:
        command.autocomplete(p)(cb)
