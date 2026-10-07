"""Loans. A loan is money a member OWES the alliance. It is kept apart from deposits and is never a deposit.

Model (deliberately simple and explicit):
  * principal + a FLAT interest amount, both fixed when the loan is recorded (interest = principal x rate%).
    No hidden accrual: what is owed is always principal + interest - what has been paid or written off.
  * a real PnW #loan payment is applied INTEREST first, then PRINCIPAL, oldest loan first. Anything more than is owed
    becomes the member's normal deposit (a real DEPOSIT ledger entry tied to that PnW record).
  * a deduction pays a loan out of the member's available cash (LOAN_DEDUCTION ledger entry).
  * a loan is OVERDUE once its due date has passed with something still owed. Overdue loans raise an ECON alert;
    nothing is ever taken from a member automatically.
Recording a loan does NOT move money: send the money with the normal withdrawal tools, then record the debt here.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal

from . import ledger as L
from . import money as M
from .util import now_iso, utcnow


class LoanError(L.LedgerError):
    pass


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def due(loan) -> tuple[int, int]:
    """(interest still owed, principal still owed) in cents."""
    return (loan["interest_cents"] - loan["interest_paid_cents"],
            loan["principal_cents"] - loan["principal_paid_cents"])


def owed(loan) -> int:
    i, p = due(loan)
    return i + p


def active(conn, nation_id: int) -> list:
    return conn.execute("SELECT * FROM loans WHERE nation_id=? AND status='ACTIVE' ORDER BY issued_at, id",
                        (nation_id,)).fetchall()


def total_owed(conn, nation_id: int) -> int:
    return sum(owed(l) for l in active(conn, nation_id))


def _event(conn, loan_id, nation_id, kind, *, interest=0, principal=0, excess=0, record_id=None, actor, note=None) -> int:
    cur = conn.execute(
        "INSERT INTO loan_events(loan_id,nation_id,ts,kind,interest_cents,principal_cents,excess_cents,pnw_record_id,actor,note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (loan_id, nation_id, now_iso(), kind, interest, principal, excess, record_id, str(actor), note))
    return cur.lastrowid


def _settle(conn, loan, interest: int, principal: int, *, written_off: int = 0) -> None:
    """Move a loan forward by the given amounts and close it when nothing is owed."""
    i_paid, p_paid = loan["interest_paid_cents"] + interest, loan["principal_paid_cents"] + principal
    left = (loan["interest_cents"] - i_paid) + (loan["principal_cents"] - p_paid)
    status, closed = "ACTIVE", None
    if left == 0:
        status, closed = ("WRITTEN_OFF" if (loan["written_off_cents"] + written_off) > 0 else "PAID"), now_iso()
    conn.execute("UPDATE loans SET interest_paid_cents=?, principal_paid_cents=?, written_off_cents=?, status=?, closed_at=? WHERE id=?",
                 (i_paid, p_paid, loan["written_off_cents"] + written_off, status, closed, loan["id"]))


def _allocate(loans: list, cents: int) -> tuple[list, int]:
    """Interest first, then principal, oldest loan first. Returns ([(loan, interest, principal)], left_over)."""
    plan = []
    for loan in loans:
        if cents <= 0:
            break
        i_due, p_due = due(loan)
        i = min(cents, i_due)
        p = min(cents - i, p_due)
        if i or p:
            plan.append((loan, i, p))
            cents -= i + p
    return plan, cents


# ------------------------------------------------------------------ recording
def record_loan(conn, *, nation_id: int, principal_cents: int, interest_percent: Decimal | float | str, due_days: int | None,
                note: str, actor: str) -> dict:
    if principal_cents <= 0:
        raise LoanError("The loan amount must be more than zero.")
    pct = Decimal(str(interest_percent))
    if pct < 0 or pct > 1000:
        raise LoanError("The interest rate must be between 0% and 1000%.")
    L.assert_can_mutate(conn, nation_id, "adjust")
    interest = int((Decimal(principal_cents) * pct / 100).to_integral_value(rounding=ROUND_HALF_UP))
    issued = now_iso()
    due_at = (utcnow() + dt.timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%SZ") if due_days else None
    L.ensure_member(conn, nation_id)
    cur = conn.execute(
        "INSERT INTO loans(nation_id,principal_cents,interest_cents,status,source,issued_at,due_at,note,created_by) "
        "VALUES(?,?,?,'ACTIVE','RECORDED',?,?,?,?)", (nation_id, principal_cents, interest, issued, due_at, note, str(actor)))
    lid = cur.lastrowid
    _event(conn, lid, nation_id, "ISSUED", interest=interest, principal=principal_cents, actor=actor, note=note)
    L.audit(conn, actor, "LOAN_RECORDED", f"loan:{lid}", {"nation": nation_id, "principal_cents": principal_cents,
                                                         "interest_cents": interest, "due_at": due_at, "note": note})
    return {"loan_id": lid, "interest_cents": interest, "due_at": due_at}


def adopt_imported(conn, *, actor: str, due_days: int | None = None) -> dict:
    """Turn the outstanding loans carried in by a restore spreadsheet into real loans (no interest added)."""
    L.assert_can_mutate(conn, None, "import")
    pending = conn.execute("SELECT * FROM imported_loans WHERE status='PENDING_LOAN_MODULE' ORDER BY id").fetchall()
    due_at = (utcnow() + dt.timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%SZ") if due_days else None
    made, total = 0, 0
    for r in pending:
        L.ensure_member(conn, r["nation_id"])
        cur = conn.execute(
            "INSERT INTO loans(nation_id,principal_cents,interest_cents,status,source,imported_loan_id,issued_at,due_at,note,created_by)"
            " VALUES(?,?,0,'ACTIVE','IMPORTED',?,?,?,?,?)",
            (r["nation_id"], r["outstanding_cents"], r["id"], r["created_at"], due_at,
             f"Outstanding loan carried in by import batch #{r['batch_id']}", str(actor)))
        _event(conn, cur.lastrowid, r["nation_id"], "IMPORTED", principal=r["outstanding_cents"], actor=actor,
               note=f"import batch #{r['batch_id']}")
        conn.execute("UPDATE imported_loans SET status='APPLIED', applied_at=? WHERE id=?", (now_iso(), r["id"]))
        made += 1
        total += r["outstanding_cents"]
    L.audit(conn, actor, "LOANS_ADOPTED", None, {"loans": made, "total_cents": total, "due_at": due_at})
    return {"loans": made, "total_cents": total}


# ------------------------------------------------------------------ repayment from a real PnW record
def apply_repayment(conn, *, record_id: int, actor: str) -> dict:
    """Apply a stored PnW LOAN_REPAYMENT record to the sender's loans. Safe to call once per record."""
    rec = conn.execute("SELECT * FROM pnw_records WHERE id=?", (record_id,)).fetchone()
    if not rec or rec["classification"] != "LOAN_REPAYMENT" or rec["direction"] != "IN":
        raise LoanError(f"PnW record #{record_id} is not a recorded #loan repayment.")
    nation = rec["credited_nation_id"] or rec["sender_id"]
    if conn.execute("SELECT 1 FROM loan_events WHERE pnw_record_id=? AND kind='REPAYMENT'", (record_id,)).fetchone():
        raise LoanError(f"Record #{record_id} was already applied to a loan.")
    if L.get_state(conn, "emergency_lock") == "1":
        raise LoanError("EMERGENCY LOCK is active: the repayment is not applied yet.")
    amounts = json.loads(rec["amounts_json"])
    cash = amounts.get("money", 0)
    other = {k: v for k, v in amounts.items() if k != "money"}
    if cash <= 0:
        raise LoanError("This record has no money in it, so nothing can be applied to a loan.")
    loans = active(conn, nation)
    if not loans:
        return {"applied": False, "reason": "NO_ACTIVE_LOAN", "nation_id": nation, "cash": cash, "other": other}
    plan, excess = _allocate(loans, cash)
    last = len(plan) - 1
    events = []
    for n, (loan, i, p) in enumerate(plan):
        ev = _event(conn, loan["id"], nation, "REPAYMENT", interest=i, principal=p, excess=excess if n == last else 0,
                    record_id=record_id, actor=actor, note=f"PnW record #{record_id}")
        _settle(conn, loan, i, p)
        events.append((loan["id"], i, p))
    credited = 0
    if excess > 0:
        L.ensure_member(conn, nation)
        L.credit_deposit(conn, pnw_record_id=record_id, nation_id=nation, amounts={"money": excess}, actor=actor,
                         note=f"Loan repayment #{record_id}: amount above what was owed")
        credited = excess
    L.audit(conn, actor, "LOAN_REPAYMENT_APPLIED", f"pnw:{record_id}",
            {"nation": nation, "cash": cash, "applied": events, "excess_to_deposit": credited})
    return {"applied": True, "nation_id": nation, "cash": cash, "events": events, "excess": credited, "other": other,
            "still_owed": total_owed(conn, nation)}


