"""Funded balance transfers (/bank credit): a credit to a member balance ALWAYS has an equal, recorded debit.

Sources of the money:
  ALLIANCE   alliance-owned funds = what the real bank holds beyond what members are owed (the vault dashboard figure).
  MEMBER     another member's AVAILABLE balance.
  OWNERSHIP  another registered alliance's share of the shared offshore (moved to the host alliance, whose members hold the claim).

Rules (the same principles as the rest of the bank):
  * Nothing is created from nowhere. The two sides of every transfer are stored together and add up to exactly zero.
  * An insufficient source is refused. Only an ADMIN may override a deficit (ALLIANCE / MEMBER sources), with a written
    reason; the override is flagged on the record and in the audit log. An ownership share can never go below zero.
  * A second staff member must approve every transfer (same two-person rule as other adjustments).
  * Nothing here touches PnW. Real PnW records stay the only source of deposits and withdrawals.
  * Nothing is edited or deleted afterwards; a mistake is corrected by a new, documented entry.
"""
from __future__ import annotations

import json

from . import configaudit as CA
from . import ledger as L
from . import money as M
from . import notify
from . import offshore_ledger as OL
from .util import jdump, now_iso, parse_iso, utcnow

SOURCES = ("ALLIANCE", "MEMBER", "OWNERSHIP")


class TransferError(L.LedgerError):
    pass


def ref_of(transfer_id: int) -> str:
    return f"TUN-ADJ{transfer_id}"


def payload_for(*, recipient: int, amounts: dict, source_type: str, source_id, reason: str, reference: str,
                override: bool) -> dict:
    """What a second approver signs off on. Compared byte-for-byte when the requester runs the command again."""
    return {"op": "TRANSFER", "recipient": int(recipient), "amounts": {r: int(a) for r, a in sorted(amounts.items())},
            "source_type": source_type, "source_id": source_id, "reason": reason.strip(), "reference": (reference or "").strip(),
            "override": bool(override)}


# ------------------------------------------------------------------ what each source can fund
def alliance_pool(conn, holdings: dict | None) -> dict | None:
    """Alliance-owned funds per resource (never negative), or None when the real bank could not be read."""
    if holdings is None:
        return None
    from .reconcile import bank_position

    owned = bank_position(conn, holdings)["alliance_owned"] or {}
    return {r: v for r, v in owned.items() if v > 0}


def member_available(conn, nation_id: int) -> dict:
    return {r: v for r, v in L.spendable(conn, nation_id).items() if v > 0}


def source_available(conn, source_type: str, source_id, holdings: dict | None) -> dict | None:
    if source_type == "ALLIANCE":
        return alliance_pool(conn, holdings)
    if source_type == "MEMBER":
        return member_available(conn, int(source_id))
    if source_type == "OWNERSHIP":
        return OL.spendable(conn, int(source_id))
    raise TransferError("Unknown source.")


def shortfall(avail: dict | None, amounts: dict) -> dict:
    """{resource: how much the source is short}. An unreadable alliance pool counts as short of everything."""
    if avail is None:
        return dict(amounts)
    return {r: a - avail.get(r, 0) for r, a in amounts.items() if a > avail.get(r, 0)}


# ------------------------------------------------------------------ approval
def _check_approval(conn, approval_id, payload: dict, operator: str) -> None:
    if approval_id is None:
        raise TransferError("Every funded transfer needs a second staff member's approval.")
    ap = L.get_approval(conn, approval_id)
    if not ap or ap["kind"] != "ADJUSTMENT":
        raise TransferError("That approval does not exist.")
    if ap["status"] != "PENDING" or not ap["approved_by"]:
        raise TransferError("That approval is not approved yet (or was already used / revoked).")
    if ap["approved_by"] == ap["requested_by"] or str(ap["requested_by"]) != str(operator):
        raise TransferError("No valid second-person approval for this transfer.")
    if parse_iso(ap["expires_at"]) < utcnow():
        raise TransferError("That approval has expired.")
    if ap["payload_json"] != jdump(payload):
        raise TransferError("That approval was for a different transfer.")


