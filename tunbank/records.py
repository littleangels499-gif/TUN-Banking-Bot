"""Processing PnW bank records: classify, store as evidence, credit if (and only if) valid."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import bankrec as B
from . import intents as INT
from . import ledger as L
from . import money as M
from .config import cfg_bool, cfg_get, cfg_int
from .util import jdump, now_iso


@dataclass
class Ctx:
    alliance_id: int
    members: dict | None            # {nation_id: name} or None if unavailable
    snapshot_id: int | None = None
    valuation: object | None = None  # Valuation of this record (optional)
    baseline: bool = False
    bank_ids: set | None = None      # alliance ids of every bank we manage (main + offshore)


@dataclass
class Outcome:
    record_id: int | None
    kind: str          # CREDIT, TAX, DONATION, LOAN, REVIEW, OUTGOING_LINKED, EXTERNAL_OUTFLOW,
                       # BASELINE, DUPLICATE, DEFERRED, INVALID, ANOMALY
    classification: str = ""
    nation_id: int | None = None
    amounts: dict = field(default_factory=dict)
    note: str = ""
    before: dict | None = None
    after: dict | None = None
    warnings: list = field(default_factory=list)
    tx_id: int | None = None
    new_events: list = field(default_factory=list)
    intent: int | None = None
    member_deposit: int | None = None


def _banks(ctx: "Ctx") -> set:
    return ctx.bank_ids or {ctx.alliance_id}


def _direction(n: dict, ctx: "Ctx") -> str:
    ids = _banks(ctx)
    is_in = n["receiver_type"] == 2 and n["receiver_id"] in ids
    is_out = n["sender_type"] == 2 and n["sender_id"] in ids
    if is_in and not is_out:
        return "IN"
    if is_out and not is_in:
        return "OUT"
    return "OTHER"


def classify_inbound(conn, n: dict, ctx: Ctx) -> tuple[str, str, int | None, str]:
    """-> (classification, status, credited_nation_id, reason). Never credits by itself.

    THE RULE for money arriving at the main bank from a member nation:
        #loan      -> loan repayment            (explicit exception)
        #ignore    -> alliance-owned donation   (explicit exception)
        anything else (no note, #deposit, "my savings", "warchest money", ...) -> NORMAL MEMBER DEPOSIT
    Only a record that PnW itself identifies as a system/tax record (a PnW tax id), or a tag an Admin has listed in
    `system_tags`, is not a member deposit. A different note never leaves a member's deposit unclassified.
    """
    tags = B.note_tags(n["note"])
    ignore_tag = cfg_get(conn, "tag_ignore").lower()
    loan_tag = cfg_get(conn, "tag_loan").lower()
    system_tags = {t.strip().lower() for t in cfg_get(conn, "system_tags").replace(";", ",").split(",") if t.strip()}
    if n["sender_type"] != 1:
        return "REVIEW", "AWAITING_REVIEW", None, "sender is not a nation"
    if n["tax_id"]:                                   # positively identified by PnW as a tax collection
        return "TAX", "NO_CREDIT", n["sender_id"], "PnW tax collection"
    if tags & system_tags:                            # explicitly configured system type
        return "OTHER", "NO_CREDIT", n["sender_id"], "configured system tag: " + ", ".join(sorted(tags & system_tags))
    is_ignore, is_loan = ignore_tag in tags, loan_tag in tags
    if is_ignore and is_loan:
        return "REVIEW", "AWAITING_REVIEW", None, f"note has both {ignore_tag} and {loan_tag}"
    if is_ignore:
        return "ALLIANCE_DONATION", "NO_CREDIT", n["sender_id"], f"{ignore_tag} donation"
    if is_loan:
        return "LOAN_REPAYMENT", "NO_CREDIT", n["sender_id"], "loan repayment"
    # everything else from a member is that member's normal deposit
    if cfg_bool(conn, "require_alliance_member_deposit"):
        if ctx.members is None:
            return "REVIEW", "AWAITING_REVIEW", None, "alliance member list unavailable"
        if n["sender_id"] not in ctx.members:
            return "REVIEW", "AWAITING_REVIEW", None, "sender is not a current alliance member"
    if not n["amounts"]:
        return "REVIEW", "AWAITING_REVIEW", None, "record contains no money or resources"
    return "MEMBER_DEPOSIT", "CREDITED", n["sender_id"], "normal member deposit"


def process_record(conn, rec: dict, ctx: Ctx) -> Outcome:
    """Handle ONE raw PnW bank record inside the caller's transaction."""
    try:
        n = B.normalize(rec)
    except (ValueError, M.AmountError) as exc:
        eid, new = L.raise_event(conn, "WARNING", "UNREADABLE_PNW_RECORD", ref_type="pnw_record",
                                 ref_id=rec.get("id"), details={"error": str(exc)},
                                 dedupe_key=f"badrec:{rec.get('id')}")
        return Outcome(None, "INVALID", note=str(exc), new_events=[eid] if new else [])

    existing = B.get_record(conn, n["id"])
    if existing is not None:
        same = (existing["amounts_json"] == jdump(n["amounts"])
                and existing["sender_id"] == n["sender_id"]
                and existing["receiver_id"] == n["receiver_id"])
        if not same:
            eid, new = L.raise_event(
                conn, "RECON", "PNW_RECORD_CHANGED", ref_type="pnw_record", ref_id=n["id"],
                details={"stored": existing["amounts_json"], "now": n["amounts"]},
                dedupe_key=f"recchanged:{n['id']}")
            return Outcome(n["id"], "ANOMALY", note="PnW record differs from what was stored",
                           new_events=[eid] if new else [])
        return Outcome(n["id"], "DUPLICATE", classification=existing["classification"])

    ids = _banks(ctx)
    if (n["sender_type"] == 2 and n["sender_id"] in ids and n["receiver_type"] == 2 and n["receiver_id"] in ids
            and n["sender_id"] != n["receiver_id"]):
        return _process_offshore_transfer(conn, n, ctx)
    direction = _direction(n, ctx)
    if direction == "OTHER":
        B.insert_record(conn, n, direction="OTHER", classification="OTHER", status="NO_CREDIT",
                        snapshot_id=ctx.snapshot_id)
        return Outcome(n["id"], "DEFERRED", "OTHER", note="not a deposit or withdrawal for this bank")

    if direction == "OUT":
        return _process_outbound(conn, n, ctx)

    if n["receiver_id"] != ctx.alliance_id:      # paid straight into the offshore bank, not the main collection bank
        cls, status, nation, reason = ("REVIEW", "AWAITING_REVIEW", None, "deposit made directly to the offshore bank")
    else:
        cls, status, nation, reason = classify_inbound(conn, n, ctx)
    out = Outcome(n["id"], "REVIEW", cls, nation, dict(n["amounts"]), reason)

    if ctx.baseline:
        # First-ever scan: history is stored as evidence but NEVER credited, because
        # opening balances already include it.
        B.insert_record(conn, n, direction="IN", classification=cls, status="BASELINE",
                        credited_nation_id=None, snapshot_id=ctx.snapshot_id)
        if cls == "TAX":
            _store_tax(conn, n, ctx)
        out.kind = "BASELINE"
        return out

    if cls == "MEMBER_DEPOSIT":
        if L.get_state(conn, "emergency_lock") == "1":
            B.insert_record(conn, n, direction="IN", classification=cls, status="PENDING_CREDIT",
                            credited_nation_id=nation, snapshot_id=ctx.snapshot_id)
            out.kind = "DEFERRED"
            out.note = "EMERGENCY LOCK active: stored as evidence, credit postponed"
            return out
        B.insert_record(conn, n, direction="IN", classification=cls, status="CREDITED",
                        credited_nation_id=nation, snapshot_id=ctx.snapshot_id)
        name = (ctx.members or {}).get(nation)
        L.ensure_member(conn, nation, name)
        res = L.credit_deposit(conn, pnw_record_id=n["id"], nation_id=nation, amounts=n["amounts"],
                               actor="system:scanner", snapshot_id=ctx.snapshot_id, note=n["note"])
        out.kind, out.before, out.after, out.nation_id = "CREDIT", res["before"], res["after"], nation
        out.intent = INT.match(conn, nation, n["amounts"], n["id"])
        _match_member_deposit(conn, n, nation, out)
        _flag_large(conn, n, ctx, out)
        return out

    B.insert_record(conn, n, direction="IN", classification=cls, status=status,
                    credited_nation_id=None, snapshot_id=ctx.snapshot_id)
    if cls == "TAX":
        _store_tax(conn, n, ctx)
        out.kind = "TAX"
    elif cls == "ALLIANCE_DONATION":
        out.kind = "DONATION"
    elif cls == "LOAN_REPAYMENT":
        out.kind = "LOAN"
    else:
        out.kind = "REVIEW"
    return out


def _match_member_deposit(conn, n: dict, nation: int, out: "Outcome") -> None:
    """A deposit the member started from Discord carries TUN-DEP<id>. When its REAL record is credited, link them."""
    from .memberdeposit import dep_tag

    tid = dep_tag(n["note"])
    if not tid:
        return
    row = conn.execute("SELECT * FROM member_deposits WHERE id=?", (tid,)).fetchone()
    if row and row["nation_id"] == nation and row["status"] in ("PENDING", "SENT", "UNCERTAIN"):
        conn.execute("UPDATE member_deposits SET status='CREDITED', pnw_record_id=?, credited_at=?, updated_at=?, failure_reason=NULL WHERE id=?",
                     (n["id"], now_iso(), now_iso(), tid))
        conn.execute("UPDATE integrity_events SET status='RESOLVED', resolved_by='system', resolved_at=?, resolution_note=? "
                     "WHERE status='OPEN' AND dedupe_key=?", (now_iso(), f"Credited from PnW record #{n['id']}", f"member-deposit-uncertain:{tid}"))
        out.member_deposit = tid


def _process_offshore_transfer(conn, n: dict, ctx: Ctx) -> Outcome:
    """Money moved between OUR OWN banks (main <-> offshore). It changes where funds sit physically and
    nothing else: no member balance is touched."""
    from . import offshore as OFF

    direction = "TO_OFFSHORE" if n["sender_id"] == ctx.alliance_id else "TO_MAIN"
    out = Outcome(n["id"], "OFFSHORE", "OTHER", None, dict(n["amounts"]))
    B.insert_record(conn, n, direction="OTHER", classification="OTHER",
                    status="BASELINE" if ctx.baseline else "NO_CREDIT", snapshot_id=ctx.snapshot_id)
    if ctx.baseline:
        out.kind = "BASELINE"
        return out
    tag = OFF.off_tag(n["note"])
    row = conn.execute("SELECT * FROM offshore_transfers WHERE id=?", (tag,)).fetchone() if tag else None
    now = now_iso()
    if (row and row["status"] in ("PLANNED", "PENDING", "UNCERTAIN") and row["direction"] == direction
            and json.loads(row["amounts_json"]) == n["amounts"]):
        conn.execute("UPDATE offshore_transfers SET status='COMPLETED', pnw_record_id=?, completed_at=?, updated_at=?, "
                     "failure_reason=NULL WHERE id=?", (n["id"], now, now, row["id"]))
        conn.execute("UPDATE integrity_events SET status='RESOLVED', resolved_by='system', resolved_at=?, "
                     "resolution_note=? WHERE status='OPEN' AND dedupe_key=?",
                     (now, f"Confirmed by PnW record #{n['id']}", f"offshore-uncertain:{row['id']}"))
        L.audit(conn, "system", "OFFSHORE_CONFIRMED", f"offshore:{row['id']}", {"pnw_record_id": n["id"]})
        out.note = f"Offshore transfer #{row['id']} confirmed by the PnW record"
        return out
    # not planned by the bot (done in-game) or it doesn't match a plan: record it honestly
    cents = ctx.valuation.total_cents if ctx.valuation is not None else None
    cur = conn.execute(
        "INSERT INTO offshore_transfers(created_at,updated_at,direction,mode,status,actor,reason,amounts_json,"
        "value_cents,price_snapshot_id,pnw_record_id,completed_at) VALUES(?,?,?,'OBSERVED','COMPLETED','system',?,?,?,?,?,?)",
        (now, now, direction, "seen in PnW; not started from the bot", jdump(n["amounts"]), cents, ctx.snapshot_id, n["id"], now))
    L.audit(conn, "system", "OFFSHORE_OBSERVED", f"offshore:{cur.lastrowid}", {"pnw_record_id": n["id"], "direction": direction})
    out.note = "Funds moved between the main and offshore banks in PnW (not started from the bot)"
    if row:
        eid, new = L.raise_event(conn, "WARNING", "OFFSHORE_MISMATCH", ref_type="pnw_record", ref_id=n["id"],
                                 details={"message": f"PnW record #{n['id']} carries the tag of offshore transfer #{row['id']} "
                                          "but its amounts or direction differ.", "planned": row["amounts_json"], "seen": n["amounts"]},
                                 dedupe_key=f"offshore-mismatch:{n['id']}")
        if new:
            out.new_events.append(eid)
    return out


def turn_key(record_date: str) -> str:
    """PnW turns are 2 hours long and start on even UTC hours. 2026-10-02 14:00:03 -> '2026-10-02 14'."""
    import datetime as dt

    text = (record_date or "").strip().replace("T", " ").replace("Z", "")[:19]
    try:
        d = dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = dt.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            d = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return f"{d:%Y-%m-%d} {d.hour - d.hour % 2:02d}"


def _turn_add(conn, n: dict, ctx: Ctx):
    key = turn_key(n["record_date"])
    row = conn.execute("SELECT * FROM tax_turns WHERE turn_key=?", (key,)).fetchone()
    totals = M.add(json.loads(row["totals_json"]) if row else {}, n["amounts"])
    now = now_iso()
    if row:
        conn.execute("UPDATE tax_turns SET records=records+1, totals_json=?, last_seen_at=? WHERE turn_key=?", (jdump(totals), now, key))
    else:
        conn.execute("INSERT INTO tax_turns(turn_key,started_at,records,totals_json,last_seen_at,alerted_at) VALUES(?,?,?,?,?,?)",
                     (key, key + ":00", 1, jdump(totals), now, "baseline" if ctx.baseline else None))


def backfill_tax_turns(conn) -> int:
    """One-time, after the upgrade that added per-turn summaries: group the tax records already stored into turns and mark
    them as already announced, so the next alert is the true total of the next turn and old history is never re-announced."""
    if conn.execute("SELECT COUNT(*) FROM tax_turns").fetchone()[0] or not conn.execute("SELECT 1 FROM tax_records LIMIT 1").fetchone():
        return 0
    turns: dict = {}
    for r in conn.execute("SELECT record_date, recorded_at, amounts_json FROM tax_records ORDER BY id"):
        key = turn_key(r["record_date"] or r["recorded_at"])
        t = turns.setdefault(key, {"n": 0, "totals": {}})
        t["n"] += 1
        t["totals"] = M.add(t["totals"], json.loads(r["amounts_json"]))
    for key, t in turns.items():
        conn.execute("INSERT INTO tax_turns(turn_key,started_at,records,totals_json,last_seen_at,alerted_at) VALUES(?,?,?,?,?,'backfill')",
                     (key, key + ":00", t["n"], jdump(t["totals"]), now_iso()))
    return len(turns)


def _store_tax(conn, n: dict, ctx: Ctx):
    _turn_add(conn, n, ctx)
    conn.execute(
        "INSERT OR IGNORE INTO tax_records(pnw_record_id,nation_id,tax_id,record_date,amounts_json,"
        "price_snapshot_id,recorded_at) VALUES(?,?,?,?,?,?,?)",
        (n["id"], n["sender_id"], n["tax_id"], n["record_date"], jdump(n["amounts"]),
         ctx.snapshot_id, now_iso()))


def _flag_large(conn, n: dict, ctx: Ctx, out: Outcome):
    limit = cfg_int(conn, "large_credit_value") * 100
    v = ctx.valuation
    if v is None or v.total_cents is None:
        out.warnings.append("VALUATION UNAVAILABLE at the time of this deposit")
        return
    if limit and v.total_cents >= limit:
        out.warnings.append("LARGE DEPOSIT - verify it in-game")
        eid, new = L.raise_event(conn, "WARNING", "LARGE_CREDIT", nation_id=n["sender_id"],
                                 ref_type="pnw_record", ref_id=n["id"],
                                 details={"value_cents": v.total_cents},
                                 dedupe_key=f"largecredit:{n['id']}")
        if new:
            out.new_events.append(eid)


def _process_outbound(conn, n: dict, ctx: Ctx) -> Outcome:
    """A record where the ALLIANCE bank sent something out."""
    tx_id = B.tx_tag(n["note"])
    tx, items = L.get_tx(conn, tx_id) if tx_id else (None, {})
    out = Outcome(n["id"], "EXTERNAL_OUTFLOW", "EXTERNAL_OUTFLOW", n["receiver_id"], dict(n["amounts"]))

    def external(reason: str, severity: str, kind: str) -> Outcome:
        B.insert_record(conn, n, direction="OUT", classification="EXTERNAL_OUTFLOW",
                        status="AWAITING_REVIEW", snapshot_id=ctx.snapshot_id)
        eid, new = L.raise_event(conn, severity, kind, ref_type="pnw_record", ref_id=n["id"],
                                 details={"reason": reason, "amounts": n["amounts"],
                                          "receiver": n["receiver_id"], "note": n["note"]},
                                 dedupe_key=f"{kind}:{n['id']}")
        out.note = reason
        if new:
            out.new_events.append(eid)
        return out

    if ctx.baseline:
        B.insert_record(conn, n, direction="OUT", classification="EXTERNAL_OUTFLOW",
                        status="BASELINE", snapshot_id=ctx.snapshot_id)
        out.kind = "BASELINE"
        return out

    if tx is None:
        return external("Money left the alliance bank but not through TUN Bank "
                        "(in-game withdrawal or another bot).", "WARNING", "EXTERNAL_OUTFLOW")

    matches = (n["receiver_id"] == tx["dest_nation_id"] and n["receiver_type"] == 1
               and n["amounts"] == items)
    if tx["status"] == "COMPLETED":
        # PnW shows a SECOND record carrying the tag of a finished transfer.
        return external(f"Second PnW record for already-completed transaction #{tx_id}: "
                        "possible DUPLICATE TRANSFER.", "CRITICAL", "DUPLICATE_WITHDRAWAL")
    if tx["status"] in ("FAILED", "CANCELLED"):
        return external(f"PnW shows money sent for transaction #{tx_id}, which the bot recorded "
                        "as failed/cancelled.", "CRITICAL", "FAILED_TX_HAS_RECORD")
    if tx["status"] not in L.IN_FLIGHT:
        return external(f"Record references transaction #{tx_id} in state {tx['status']}.",
                        "RECON", "TX_STATE_MISMATCH")
    if not matches:
        o = external(f"PnW record differs from transaction #{tx_id} (destination or amounts).",
                     "CRITICAL", "TX_MISMATCH")
        L.mark_uncertain(conn, tx_id, "PnW record does not match the requested transfer")
        return o

    # Perfect match: this is proof the transfer really happened.
    B.insert_record(conn, n, direction="OUT", classification="OUTGOING_TX", status="LINKED_TX",
                    tx_id=tx_id, snapshot_id=ctx.snapshot_id)
    L.attach_pnw_record(conn, tx_id, n["id"])
    L.complete_withdrawal(conn, tx_id)
    out.kind, out.classification, out.tx_id = "OUTGOING_LINKED", "OUTGOING_TX", tx_id
    out.note = f"Confirmed transaction #{tx_id} from the PnW bank record"
    return out


def credit_pending(conn, ctx_members: dict | None) -> list:
    """After an emergency lock is lifted: credit deposits that were held back."""
    if L.get_state(conn, "emergency_lock") == "1":
        return []
    outs = []
    rows = conn.execute("SELECT * FROM pnw_records WHERE status='PENDING_CREDIT' ORDER BY id").fetchall()
    for r in rows:
        amounts = {k: v for k, v in json.loads(r["amounts_json"]).items()}
        conn.execute("UPDATE pnw_records SET status='CREDITED' WHERE id=?", (r["id"],))
        res = L.credit_deposit(conn, pnw_record_id=r["id"], nation_id=r["credited_nation_id"],
                               amounts=amounts, actor="system:scanner", note=r["note"])
        outs.append(Outcome(r["id"], "CREDIT", "MEMBER_DEPOSIT", r["credited_nation_id"], amounts,
                            "credited after emergency lock lifted", res["before"], res["after"]))
    return outs


def resolve_review(conn, *, record_id: int, action: str, actor: str, note: str,
                   nation_id: int | None = None, snapshot_id=None) -> Outcome:
    """ECON decision on a record the bot could not classify on its own.

    'credit'    - it IS a member deposit for nation_id (still tied to the real PnW record)
    'alliance'  - it is alliance money; nobody is credited
    'dismiss'   - acknowledge (used for external outflows)
    """
    if not note or not note.strip():
        raise L.LedgerError("A reason is required.")
    rec = B.get_record(conn, record_id)
    if rec is None:
        raise L.LedgerError(f"PnW record {record_id} is not in the database.")
    if rec["status"] not in ("AWAITING_REVIEW", "BASELINE", "PENDING_CREDIT"):
        raise L.LedgerError(f"Record {record_id} is {rec['status']}; it cannot be reviewed.")
    amounts = json.loads(rec["amounts_json"])
    out = Outcome(record_id, "REVIEW", rec["classification"], nation_id, amounts)
    if action == "credit":
        if rec["direction"] != "IN":
            raise L.LedgerError("Only incoming records can be credited.")
        if nation_id is None:
            raise L.LedgerError("Choose which nation gets the credit.")
        conn.execute("UPDATE pnw_records SET classification='MEMBER_DEPOSIT', status='CREDITED', "
                     "credited_nation_id=? WHERE id=?", (nation_id, record_id))
        L.ensure_member(conn, nation_id)
        res = L.credit_deposit(conn, pnw_record_id=record_id, nation_id=nation_id, amounts=amounts,
                               actor=actor, snapshot_id=snapshot_id, note=f"review: {note}")
        out.kind, out.before, out.after = "CREDIT", res["before"], res["after"]
    elif action == "alliance":
        conn.execute("UPDATE pnw_records SET classification='OTHER', status='NO_CREDIT' WHERE id=?",
                     (record_id,))
        out.kind = "DONATION"
    elif action == "dismiss":
        conn.execute("UPDATE pnw_records SET status='DISMISSED' WHERE id=?", (record_id,))
        out.kind = "REVIEW"
    else:
        raise L.LedgerError("Unknown action.")
    conn.execute(
        "UPDATE integrity_events SET status='RESOLVED', resolved_by=?, resolved_at=?, "
        "resolution_note=? WHERE status='OPEN' AND ref_type='pnw_record' AND ref_id=?",
        (str(actor), now_iso(), f"Reviewed ({action}): {note}", str(record_id)))
    from . import configaudit as CA
    CA.record(conn, actor=actor, setting="record_classification", previous=f"{rec['classification']} / {rec['status']}",
              new={"credit": "member deposit (credited)", "alliance": "alliance money (no credit)", "dismiss": "dismissed"}[action],
              target=f"PnW record #{record_id}" + (f" → nation [#{nation_id}]" if nation_id else "") + f" · {note}",
              category="CLASSIFICATION", only_if_changed=False)
    L.audit(conn, actor, "RECORD_REVIEWED", f"pnw:{record_id}",
            {"action": action, "nation_id": nation_id, "note": note})
    return out