# ------------------------------------------------------------------ deduction from the member's deposit
def deduct(conn, *, nation_id: int, amount_cents: int, actor: str, note: str) -> dict:
    """Pay loans out of the member's AVAILABLE cash. Cannot take more than is spendable or more than is owed."""
    if amount_cents <= 0:
        raise LoanError("The amount must be more than zero.")
    L.assert_can_mutate(conn, nation_id, "adjust")
    loans = active(conn, nation_id)
    if not loans:
        raise LoanError("This member has no active loan.")
    if amount_cents > sum(owed(l) for l in loans):
        raise LoanError(f"That is more than is owed ({dollars(sum(owed(l) for l in loans))}).")
    free = L.spendable(conn, nation_id).get("money", 0)
    if free < amount_cents:
        raise L.InsufficientFunds(f"Only {dollars(max(free, 0))} of available cash can be used (locked funds and "
                                  "pending withdrawals are not touched).")
    plan, left = _allocate(loans, amount_cents)
    before = L.snapshot_accounts(conn, nation_id)
    g = L.new_group("LOAN")
    events = []
    for loan, i, p in plan:
        ev = _event(conn, loan["id"], nation_id, "DEDUCTION", interest=i, principal=p, actor=actor, note=note)
        _settle(conn, loan, i, p)
        L.post_entries(conn, [dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource="money", delta=-(i + p),
                                   entry_type="LOAN_DEDUCTION", loan_event_id=ev, actor=actor,
                                   note=f"Loan #{loan['id']} deduction: {note}")])
        events.append((loan["id"], i, p))
    L.audit(conn, actor, "LOAN_DEDUCTION", f"nation:{nation_id}", {"amount_cents": amount_cents, "applied": events, "note": note})
    return {"events": events, "before": before, "after": L.snapshot_accounts(conn, nation_id),
            "still_owed": total_owed(conn, nation_id)}


