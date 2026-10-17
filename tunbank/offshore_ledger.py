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
def enable_shared(conn, *, actor: str, host_alliance_id: int, host_name: str) -> dict:
    """Switch shared mode on. NOTHING moves and NO share is assigned: every alliance starts at zero and the whole physical
    offshore shows as 'unassigned'. An Admin then records each alliance's real starting share with /offshore adjustownership."""
    if shared_enabled(conn):
        raise OffshoreError("Shared offshore mode is already on.")
    L.assert_can_mutate(conn, None, "adjust")
    register(conn, alliance_id=host_alliance_id, name=host_name, actor=actor, is_host=True, note="the alliance that runs this bot")
    L.set_state(conn, "offshore_shared", "1")
    L.set_state(conn, "offshore_shared_since", now_iso())
    L.audit(conn, actor, "OFFSHORE_SHARED_ENABLED", None, {"assigned": {}})
    return {}


def correct_ownership(conn, *, alliance_id: int, targets: dict, physical: dict, actor: str, reason: str,
                      replace_all: bool = False) -> dict:
    """Set an alliance's RECORDED share to the given amounts (an initial accounting correction). Moves nothing in PnW.

    Resources not listed are left alone unless replace_all (then they become zero). The result is compared with the
    real offshore and any discrepancy is REPORTED, never hidden or auto-fixed. Fully audited: who, why, when, before, after."""
    L.assert_can_mutate(conn, None, "adjust")
    if len((reason or "").strip()) < 5:
        raise OffshoreError("A reason is required.")
    if not registered(conn, alliance_id):
        raise OffshoreError("That alliance is not registered.")
    before = balances(conn, alliance_id)
    hold = held(conn, alliance_id)
    resources = set(targets) | (set(before) if replace_all else set())
    rows, after = [], dict(before)
    g = L.new_group("OFFS")
    for r in sorted(resources):
        want = targets.get(r, 0)
        if want < 0:
            raise OffshoreError("A share can't be negative.")
        if want < hold.get(r, 0):
            raise OffshoreError(f"{M.LABELS[r]} can't be set below what payouts in progress have already promised ({M.fmt_units(r, hold[r])}).")
        d = want - before.get(r, 0)
        if d == 0:
            continue
        rows.append(dict(group_id=g, alliance_id=alliance_id, resource=r, delta=d, entry_type="OPENING" if d > 0 else "ADJUSTMENT",
                         actor=actor, note=f"[OWNERSHIP CORRECTION] {reason}"))
        if want:
            after[r] = want
        else:
            after.pop(r, None)
    if not rows:
        raise OffshoreError("That is already what's recorded; nothing to change.")
    post(conn, rows)
    tot = totals(conn)
    conn.execute("INSERT INTO offshore_corrections(ts,actor,alliance_id,reason,before_json,after_json,physical_json,totals_after_json,group_id) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (now_iso(), str(actor), alliance_id, reason, json.dumps(before, sort_keys=True),
                                                json.dumps(after, sort_keys=True), json.dumps(physical, sort_keys=True),
                                                json.dumps(tot, sort_keys=True), g))
    L.audit(conn, actor, "OFFSHORE_OWNERSHIP_CORRECTED", f"alliance:{alliance_id}",
            {"before": before, "after": after, "reason": reason, "physical": physical, "shares_total": tot})
    return {"before": before, "after": after, "totals": tot, "unassigned": unassigned(physical, tot)}


def preview_correction(conn, *, alliance_id: int, targets: dict, replace_all: bool) -> dict:
    """What the alliance's share would be after a correction, and the combined shares of everyone (no writes)."""
    before = balances(conn, alliance_id)
    after = dict(before)
    for r in (set(targets) | (set(before) if replace_all else set())):
        want = targets.get(r, 0)
        if want:
            after[r] = want
        else:
            after.pop(r, None)
    tot = dict(totals(conn))
    for r in set(before) | set(after):
        tot[r] = tot.get(r, 0) - before.get(r, 0) + after.get(r, 0)
    return {"before": before, "after": after, "totals_after": {r: v for r, v in tot.items() if v}}


def analysis(physical: dict, tot: dict, snapshot=None) -> dict:
    """Resource by resource AND overall value, kept apart so a surplus in one resource never hides a shortage in another."""
    from .valuation import value_amounts

    rows = {}
    for r in RESOURCES:
        p, o = physical.get(r, 0), tot.get(r, 0)
        if p or o:
            rows[r] = {"physical": p, "owned": o, "difference": p - o,
                       "state": "SHORT" if o > p else ("EXCESS" if p > o else "OK")}
    vo, vp = value_amounts({r: v["owned"] for r, v in rows.items()}, snapshot), value_amounts({r: v["physical"] for r, v in rows.items()}, snapshot)
    usable = vo.usable_for_limits and vp.usable_for_limits and vo.total_cents is not None and vp.total_cents is not None
    return {"resources": rows, "value_owned": vo.total_cents if usable else None, "value_physical": vp.total_cents if usable else None,
            "net_cents": (vp.total_cents - vo.total_cents) if usable else None,
            "missing_prices": sorted(set(vo.missing) | set(vp.missing))}


