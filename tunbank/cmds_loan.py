"""/loan commands: record, view and settle loans. Loans are separate from deposits."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import fmt
from . import icons
from . import ledger as L
from . import loans as LN
from . import money as M
from . import resolve as RS
from .ui import Services, chunk_cards, confirm, nation_arg, need, paginate, reply, thinking

log = logging.getLogger("tunbank.loans")


def _amount(text: str) -> int:
    """'25m', '25,000,000', '$1.5k' -> cents. Raises LoanError on nonsense."""
    try:
        return M.parse_amounts(f"money={text.replace('$', '').strip()}")["money"]
    except (M.AmountError, KeyError) as exc:
        raise LN.LoanError(f"I couldn't read that amount: {exc}") from exc


def loan_line(l) -> str:
    flag = " ⚠️ OVERDUE" if LN.is_overdue(l) else ""
    due = f" · due {l['due_at'][:10]}" if l["due_at"] else " · no due date"
    return (f"**#{l['id']}** {LN.dollars(LN.owed(l))} owed of {LN.dollars(l['principal_cents'] + l['interest_cents'])}"
            f" ({l['status'].lower().replace('_', ' ')}){due}{flag}")


def loan_card(conn, nation_id: int, title: str, *, history: bool = True) -> A.Card:
    loans = conn.execute("SELECT * FROM loans WHERE nation_id=? ORDER BY id", (nation_id,)).fetchall()
    c = A.Card(f"{icons.status('bank')} {title}", color=A.ORANGE if any(LN.is_overdue(l) for l in loans) else A.BLUE)
    if not loans:
        c.description = "No loans on record."
        return c
    owed = sum(LN.owed(l) for l in loans if l["status"] == "ACTIVE")
    c.add("Total still owed", LN.dollars(owed), True)
    c.add("Loans", str(len(loans)), True)
    c.add("Loan details", "\n".join(loan_line(l) for l in loans[:10]))
    if history:
        ev = conn.execute("SELECT * FROM loan_events WHERE nation_id=? ORDER BY id DESC LIMIT 8", (nation_id,)).fetchall()
        c.add("Recent history", "\n".join(
            f"`{e['ts'][:10]}` loan #{e['loan_id']} {e['kind'].lower().replace('_', ' ')} "
            f"{LN.dollars(e['interest_cents'] + e['principal_cents'])}"
            + (f" (+{LN.dollars(e['excess_cents'])} to deposit)" if e["excess_cents"] else "") for e in ev))
    return c


def overdue_card(l) -> A.Card:
    c = A.Card("⚠️ Loan overdue", f"Loan #{l['id']} · nation #{l['nation_id']}", A.ORANGE, kind="LOAN_OVERDUE")
    c.add("Still owed", LN.dollars(LN.owed(l)), True)
    c.add("Was due", (l["due_at"] or "")[:10], True)
    c.add("What now", "Nothing is taken automatically. ECON can use `/loan deduct` (from their deposit) or contact the member.")
    return c


def register(loan: app_commands.Group, svc: Services):
    uid = lambda i: str(i.user.id)  # noqa: E731

    @loan.command(name="add", description="Minister: record a loan you gave a member (this does not send any money)")
    @app_commands.describe(nation="Member: id, name, link or @user", amount="Loan amount, e.g. 25m",
                           interest_percent="Flat interest, e.g. 5 for 5% (default 0)", due_days="Days until it is due (blank = no due date)",
                           note="What the loan is for")
    async def add(interaction: discord.Interaction, nation: str, amount: str, note: str,
                  interest_percent: float = 0.0, due_days: Optional[int] = None):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        try:
            cents = _amount(amount)
            c = A.Card("Record this loan?", "Recording a loan does NOT send money. Send it with the normal bank tools first.", A.ORANGE)
            with svc.db.read() as conn:
                c.add("Member", RS.label(conn, nid), True)
            c.add("Amount", LN.dollars(cents), True)
            c.add("Flat interest", f"{interest_percent:g}% = {LN.dollars(round(cents * interest_percent / 100))}", True)
            c.add("Due", f"in {due_days} days" if due_days else "no due date", True)
            c.add("Note", note[:300])
            if not await confirm(svc, interaction, c):
                return await reply(interaction, "Cancelled. Nothing was recorded.")

            def do():
                with svc.db.tx() as conn:
                    return LN.record_loan(conn, nation_id=nid, principal_cents=cents, interest_percent=interest_percent,
                                          due_days=due_days, note=note, actor=uid(interaction))
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not recorded: {exc}")
        await reply(interaction, f"Loan **#{res['loan_id']}** recorded: {LN.dollars(cents)} + {LN.dollars(res['interest_cents'])} interest.")

    @loan.command(name="adopt", description="Admin: turn the loans carried in by a restore spreadsheet into real loans")
    @app_commands.describe(due_days="Optional: give them all a due date this many days from now")
    async def adopt(interaction: discord.Interaction, due_days: Optional[int] = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            pend = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(outstanding_cents),0) t FROM imported_loans "
                                "WHERE status='PENDING_LOAN_MODULE'").fetchone()
        if not pend["n"]:
            return await reply(interaction, "There are no imported loans waiting.")
        c = A.Card("Create these loans?", "Each becomes an active loan with no interest added. Nothing in anyone's deposit changes.", A.ORANGE)
        c.add("Loans", str(pend["n"]), True)
        c.add("Total outstanding", LN.dollars(pend["t"]), True)
        c.add("Due date", f"in {due_days} days" if due_days else "none (set later with a write-off or repayment)", True)
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return LN.adopt_imported(conn, actor=uid(interaction), due_days=due_days)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"{res['loans']} loan(s) created, {LN.dollars(res['total_cents'])} in total.")

    @loan.command(name="list", description="Staff: every active loan, overdue first")
    async def list_(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            rows = conn.execute("SELECT * FROM loans WHERE status='ACTIVE'").fetchall()
            rows.sort(key=lambda l: (not LN.is_overdue(l), l["due_at"] or "9999", l["id"]))
            lines = [f"{RS.label(conn, l['nation_id'])} · {loan_line(l)}" for l in rows]
            total = sum(LN.owed(l) for l in rows)
        pages = chunk_cards(f"{icons.status('bank')} Active loans · {LN.dollars(total)} owed", lines, per_page=8,
                            empty="There are no active loans.")
        await paginate(interaction, pages)

    @loan.command(name="view", description="Staff: one member's loans and repayment history")
    @app_commands.describe(nation="Member: id, name, link or @user")
    async def view(interaction: discord.Interaction, nation: str):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        with svc.db.read() as conn:
            card = loan_card(conn, nid, f"Loans · {RS.label(conn, nid)}")
        await reply(interaction, card=card)

    @loan.command(name="mine", description="See your own loans")
    async def mine(interaction: discord.Interaction):
        await thinking(interaction)
        with svc.db.read() as conn:
            m = L.member_by_discord(conn, interaction.user.id)
            if not m:
                return await reply(interaction, "You have not linked your nation yet. Use `/nation link` first.")
            card = loan_card(conn, m["nation_id"], "Your loans")
        await reply(interaction, card=card)

    @loan.command(name="deduct", description="Minister: pay a member's loan out of their available cash")
    @app_commands.describe(nation="Member: id, name, link or @user", amount="How much, e.g. 5m", note="Why")
    async def deduct(interaction: discord.Interaction, nation: str, amount: str, note: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        try:
            cents = _amount(amount)
            with svc.db.read() as conn:
                owed, free = LN.total_owed(conn, nid), L.spendable(conn, nid).get("money", 0)
                label = RS.label(conn, nid)
            c = A.Card("Take this from their deposit?", "This reduces their available cash and the loan they owe.", A.ORANGE)
            c.add("Member", label, True)
            c.add("Amount", LN.dollars(cents), True)
            c.add("Loan owed now", LN.dollars(owed), True)
            c.add("Available cash", LN.dollars(max(free, 0)), True)
            c.add("Note", note[:300])
            if not await confirm(svc, interaction, c):
                return await reply(interaction, "Cancelled. Nothing was changed.")

            def do():
                with svc.db.tx() as conn:
                    return LN.deduct(conn, nation_id=nid, amount_cents=cents, actor=uid(interaction), note=note)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Nothing was deducted: {exc}")
        await reply(interaction, f"Deducted {LN.dollars(cents)}. Still owed: {LN.dollars(res['still_owed'])}.")

    @loan.command(name="writeoff", description="Admin: forgive what is left of one loan (permanent record)")
    @app_commands.describe(loan_id="Loan number", reason="Why it is being forgiven")
    async def writeoff(interaction: discord.Interaction, loan_id: int, reason: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            l = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not l:
            return await reply(interaction, f"There is no loan #{loan_id}.")
        c = A.Card("Write this loan off?", "The remaining amount is forgiven. This can't be undone.", A.RED)
        c.add("Loan", loan_line(l))
        c.add("Reason", reason[:300])
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return LN.write_off(conn, loan_id=loan_id, reason=reason, actor=uid(interaction))
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Loan #{loan_id} written off ({LN.dollars(res['written_off_cents'])} forgiven).")

    @loan.command(name="applypayment", description="Banker: apply a stored #loan payment (PnW record) that was not applied automatically")
    @app_commands.describe(record="The PnW bank record number")
    async def applypayment(interaction: discord.Interaction, record: int):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        try:
            def do():
                with svc.db.tx() as conn:
                    return LN.apply_repayment(conn, record_id=record, actor=uid(interaction))
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        if not res["applied"]:
            return await reply(interaction, "That member has no active loan, so nothing was applied. Nothing was credited either.")
        await reply(interaction, f"Applied {LN.dollars(res['cash'])}. Still owed: {LN.dollars(res['still_owed'])}."
                                 + (f" {LN.dollars(res['excess'])} above the loan was credited to their deposit." if res["excess"] else ""))