def write_off(conn, *, loan_id: int, reason: str, actor: str) -> dict:
    loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan:
        raise LoanError(f"There is no loan #{loan_id}.")
    if loan["status"] != "ACTIVE":
        raise LoanError(f"Loan #{loan_id} is already {loan['status'].lower().replace('_', ' ')}.")
    if len((reason or "").strip()) < 5:
        raise LoanError("A reason is required to write a loan off.")
    i, p = due(loan)
    _event(conn, loan_id, loan["nation_id"], "WRITE_OFF", interest=i, principal=p, actor=actor, note=reason)
    _settle(conn, loan, i, p, written_off=i + p)
    L.audit(conn, actor, "LOAN_WRITTEN_OFF", f"loan:{loan_id}", {"nation": loan["nation_id"], "cents": i + p, "reason": reason})
    return {"written_off_cents": i + p, "nation_id": loan["nation_id"]}


# ------------------------------------------------------------------ overdue
def is_overdue(loan, now: str | None = None) -> bool:
    return bool(loan["status"] == "ACTIVE" and loan["due_at"] and loan["due_at"] < (now or now_iso()) and owed(loan) > 0)


def newly_overdue(conn) -> list:
    """Overdue loans ECON has not been told about yet."""
    now = now_iso()
    return [l for l in conn.execute("SELECT * FROM loans WHERE status='ACTIVE' AND due_at IS NOT NULL AND due_at < ? "
                                    "AND overdue_alerted_at IS NULL", (now,)).fetchall() if owed(l) > 0]


def mark_overdue_alerted(conn, loan_ids: list) -> None:
    for lid in loan_ids:
        conn.execute("UPDATE loans SET overdue_alerted_at=? WHERE id=?", (now_iso(), lid))