def unexplained(conn, offshore_id: int | None, limit: int = 25) -> list[dict]:
    """PnW records that touched the offshore bank AFTER shared mode was switched on and are not part of any alliance's share
    yet. They are never assigned automatically: ECON investigates, then attributes them (or not)."""
    if not offshore_id:
        return []
    since = L.get_state(conn, "offshore_shared_since") or ""
    rows = conn.execute(
        "SELECT p.id, p.direction, p.sender_id, p.sender_type, p.receiver_id, p.receiver_type, p.amounts_json, p.note, p.first_seen_at "
        "FROM pnw_records p WHERE (p.sender_id=? OR p.receiver_id=?) AND p.first_seen_at >= ? "
        "AND p.status NOT IN ('BASELINE','DISMISSED') "
        "AND NOT EXISTS (SELECT 1 FROM offshore_entries e WHERE e.pnw_record_id = p.id) "
        "AND NOT (p.sender_id=? AND p.receiver_id=?) ORDER BY p.id DESC LIMIT ?",
        (offshore_id, offshore_id, since, offshore_id, offshore_id, limit)).fetchall()
    out = []
    for r in rows:
        amounts = json.loads(r["amounts_json"])
        if not any(v > 0 for v in amounts.values()):
            continue
        out.append(dict(r) | {"amounts": amounts, "inbound": r["receiver_id"] == offshore_id})
    return out


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


WIPE_PHRASE = "WIPE OFFSHORE ASSIGNMENTS"


def wipe_assignments(conn, *, actor: str, reason: str, include_host: bool) -> dict:
    """Clear every alliance's recorded share of the offshore so the funds can be assigned again from scratch.

    NOTHING IS DELETED OR REWRITTEN. Each share is brought to zero with reversing ADJUSTMENT entries (all in one group, so the
    wipe is one identifiable event), exactly like /offshore release does for one alliance. The funds stay in the offshore and
    simply become "unassigned". Member balances, PnW and every other record are untouched. ALL-OR-NOTHING: it refuses while
    anything is in flight, and checks the result before it commits (the caller's transaction rolls back on any error).
    """
    if not (reason or "").strip():
        raise OffshoreError("A reason is required.")
    if not shared_enabled(conn):
        raise OffshoreError("The offshore is not in shared mode, so there are no alliance shares to clear.")
    L.assert_can_mutate(conn, None, "adjust")
    targets = []
    for a in alliances(conn, active_only=False):
        if a["is_host"] and not include_host:
            continue
        bal = balances(conn, a["alliance_id"])
        if bal:
            targets.append((a, bal))
    if not targets:
        raise OffshoreError("No alliance has a share assigned, so there is nothing to clear.")
    stuck = [a["name"] for a, _ in targets if held(conn, a["alliance_id"])]
    if stuck:
        raise OffshoreError("Payouts are still planned or in flight for: " + ", ".join(stuck) + ". Wait for them to finish, or resolve them, first.")
    busy_tx = conn.execute("SELECT COUNT(*) FROM transactions WHERE status IN ('CONFIRMED','RECONCILIATION_REQUIRED')").fetchone()[0]
    busy_off = conn.execute("SELECT COUNT(*) FROM offshore_transfers WHERE status IN ('PLANNED','PENDING','UNCERTAIN')").fetchone()[0]
    if busy_tx or busy_off:
        raise OffshoreError(f"{busy_tx} withdrawal(s) and {busy_off} offshore transfer(s) are still in flight. "
                            "Clearing shares now could leave one of them unable to debit its share; wait until they finish.")
    g = L.new_group("OFFS")
    note = f"ASSIGNMENT WIPE: {reason.strip()}"
    post(conn, [dict(group_id=g, alliance_id=a["alliance_id"], resource=r, delta=-amt, entry_type="ADJUSTMENT", actor=actor, note=note)
                for a, bal in targets for r, amt in bal.items()])
    for a, _ in targets:                                    # verify before committing: every share is exactly zero
        if balances(conn, a["alliance_id"]):
            raise OffshoreError(f"Safety stop: {a['name']}'s share is not zero after the wipe. Nothing was changed.")
    if not cache_matches(conn):
        raise OffshoreError("Safety stop: stored shares do not match their ledger entries after the wipe. Nothing was changed.")
    previous = {str(a["alliance_id"]): {"name": a["name"], "is_host": bool(a["is_host"]), "amounts": bal} for a, bal in targets}
    L.audit(conn, actor, "OFFSHORE_ASSIGNMENTS_WIPED", "offshore", {"group_id": g, "reason": reason.strip(), "include_host": include_host,
                                                                   "previous": previous})
    return {"group_id": g, "previous": previous, "cleared": len(targets)}


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
def summary(conn, physical: dict, snapshot=None, offshore_id: int | None = None) -> dict:
    tot = totals(conn)
    bal = all_balances(conn)
    owners = [{"alliance_id": a["alliance_id"], "name": a["name"], "is_host": bool(a["is_host"]),
               "balances": bal.get(a["alliance_id"], {}), "held": held(conn, a["alliance_id"])} for a in alliances(conn)]
    an = analysis(physical, tot, snapshot)
    return {"physical": physical, "owners": owners, "totals": tot, "unassigned": unassigned(physical, tot),
            "analysis": an, "unexplained": unexplained(conn, offshore_id)}


def history(conn, alliance_id: int, limit: int = 15) -> list:
    return conn.execute("SELECT * FROM offshore_entries WHERE alliance_id=? ORDER BY id DESC LIMIT ?", (alliance_id, limit)).fetchall()
