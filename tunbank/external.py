"""External withdrawals: money that left the alliance OFFSHORE for a linked member WITHOUT going through TUN Bank
(in-game, or another bot). It really left the bank, so the member's recorded balance must go down by the same amount.

The bot only deducts automatically when it can be confident this is a member withdrawal. Anything doubtful goes to
`/bank review` and nothing is guessed. The verdict (`decide`) is deliberately strict:

  AUTO    the record is from our offshore, to a nation, that is a LINKED TUN Bank member who is a current alliance member,
          the note does not look like a grant / payment / expense, the nation is not on the exempt list, and no emergency
          lock is active.
  REVIEW  it looks like it could be a member withdrawal but something is uncertain (explained in the reason).
  SKIP    not applicable (not from the offshore, not to a nation, the recipient has no TUN account...): the existing
          "money left the bank outside TUN Bank" warning applies unchanged.

Recording: a normal guarded ledger ADJUSTMENT (so nothing in the ledger rules changes) plus an immutable row in
`external_withdrawals`, which is what labels it "external withdrawal" everywhere and which the database refuses to accept
unless the real outgoing PnW record exists. One PnW record can only ever be processed once (UNIQUE). The balance may go
negative: the money is already gone, so a debt is recorded truthfully, never hidden or clamped.
"""
from __future__ import annotations

import json

from . import bankrec as B
from . import configaudit as CA
from . import ledger as L
from . import money as M
from . import notify
from .config import cfg_get
from .util import jdump, now_iso

MODES = ("auto", "review", "off")
LABEL = "External withdrawal"          # the one name used everywhere it is displayed or exported


def mode(conn) -> str:
    v = (cfg_get(conn, "external_withdrawal_mode") or "").strip().lower()
    return v if v in MODES else "review"                   # an unknown value can never switch on automatic deduction


def exclude_words(conn) -> list:
    return [w.strip().lower() for w in (cfg_get(conn, "external_withdrawal_exclude_words") or "").split(",") if w.strip()]


def exempt_nations(conn) -> set:
    return {int(x) for x in (cfg_get(conn, "external_withdrawal_exempt_nations") or "").replace(" ", "").split(",") if x.isdigit()}


def decide(conn, n: dict, ctx) -> tuple[str, str]:
    """(verdict, reason) for an untagged outgoing record. See the module docstring."""
    m = mode(conn)
    if m == "off" or not ctx.offshore_id or n["sender_id"] != ctx.offshore_id or n["sender_type"] != 2:
        return "SKIP", ""
    if n["receiver_type"] != 1 or not n["receiver_id"] or n.get("is_tax") or not n["amounts"]:
        return "SKIP", ""
    mem = L.get_member(conn, n["receiver_id"])
    if not mem:
        return "SKIP", ""
    who = f"nation #{n['receiver_id']}"
    if not mem["discord_id"]:
        return "REVIEW", f"The recipient ({who}) has a TUN Bank account but is not linked to a Discord user, so I will not guess."
    if ctx.members is None:
        return "REVIEW", "The alliance member list could not be read right now, so I cannot confirm the recipient is a current member."
    if n["receiver_id"] not in ctx.members:
        return "REVIEW", (f"The recipient ({who}) is not currently in the alliance. It may be a payout for another registered "
                          "alliance or an outside payment, so it is NOT charged to a member automatically.")
    if n["receiver_id"] in exempt_nations(conn):
        return "REVIEW", f"The recipient ({who}) is on the exempt list (alliance expenses), so it is not charged automatically."
    note = (n["note"] or "").lower()
    hit = next((w for w in exclude_words(conn) if w in note), None)
    if hit:
        return "REVIEW", f"The note mentions '{hit}', so this may be a grant or payment rather than a member withdrawal."
    if any(r not in M.RESOURCES for r in n["amounts"]):
        return "REVIEW", "The record contains something that is not a normal resource."
    if L.get_state(conn, "emergency_lock") == "1":
        return "REVIEW", "An emergency lock is active, so no balance is changed until staff review this."
    if m == "review":
        return "REVIEW", "Automatic deduction of external withdrawals is switched to review-only (`external_withdrawal_mode`)."
    return "AUTO", ""


