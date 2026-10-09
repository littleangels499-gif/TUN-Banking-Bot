"""Shared offshore: ONE physical PnW offshore bank, MANY registered alliances, one beneficial-ownership ledger.

  * The offshore's real PnW balance is the physical source of truth. This ledger only says whose share of it is whose.
  * Ownership grows only from a real PnW deposit record (or an Admin assigning funds that are physically there but
    still UNASSIGNED) and shrinks only from a real PnW outflow record, a documented release, or a transfer between alliances.
  * It can never go negative, and the bot NEVER changes an alliance's share to make the numbers match the bank.
    If the shares add up to more than the bank holds, reconciliation says so loudly instead.
  * Nothing here knows about "TUN": the host alliance is just the registered alliance flagged is_host.
"""
from __future__ import annotations

import json

from . import ledger as L
from . import money as M
from .util import now_iso, sha256_text

RESOURCES = M.RESOURCES


class OffshoreError(L.LedgerError):
    pass


# ------------------------------------------------------------------ mode + registry
def shared_enabled(conn) -> bool:
    return L.get_state(conn, "offshore_shared") == "1"


def registered(conn, alliance_id: int):
    r = conn.execute("SELECT * FROM offshore_alliances WHERE alliance_id=? AND active=1", (alliance_id,)).fetchone()
    return dict(r) if r else None


