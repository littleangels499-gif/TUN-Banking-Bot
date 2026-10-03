"""Permissions. Discord role IDs are stored in the database (set with /bankset role),
never written in the code.

Levels, from lowest to highest:
  AUDITOR  - read-only audit / reconciliation views
  BANKER   - day-to-day banking within limits (Banker / Econ Officer)
  MINISTER - reserve/release, approvals, reports, wider banking (Econ Minister)
  ADMIN    - configuration and security (Administrator)
Bot owners (OWNER_DISCORD_IDS in .env) always count as ADMIN, so a brand-new
bot can be set up before any role exists.
"""
from __future__ import annotations

ORDER = {"AUDITOR": 1, "BANKER": 2, "MINISTER": 3, "ADMIN": 4}
# AUDITOR is read-only and sits beside the chain, not above BANKER: an auditor cannot move money.
CAN_MOVE_MONEY = {"BANKER", "MINISTER", "ADMIN"}


def levels_for(conn, user_id: int, role_ids, owner_ids) -> set:
    levels = set()
    if int(user_id) in owner_ids:
        levels.add("ADMIN")
    ids = {str(r) for r in role_ids}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT DISTINCT level FROM role_permissions WHERE role_id IN ({marks})",
                              tuple(ids)):
            levels.add(r["level"])
    if conn.execute("SELECT 1 FROM bankers WHERE discord_id=?", (str(user_id),)).fetchone():
        levels.add("BANKER")
    return levels


def has(levels: set, needed: str) -> bool:
    """True if the user holds `needed` or any higher money-moving level."""
    if needed == "AUDITOR":
        return bool(levels & {"AUDITOR", "MINISTER", "ADMIN"})
    if needed == "BANKER":
        return bool(levels & {"BANKER", "MINISTER", "ADMIN"})
    if needed == "MINISTER":
        return bool(levels & {"MINISTER", "ADMIN"})
    if needed == "ADMIN":
        return "ADMIN" in levels
    return False


def can_read_finance(levels: set) -> bool:
    return bool(levels)  # any staff level may read ECON financial views


def set_role(conn, level: str, role_id: str, add: bool):
    if level not in ORDER:
        raise ValueError("level must be AUDITOR, BANKER, MINISTER or ADMIN")
    if add:
        conn.execute("INSERT OR IGNORE INTO role_permissions(level, role_id) VALUES(?,?)",
                     (level, str(role_id)))
    else:
        conn.execute("DELETE FROM role_permissions WHERE level=? AND role_id=?", (level, str(role_id)))


def list_roles(conn):
    return conn.execute("SELECT level, role_id FROM role_permissions ORDER BY level, role_id").fetchall()