# ------------------------------------------------------------------ the transfer
def post_transfer(conn, *, recipient: int, amounts: dict, source_type: str, source_id, reason: str, reference: str,
                  operator: str, approval_id, idem: str, holdings: dict | None, snapshot_id=None,
                  override: bool = False, override_reason: str = "") -> dict:
    """Validate everything, then write both sides inside the CALLER's transaction. Returns {transfer_id, ref, ...}."""
    old = conn.execute("SELECT id FROM balance_transfers WHERE idempotency_key=?", (idem,)).fetchone()
    if old:
        return {"transfer_id": old["id"], "ref": ref_of(old["id"]), "duplicate": True}
    amounts = {r: int(a) for r, a in amounts.items() if a}
    if not amounts:
        raise TransferError("Nothing to transfer.")
    for r, a in amounts.items():
        if r not in M.RESOURCES:
            raise TransferError(f"Unknown resource {r}.")
        if a <= 0:
            raise TransferError("Amounts must be positive.")
    if source_type not in SOURCES:
        raise TransferError("Choose where the money comes from: alliance funds, a member, or an alliance ownership account.")
    if not reason.strip():
        raise TransferError("A reason is mandatory.")
    if override and not override_reason.strip():
        raise TransferError("A deficit override needs a written justification.")
    if override and source_type == "OWNERSHIP":
        raise TransferError("An alliance's ownership share can never be taken below zero, so it cannot be overridden.")
    L.assert_can_mutate(conn, recipient, "adjust")
    if not L.get_member(conn, recipient):
        raise TransferError(f"Nation {recipient} has no TUN Bank account. Link the nation first - an account is never created silently.")
    if source_type == "MEMBER":
        if source_id is None or not L.get_member(conn, int(source_id)):
            raise TransferError("The source member has no TUN Bank account.")
        if int(source_id) == recipient:
            raise TransferError("The source and the recipient are the same account.")
        L.assert_can_mutate(conn, int(source_id), "adjust")
    if source_type == "OWNERSHIP":
        if not OL.shared_enabled(conn):
            raise TransferError("Shared offshore is not switched on, so there are no alliance ownership accounts.")
        host = OL.host_id(conn)
        if host is None:
            raise TransferError("No host alliance is registered.")
        if source_id is None or not OL.registered(conn, int(source_id)):
            raise TransferError("That alliance is not registered for the shared offshore.")
        if int(source_id) == host:
            raise TransferError("That is the host alliance itself - use alliance funds as the source.")

    payload = payload_for(recipient=recipient, amounts=amounts, source_type=source_type, source_id=source_id,
                          reason=reason, reference=reference, override=override)
    _check_approval(conn, approval_id, payload, operator)

    # --- the source must really cover it (re-checked here, inside the transaction, not only on the preview screen)
    avail = source_available(conn, source_type, source_id, holdings)
    short = shortfall(avail, amounts)
    if short and not override:
        raise TransferError("The source does not cover this: " + ", ".join(
            f"{M.LABELS[r]} short by {M.fmt_units(r, v)}" for r, v in short.items())
            + (". The real bank could not be read, so alliance funds cannot be verified." if avail is None else "."))
    if source_type == "MEMBER":
        debit = {r: -a for r, a in amounts.items()}
        L.adjustment_debt(conn, int(source_id), debit)         # refuses while a pending withdrawal holds the funds

    # --- both sides
    g = L.new_group("ADJ")
    cur = conn.execute(
        "INSERT INTO balance_transfers(idempotency_key,recipient_nation_id,source_type,source_id,reason,reference,operator,approval_id,"
        "deficit_override,override_reason,override_by,price_snapshot_id,group_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (idem, recipient, source_type, None if source_type == "ALLIANCE" else int(source_id), reason.strip(), (reference or "").strip() or None,
         str(operator), approval_id, 1 if (override and short) else 0, override_reason.strip() or None if (override and short) else None,
         str(operator) if (override and short) else None, snapshot_id, g, now_iso()))
    tid = cur.lastrowid
    ref = ref_of(tid)
    evidence = f"{ref}" + (f" | {reference.strip()}" if (reference or "").strip() else "")
    note = f"{reason.strip()} | {ref}"

    def adj_row(nation, deltas):
        c = conn.execute("INSERT INTO adjustments(nation_id,bucket,reason,evidence,actor,approval_id,price_snapshot_id,created_at) "
                         "VALUES(?, 'AVAILABLE', ?,?,?,?,?,?)", (nation, f"{reason.strip()} ({ref})", evidence, str(operator), approval_id, snapshot_id, now_iso()))
        for r, d in deltas.items():
            conn.execute("INSERT INTO adjustment_items(adjustment_id,resource,delta) VALUES(?,?,?)", (c.lastrowid, r, d))
        return c.lastrowid

    before_r = L.snapshot_accounts(conn, recipient)
    rows = []
    r_adj = adj_row(recipient, amounts)
    rows += [dict(group_id=g, nation_id=recipient, bucket="AVAILABLE", resource=r, delta=a, entry_type="ADJUSTMENT",
                  adjustment_id=r_adj, actor=operator, note=note, price_snapshot_id=snapshot_id) for r, a in amounts.items()]
    lines = [("MEMBER", recipient, r, a) for r, a in amounts.items()]
    s_adj, before_s = None, None
    if source_type == "MEMBER":
        sid = int(source_id)
        before_s = L.snapshot_accounts(conn, sid)
        s_adj = adj_row(sid, {r: -a for r, a in amounts.items()})
        rows += [dict(group_id=g, nation_id=sid, bucket="AVAILABLE", resource=r, delta=-a, entry_type="ADJUSTMENT",
                      adjustment_id=s_adj, actor=operator, note=note, price_snapshot_id=snapshot_id) for r, a in amounts.items()]
        lines += [("MEMBER", sid, r, -a) for r, a in amounts.items()]
    elif source_type == "OWNERSHIP":
        OL.reassign(conn, from_id=int(source_id), to_id=OL.host_id(conn), amounts=amounts, actor=operator,
                    note=f"{ref}: funds a member credit. {reason.strip()}")
        lines += [("OWNERSHIP", int(source_id), r, -a) for r, a in amounts.items()]
    else:
        lines += [("ALLIANCE", None, r, -a) for r, a in amounts.items()]
    L.post_entries(conn, rows)
    for t, acc, r, d in lines:
        conn.execute("INSERT INTO balance_transfer_lines(transfer_id,account_type,account_id,resource,delta) VALUES(?,?,?,?,?)",
                     (tid, t, acc, r, d))
    after_r = L.snapshot_accounts(conn, recipient)
    CA.record(conn, actor=operator, setting="balance_transfer", previous=CA.amounts_text(before_r["available"]),
              new=CA.amounts_text(after_r["available"]),
              target=f"nation [#{recipient}] · {ref} · from {source_type}{'' if source_id is None else ' #' + str(source_id)} · reason: {reason}",
              category="ACCOUNTING", only_if_changed=False)
    L.audit(conn, operator, "BALANCE_TRANSFER", f"balance_transfer:{tid}", {
        "ref": ref, "recipient": recipient, "amounts": amounts, "source_type": source_type, "source_id": source_id,
        "reason": reason, "reference": reference, "approval_id": approval_id,
        "deficit_override": bool(override and short), "override_reason": override_reason if (override and short) else None,
        "shortfall": short or None})
    if override and short:
        L.audit(conn, operator, "BALANCE_TRANSFER_DEFICIT_OVERRIDE", f"balance_transfer:{tid}",
                {"ref": ref, "shortfall": short, "justification": override_reason})
    if approval_id:
        L.finish_approval(conn, approval_id, "EXECUTED", {"transfer_id": tid, "ref": ref})
    src_text = {"ALLIANCE": "Alliance funds", "MEMBER": f"Another member's balance (nation #{source_id})",
                "OWNERSHIP": f"An alliance ownership account (alliance #{source_id})"}[source_type]
    notify.enqueue(conn, dedupe_key=f"xfer:{tid}:r", nation_id=recipient, kind="CREDIT", title="Balance credit",
                   deltas=amounts, before=before_r["available"], after=after_r["available"], reason=reason.strip(),
                   reference=ref + (f" · {reference.strip()}" if (reference or "").strip() else ""), source=src_text,
                   origin="Through TUN Bank")
    if source_type == "MEMBER":
        notify.enqueue(conn, dedupe_key=f"xfer:{tid}:s", nation_id=int(source_id), kind="DEBIT", title="Balance transferred out",
                       deltas={r: -a for r, a in amounts.items()}, before=before_s["available"],
                       after=L.snapshot_accounts(conn, int(source_id))["available"], reason=reason.strip(),
                       reference=ref, source=f"Credited to nation #{recipient}", origin="Through TUN Bank")
    return {"transfer_id": tid, "ref": ref, "duplicate": False, "shortfall": short, "overridden": bool(override and short),
            "before": before_r, "after": after_r, "source_before": before_s,
            "source_after": L.snapshot_accounts(conn, int(source_id)) if source_type == "MEMBER" else None}