def record_withdrawal(conn, *, record_id: int, nation_id: int, decision: str, decided_by: str, note: str,
                      source_label: str = "offshore", snapshot_id=None) -> dict:
    """Deduct a real outgoing PnW record from the recipient's AVAILABLE balance. Caller owns the transaction.

    Deliberately does NOT call assert_can_mutate: the money already left the bank, so the books must say so even while
    the bank is paused or reconciling. (The caller routes to review during an emergency lock.)
    """
    if decision not in ("AUTO", "STAFF"):
        raise L.LedgerError("Unknown decision.")
    if not (note or "").strip():
        raise L.LedgerError("A reason is required.")
    rec = B.get_record(conn, record_id)
    if rec is None or rec["direction"] != "OUT":
        raise L.LedgerError(f"PnW record {record_id} is not an outgoing record we know about.")
    done = conn.execute("SELECT id FROM external_withdrawals WHERE pnw_record_id=?", (record_id,)).fetchone()
    if done:
        return {"journal_id": done["id"], "duplicate": True}
    if rec["receiver_type"] != 1 or rec["receiver_id"] != nation_id:
        raise L.LedgerError("An external withdrawal is charged to the nation that actually received it; "
                            f"record {record_id} went to nation #{rec['receiver_id']}.")
    if not L.get_member(conn, nation_id):
        raise L.LedgerError(f"Nation {nation_id} has no TUN Bank account; an account is never created silently.")
    amounts = json.loads(rec["amounts_json"])
    if not amounts or any(r not in M.RESOURCES or a <= 0 for r, a in amounts.items()):
        raise L.LedgerError("The record has no valid amounts.")

    before = L.get_balances(conn, nation_id, "AVAILABLE")
    held = L.holds(conn, nation_id, "MEMBER_AVAILABLE")
    g = L.new_group("EXT")
    evidence = f"PnW record #{record_id} · from {source_label} · {decision.lower()} · {LABEL.lower()}"
    cur = conn.execute("INSERT INTO adjustments(nation_id,bucket,reason,evidence,actor,approval_id,price_snapshot_id,created_at) "
                       "VALUES(?, 'AVAILABLE', ?,?,?,NULL,?,?)",
                       (nation_id, f"{LABEL.upper()}: {note.strip()}", evidence, decided_by, snapshot_id, now_iso()))
    adj = cur.lastrowid
    for r, a in amounts.items():
        conn.execute("INSERT INTO adjustment_items(adjustment_id,resource,delta) VALUES(?,?,?)", (adj, r, -a))
    L.post_entries(conn, [dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=r, delta=-a, entry_type="ADJUSTMENT",
                               adjustment_id=adj, actor=decided_by,
                               note=f"{LABEL.upper()} | PnW record #{record_id} | {note.strip()}", price_snapshot_id=snapshot_id)
                          for r, a in amounts.items()])
    after = L.get_balances(conn, nation_id, "AVAILABLE")
    negative = {r: after[r] for r in amounts if after.get(r, 0) < 0}
    conn.execute("INSERT INTO external_withdrawals(pnw_record_id,nation_id,source_bank_id,source_label,amounts_json,before_json,"
                 "after_json,negative_json,decision,decided_by,note,group_id,record_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (record_id, nation_id, rec["sender_id"], source_label, rec["amounts_json"],
                  jdump({r: before.get(r, 0) for r in amounts}), jdump({r: after.get(r, 0) for r in amounts}), jdump(negative),
                  decision, decided_by, note.strip(), g, rec["record_date"], now_iso()))
    jid = conn.execute("SELECT id FROM external_withdrawals WHERE pnw_record_id=?", (record_id,)).fetchone()["id"]
    L.audit(conn, decided_by, "EXTERNAL_WITHDRAWAL", f"pnw:{record_id}", {
        "journal_id": jid, "nation_id": nation_id, "amounts": amounts, "source": source_label, "decision": decision,
        "note": note.strip(), "before": {r: before.get(r, 0) for r in amounts}, "after": {r: after.get(r, 0) for r in amounts},
        "negative": negative or None})
    if negative:
        L.audit(conn, decided_by, "EXTERNAL_WITHDRAWAL_NEGATIVE_BALANCE", f"pnw:{record_id}",
                {"nation_id": nation_id, "resources_below_zero": negative})
        L.raise_event(conn, "WARNING", "EXTERNAL_WITHDRAWAL_DEBT", nation_id=nation_id, ref_type="pnw_record", ref_id=record_id,
                      details={"message": f"An external withdrawal took nation #{nation_id} below zero. The debt is real and recorded; "
                                          "decide how it is settled.", "negative": negative},
                      dedupe_key=f"ext-debt:{record_id}")
    uncovered = {r: held[r] - after.get(r, 0) for r in held if held[r] and after.get(r, 0) < held[r]}
    if uncovered:
        L.raise_event(conn, "WARNING", "EXTERNAL_WITHDRAWAL_UNCOVERS_HOLD", nation_id=nation_id, ref_type="pnw_record", ref_id=record_id,
                      details={"message": "After this external withdrawal the member's balance no longer covers a pending TUN Bank withdrawal.",
                               "short": uncovered}, dedupe_key=f"ext-hold:{record_id}")
    CA.record(conn, actor=decided_by, setting="external_withdrawal", previous=CA.amounts_text({r: before.get(r, 0) for r in amounts}),
              new=CA.amounts_text({r: after.get(r, 0) for r in amounts}),
              target=f"nation [#{nation_id}] · PnW record #{record_id} · {decision.lower()} · {note.strip()}",
              category="ACCOUNTING", only_if_changed=False)
    notify.enqueue(conn, dedupe_key=f"ext:{record_id}", nation_id=nation_id, kind="EXTERNAL_WITHDRAWAL",
                   title="External withdrawal detected", deltas={r: -a for r, a in amounts.items()},
                   before=before, after=after, reason=note.strip(), reference=f"PnW record #{record_id}",
                   source=f"Sent from the alliance {source_label} to your nation",
                   origin="Detected externally (not through TUN Bank)")
    return {"journal_id": jid, "duplicate": False, "before": before, "after": after, "negative": negative, "amounts": amounts}


def verify(conn) -> list:
    """Reconciliation check: every journal row matches its ledger entries and its real PnW record."""
    bad = []
    for e in conn.execute("SELECT id, group_id, nation_id, amounts_json, pnw_record_id FROM external_withdrawals"):
        want = {r: -a for r, a in json.loads(e["amounts_json"]).items()}
        got = {x["resource"]: x["s"] for x in conn.execute(
            "SELECT resource, SUM(delta) s FROM ledger_entries WHERE group_id=? AND nation_id=? AND entry_type='ADJUSTMENT' GROUP BY resource",
            (e["group_id"], e["nation_id"]))}
        if want != got:
            bad.append({"journal_id": e["id"], "problem": "ledger entries do not match the recorded amounts"})
    return bad