def alliances(conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM offshore_alliances" + (" WHERE active=1" if active_only else "") + " ORDER BY is_host DESC, alliance_id"
    return [dict(r) for r in conn.execute(sql)]


def host_id(conn) -> int | None:
    r = conn.execute("SELECT alliance_id FROM offshore_alliances WHERE is_host=1 AND active=1").fetchone()
    return r["alliance_id"] if r else None


def register(conn, *, alliance_id: int, name: str, actor: str, role_id: str | None = None, is_host: bool = False,
             note: str | None = None) -> dict:
    old = conn.execute("SELECT * FROM offshore_alliances WHERE alliance_id=?", (alliance_id,)).fetchone()
    if old:
        conn.execute("UPDATE offshore_alliances SET name=?, role_id=COALESCE(?, role_id), active=1, note=COALESCE(?, note) WHERE alliance_id=?",
                     (name, role_id, note, alliance_id))
    else:
        conn.execute("INSERT INTO offshore_alliances(alliance_id,name,is_host,role_id,active,note,added_by,added_at) VALUES(?,?,?,?,1,?,?,?)",
                     (alliance_id, name, 1 if is_host else 0, role_id, note, str(actor), now_iso()))
    L.audit(conn, actor, "OFFSHORE_ALLIANCE_REGISTERED", f"alliance:{alliance_id}", {"name": name, "role_id": role_id, "host": is_host})
    return dict(conn.execute("SELECT * FROM offshore_alliances WHERE alliance_id=?", (alliance_id,)).fetchone())


def deactivate(conn, *, alliance_id: int, actor: str) -> None:
    a = conn.execute("SELECT * FROM offshore_alliances WHERE alliance_id=?", (alliance_id,)).fetchone()
    if not a:
        raise OffshoreError("That alliance is not registered.")
    if a["is_host"]:
        raise OffshoreError("The host alliance can't be removed.")
    if balances(conn, alliance_id):
        raise OffshoreError("That alliance still has a share in the offshore. Withdraw it, or move it with /offshore reassign, first.")
    if held(conn, alliance_id):
        raise OffshoreError("That alliance has a payout in progress.")
    conn.execute("UPDATE offshore_alliances SET active=0 WHERE alliance_id=?", (alliance_id,))
    L.audit(conn, actor, "OFFSHORE_ALLIANCE_DEACTIVATED", f"alliance:{alliance_id}", {})


# ------------------------------------------------------------------ balances
def balances(conn, alliance_id: int) -> dict:
    return {r["resource"]: r["amount"] for r in conn.execute(
        "SELECT resource, amount FROM offshore_balances WHERE alliance_id=? AND amount>0", (alliance_id,))}


def all_balances(conn) -> dict:
    out: dict = {}
    for r in conn.execute("SELECT alliance_id, resource, amount FROM offshore_balances WHERE amount>0"):
        out.setdefault(r["alliance_id"], {})[r["resource"]] = r["amount"]
    return out


def totals(conn) -> dict:
    out: dict = {}
    for r in conn.execute("SELECT resource, SUM(amount) a FROM offshore_balances WHERE amount>0 GROUP BY resource"):
        out[r["resource"]] = r["a"]
    return out


def held(conn, alliance_id: int) -> dict:
    """What payouts that are planned / in flight have already promised out of this alliance's share."""
    out: dict = {}
    for r in conn.execute("SELECT amounts_json FROM offshore_transfers WHERE direction='PAYOUT' AND alliance_id=? "
                          "AND status IN ('PLANNED','PENDING','UNCERTAIN')", (alliance_id,)):
        out = M.add(out, json.loads(r["amounts_json"]))
    return out


def spendable(conn, alliance_id: int) -> dict:
    return {r: v for r, v in M.sub(balances(conn, alliance_id), held(conn, alliance_id)).items() if v > 0}


def unassigned(physical: dict, tot: dict) -> dict:
    """physical - assigned, per resource (signed: a negative number means the shares add up to MORE than the bank holds)."""
    out = {}
    for r in RESOURCES:
        d = physical.get(r, 0) - tot.get(r, 0)
        if d:
            out[r] = d
    return out


# ------------------------------------------------------------------ the chained entries
def _hash(prev: str, e: dict, ts: str) -> str:
    return sha256_text("|".join(str(x) for x in (prev, ts, e["group_id"], e["alliance_id"], e["resource"], e["delta"], e["entry_type"],
                                                  e.get("pnw_record_id"), e.get("transfer_id"), e["actor"], e.get("note"))))


def post(conn, entries: list[dict]) -> None:
    row = conn.execute("SELECT entry_hash FROM offshore_entries ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["entry_hash"] if row else L.GENESIS
    for e in entries:
        ts = now_iso()
        h = _hash(prev, e, ts)
        conn.execute("INSERT INTO offshore_entries(ts,group_id,alliance_id,resource,delta,entry_type,pnw_record_id,transfer_id,actor,note,"
                     "prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, e["group_id"], e["alliance_id"], e["resource"], e["delta"], e["entry_type"], e.get("pnw_record_id"),
                      e.get("transfer_id"), str(e["actor"]), e.get("note"), prev, h))
        prev = h


def verify_chain(conn) -> dict:
    prev, n = L.GENESIS, 0
    for r in conn.execute("SELECT * FROM offshore_entries ORDER BY id"):
        e = dict(r)
        if r["prev_hash"] != prev or _hash(prev, e, r["ts"]) != r["entry_hash"]:
            return {"ok": False, "count": n, "broken_at": r["id"]}
        prev, n = r["entry_hash"], n + 1
    return {"ok": True, "count": n}


def cache_matches(conn) -> bool:
    sums = {(r["alliance_id"], r["resource"]): r["s"] for r in conn.execute(
        "SELECT alliance_id, resource, SUM(delta) s FROM offshore_entries GROUP BY 1,2 HAVING SUM(delta) != 0")}
    cache = {(r["alliance_id"], r["resource"]): r["amount"] for r in conn.execute("SELECT * FROM offshore_balances WHERE amount != 0")}
    return sums == cache


# ------------------------------------------------------------------ the operations
def enable_shared(conn, *, physical: dict, actor: str, host_alliance_id: int, host_name: str) -> dict:
    """Switch shared mode on WITHOUT changing anything for the host: everything the offshore holds right now is assigned to
    the host alliance (an OPENING entry per resource, documented). ECON then splits it with /offshore reassign."""
    if shared_enabled(conn):
        raise OffshoreError("Shared offshore mode is already on.")
    L.assert_can_mutate(conn, None, "adjust")
    register(conn, alliance_id=host_alliance_id, name=host_name, actor=actor, is_host=True,
             note="the alliance that runs this bot")
    g = L.new_group("OFFS")
    post(conn, [dict(group_id=g, alliance_id=host_alliance_id, resource=r, delta=v, entry_type="OPENING", actor=actor,
                     note="Shared offshore enabled: everything physically in the offshore at that moment starts as the host's share")
                for r, v in physical.items() if v > 0 and r in RESOURCES])
    L.set_state(conn, "offshore_shared", "1")
    L.audit(conn, actor, "OFFSHORE_SHARED_ENABLED", None, {"assigned_to_host": physical})
    return {"assigned": {r: v for r, v in physical.items() if v > 0}}


def assign(conn, *, alliance_id: int, amounts: dict, physical: dict, actor: str, note: str) -> None:
    """Give an alliance a share of funds that are PHYSICALLY in the offshore but not yet assigned to anyone."""
    L.assert_can_mutate(conn, None, "adjust")
    if not registered(conn, alliance_id):
        raise OffshoreError("That alliance is not registered.")
    free = {r: max(0, v) for r, v in unassigned(physical, totals(conn)).items()}
    short = [f"{M.LABELS[r]} (unassigned: {M.fmt_units(r, free.get(r, 0))})" for r, a in amounts.items() if a > free.get(r, 0)]
    if short:
        raise OffshoreError("The offshore doesn't physically hold enough UNASSIGNED funds for: " + ", ".join(short)
                            + ". A share can only be assigned from funds that are really there.")
    g = L.new_group("OFFS")
    post(conn, [dict(group_id=g, alliance_id=alliance_id, resource=r, delta=a, entry_type="OPENING", actor=actor, note=note)
                for r, a in amounts.items()])
    L.audit(conn, actor, "OFFSHORE_ASSIGNED", f"alliance:{alliance_id}", {"amounts": amounts, "note": note})


def reassign(conn, *, from_id: int, to_id: int, amounts: dict, actor: str, note: str) -> None:
    """Move part of one alliance's share to another. The physical bank doesn't change; the total owned doesn't change."""
    L.assert_can_mutate(conn, None, "adjust")
    if from_id == to_id:
        raise OffshoreError("Choose two different alliances.")
    for a in (from_id, to_id):
        if not registered(conn, a):
            raise OffshoreError(f"Alliance #{a} is not registered.")
    free = spendable(conn, from_id)
    short = [M.LABELS[r] for r, a in amounts.items() if a > free.get(r, 0)]
    if short:
        raise OffshoreError("The sending alliance's share doesn't cover: " + ", ".join(short))
    g = L.new_group("OFFS")
    rows = []
    for r, a in amounts.items():
        rows.append(dict(group_id=g, alliance_id=from_id, resource=r, delta=-a, entry_type="TRANSFER", actor=actor, note=note))
        rows.append(dict(group_id=g, alliance_id=to_id, resource=r, delta=a, entry_type="TRANSFER", actor=actor, note=note))
    post(conn, rows)
    L.audit(conn, actor, "OFFSHORE_REASSIGNED", f"alliance:{from_id}->{to_id}", {"amounts": amounts, "note": note})


def release(conn, *, alliance_id: int, amounts: dict, actor: str, note: str) -> None:
    """Correction: take funds out of an alliance's share (they become unassigned). Documented, never silent."""
    L.assert_can_mutate(conn, None, "adjust")
    free = spendable(conn, alliance_id)
    short = [M.LABELS[r] for r, a in amounts.items() if a > free.get(r, 0)]
    if short:
        raise OffshoreError("That share doesn't hold enough of: " + ", ".join(short))
    g = L.new_group("OFFS")
    post(conn, [dict(group_id=g, alliance_id=alliance_id, resource=r, delta=-a, entry_type="ADJUSTMENT", actor=actor, note=note)
                for r, a in amounts.items()])
    L.audit(conn, actor, "OFFSHORE_RELEASED", f"alliance:{alliance_id}", {"amounts": amounts, "note": note})


def credit_record(conn, n: dict, *, alliance_id: int, actor: str) -> dict:
    """A real PnW deposit into the offshore -> that alliance's share goes up (once per record)."""
    g = L.new_group("OFFS")
    rows = [dict(group_id=g, alliance_id=alliance_id, resource=r, delta=a, entry_type="DEPOSIT", pnw_record_id=n["id"], actor=actor,
                 note=f"PnW record #{n['id']}") for r, a in n["amounts"].items() if a > 0 and r in RESOURCES]
    done = {(r["resource"]) for r in conn.execute("SELECT resource FROM offshore_entries WHERE pnw_record_id=? AND alliance_id=? "
                                                  "AND entry_type='DEPOSIT'", (n["id"], alliance_id))}
    rows = [x for x in rows if x["resource"] not in done]
    post(conn, rows)
    return {x["resource"]: x["delta"] for x in rows}


def debit_record(conn, n: dict, *, alliance_id: int, actor: str, transfer_id: int | None = None) -> dict:
    """A real PnW outflow from the offshore -> that alliance's share goes down. All or nothing per resource: if the share
    doesn't cover a resource it is NOT debited (the shortfall is returned for ECON to resolve; nothing is hidden)."""
    bal = balances(conn, alliance_id)
    done = {r["resource"] for r in conn.execute("SELECT resource FROM offshore_entries WHERE pnw_record_id=? AND alliance_id=? "
                                                "AND entry_type='WITHDRAWAL'", (n["id"], alliance_id))}
    g = L.new_group("OFFS")
    rows, short = [], {}
    for r, a in n["amounts"].items():
        if a <= 0 or r not in RESOURCES or r in done:
            continue
        if bal.get(r, 0) >= a:
            rows.append(dict(group_id=g, alliance_id=alliance_id, resource=r, delta=-a, entry_type="WITHDRAWAL", pnw_record_id=n["id"],
                             transfer_id=transfer_id, actor=actor, note=f"PnW record #{n['id']}"))
        else:
            short[r] = a - bal.get(r, 0)
    post(conn, rows)
    return short


def host_share(conn, physical: dict | None = None) -> dict:
    """The host's spendable share, never more than what is physically in the offshore."""
    hid = host_id(conn)
    if hid is None:
        return {}
    share = balances(conn, hid)
    if physical is not None:
        share = {r: min(v, physical.get(r, 0)) for r, v in share.items() if min(v, physical.get(r, 0)) > 0}
    return share


# ------------------------------------------------------------------ views
def summary(conn, physical: dict) -> dict:
    tot = totals(conn)
    owners = []
    bal = all_balances(conn)
    for a in alliances(conn):
        owners.append({"alliance_id": a["alliance_id"], "name": a["name"], "is_host": bool(a["is_host"]),
                       "balances": bal.get(a["alliance_id"], {}), "held": held(conn, a["alliance_id"])})
    return {"physical": physical, "owners": owners, "totals": tot, "unassigned": unassigned(physical, tot)}


def history(conn, alliance_id: int, limit: int = 15) -> list:
    return conn.execute("SELECT * FROM offshore_entries WHERE alliance_id=? ORDER BY id DESC LIMIT ?", (alliance_id, limit)).fetchall()