# ------------------------------------------------------------------ verification (called by reconciliation)
def verify(conn) -> list[dict]:
    """Every transfer's journal must add up to zero per resource, and its member lines must match the real ledger entries."""
    bad = []
    for r in conn.execute("SELECT transfer_id, resource, SUM(delta) s FROM balance_transfer_lines GROUP BY transfer_id, resource HAVING s != 0"):
        bad.append({"transfer_id": r["transfer_id"], "problem": f"{r['resource']} does not balance (off by {r['s']})"})
    for t in conn.execute("SELECT id, group_id FROM balance_transfers"):
        led = {(x["nation_id"], x["resource"]): x["s"] for x in conn.execute(
            "SELECT nation_id, resource, SUM(delta) s FROM ledger_entries WHERE group_id=? AND entry_type='ADJUSTMENT' GROUP BY nation_id, resource", (t["group_id"],))}
        jr = {(x["account_id"], x["resource"]): x["s"] for x in conn.execute(
            "SELECT account_id, resource, SUM(delta) s FROM balance_transfer_lines WHERE transfer_id=? AND account_type='MEMBER' GROUP BY account_id, resource", (t["id"],))}
        if led != jr:
            bad.append({"transfer_id": t["id"], "problem": "member lines differ from the ledger entries"})
    return bad
