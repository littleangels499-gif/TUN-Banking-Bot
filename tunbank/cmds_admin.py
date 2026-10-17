"""Staff commands part 2: corrections, approvals, limits, freezes, imports, exports, settings."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import config_reset as CRST
from . import config_view as CV
from . import identity
from . import configaudit as CA
from . import credentials as CR
from . import exports as X
from . import fmt
from . import icons
from . import importer
from . import ledger as L
from . import limits as LIM
from . import money as M
from . import perms
from . import records as REC
from . import resolve as RS
from .config import DEFAULTS, cfg_get, cfg_set
from .util import now_iso as B_now
from .pnw import PnWRejected, PnWUncertain
from .buttons import ActionView, ItemPager, open_form
from .ui import (Services, actor_label, chunk_cards, confirm, nation_arg, need, paginate, post_outcomes, reply,
                 thinking, xlsx_file)
from .util import jdump
from .valuation import value_amounts

log = logging.getLogger("tunbank.admin")


def _cents(text: str) -> int:
    """'500m' -> cents (dollars x 100). '0' clears a limit."""
    if text.strip() in ("0", "off", "none"):
        return 0
    return M.parse_one(text)


def register(bank: app_commands.Group, bankset: app_commands.Group, ledger_grp: app_commands.Group, svc: Services):
    uid = lambda i: str(i.user.id)  # noqa: E731

    # ------------------------------------------------------------ freeze
    @bank.command(name="freeze", description="ECON: freeze a member's account (blocks their self-withdrawals)")
    async def freeze(interaction: discord.Interaction, nation: str, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        def do():
            with svc.db.tx() as conn:
                if not L.get_member(conn, nation_id):
                    return False
                old = L.get_member(conn, nation_id)
                CA.record(conn, actor=uid(interaction), setting="account_frozen", previous="frozen" if old["frozen"] else "active", new="frozen",
                          target=f"nation [#{nation_id}] · {reason}", category="SECURITY")
                conn.execute("UPDATE members SET frozen=1, frozen_reason=? WHERE nation_id=?", (reason, nation_id))
                L.audit(conn, uid(interaction), "ACCOUNT_FROZEN", f"nation:{nation_id}", {"reason": reason})
                return True
        ok = await asyncio.to_thread(do)
        await reply(interaction, f"Account {identity.label(nation_id)} frozen." if ok else "No such account.")

    @bank.command(name="unfreeze", description="ECON: unfreeze a member's account")
    async def unfreeze(interaction: discord.Interaction, nation: str, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        def do():
            with svc.db.tx() as conn:
                old = L.get_member(conn, nation_id)
                CA.record(conn, actor=uid(interaction), setting="account_frozen", previous="frozen" if old and old["frozen"] else "active", new="active",
                          target=f"nation [#{nation_id}] · {reason}", category="SECURITY")
                conn.execute("UPDATE members SET frozen=0, frozen_reason=NULL WHERE nation_id=?", (nation_id,))
                L.audit(conn, uid(interaction), "ACCOUNT_UNFROZEN", f"nation:{nation_id}", {"reason": reason})
        await asyncio.to_thread(do)
        await reply(interaction, f"Account {identity.label(nation_id)} unfrozen.")

    # ------------------------------------------------- pause / resume withdrawals
    @bank.command(name="lock", description="ECON: PAUSE all withdrawals and conversions (deposits keep being recorded)")
    async def pause(interaction: discord.Interaction, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        def do():
            with svc.db.tx() as conn:
                CA.record(conn, actor=uid(interaction), setting="withdrawals_paused", previous="no" if L.get_state(conn, "bank_paused") != "1" else "yes",
                          new="yes", target=reason, category="SECURITY")
                L.set_state(conn, "bank_paused", "1")
                L.audit(conn, uid(interaction), "BANK_PAUSED", None, {"reason": reason})
        await asyncio.to_thread(do)
        await reply(interaction, "Withdrawals are paused.")
        await svc.alerts.econ(A.Card("Withdrawals PAUSED", f"{actor_label(interaction)}: {reason}", A.ORANGE))

    @bank.command(name="unlock", description="ECON: resume withdrawals")
    async def resume(interaction: discord.Interaction, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        def do():
            with svc.db.tx() as conn:
                CA.record(conn, actor=uid(interaction), setting="withdrawals_paused", previous="yes" if L.get_state(conn, "bank_paused") == "1" else "no",
                          new="no", target=reason, category="SECURITY")
                L.set_state(conn, "bank_paused", "0")
                L.audit(conn, uid(interaction), "BANK_RESUMED", None, {"reason": reason})
        await asyncio.to_thread(do)
        await reply(interaction, "Withdrawals resumed.")
        await svc.alerts.econ(A.Card("Withdrawals RESUMED", f"{actor_label(interaction)}: {reason}", A.GREEN))

    # ------------------------------------------------------------ adjust
    @bank.command(name="adjust", description="ECON: correct an overstated balance (to ADD funds use /bank credit - every credit must be funded)")
    @app_commands.describe(nation="Member: id, name, link or @user", reason="What went wrong", evidence="Ticket, PnW record id, link...",
                           add="(Not allowed any more: use /bank credit)", remove="Amounts to REMOVE (below zero = a debt, needs a 2nd approver)")
    async def adjust(interaction: discord.Interaction, nation: str, reason: str, evidence: str,
                     add: str = "", remove: str = ""):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        if add.strip():
            return await reply(interaction, "Adding money straight to a balance would create funds from nowhere, so `/bank adjust` can no longer do it.\n"
                                            "Use **`/bank credit`** instead: it takes the same amount from alliance funds, another member, or an alliance "
                                            "ownership account, so both sides are recorded and the books still balance.")
        if not remove.strip():
            return await reply(interaction, "Give the `remove` amounts. (To add funds use `/bank credit`.)")
        deltas = {}
        try:
            if add.strip():
                for r, u in M.parse_amounts(add).items():
                    deltas[r] = u
            if remove.strip():
                for r, u in M.parse_amounts(remove).items():
                    if r in deltas:
                        return await reply(interaction, f"{r} is in both add and remove.")
                    deltas[r] = -u
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        snap = await svc.prices.get()
        val = value_amounts({r: abs(d) for r, d in deltas.items()}, snap)
        payload = {"nation": nation_id, "deltas": deltas, "evidence": evidence}
        approval_id = None
        try:
            with svc.db.read() as conn:
                debt = L.adjustment_debt(conn, nation_id, deltas)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        if any(d > 0 for d in deltas.values()) or debt:
            def find():
                from .util import parse_iso, utcnow
                with svc.db.tx() as conn:
                    for ap in conn.execute("SELECT * FROM approval_requests WHERE kind='ADJUSTMENT' AND "
                                           "status='PENDING' AND requested_by=? ORDER BY id DESC", (uid(interaction),)):
                        if ap["payload_json"] == jdump(payload):
                            if parse_iso(ap["expires_at"]) < utcnow():
                                conn.execute("UPDATE approval_requests SET status='EXPIRED' WHERE id=?", (ap["id"],))
                                continue
                            return ap["id"], bool(ap["approved_by"])
                    return L.create_approval(conn, "ADJUSTMENT", payload, uid(interaction), reason), False
            approval_id, approved = await asyncio.to_thread(find)
            if not approved:
                c = A.Card("Adjustment needs approval", "Adding funds, or taking a balance below zero, needs a SECOND staff member.",
                           A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by {actor_label(interaction)}")
                c.add("Nation", identity.label(nation_id), True)
                c.add("Change", fmt.amount_lines({r: abs(d) for r, d in deltas.items()}))
                c.add("Reason", reason)
                c.add("Evidence", evidence)
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} posted. After a different Minister "
                                                "approves it, run this same command again.")
        card = A.Card("Confirm ADJUSTMENT", "This corrects an accounting record. It is permanent and audited.", A.ORANGE)
        card.add("Nation", identity.label(nation_id), True)
        card.add("Change", "\n".join(f"{M.LABELS[r]}: {'+' if d > 0 else ''}{M.fmt_units(r, d)}" for r, d in deltas.items())
                 + "\n" + fmt.value_line(val))
        if debt:
            card.add("⚠️ Takes a balance BELOW ZERO",
                     "\n".join(f"{M.LABELS[r]}: resulting balance −{M.fmt_units(r, -b)} (a debt to the alliance, not clamped)"
                               for r, b in debt.items()))
        card.add("Reason", reason)
        card.add("Evidence", evidence)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled.")
        try:
            def do():
                with svc.db.tx() as conn:
                    res = L.apply_adjustment(conn, nation_id=nation_id, deltas=deltas, reason=reason,
                                             evidence=evidence, actor=uid(interaction), approval_id=approval_id,
                                             snapshot_id=snap.id if snap else None)
                    if approval_id:
                        L.finish_approval(conn, approval_id, "EXECUTED", {"adjustment_id": res["adjustment_id"]})
                    return res
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        out = A.adjustment_card(nation_id, deltas, val, reason, evidence, res, actor_label(interaction), res["adjustment_id"])
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
        await svc.alerts.flush_notifications()                 # the member's DM (queued in the same transaction as the change)

    # ------------------------------------------------- /bank credit (funded: every credit has an equal, recorded debit)
    @bank.command(name="credit", description="ECON: credit a member's balance, funded from alliance funds, another member or an ownership account")
    @app_commands.describe(nation="Member to credit: id, name, link or @user", amounts="e.g. money=5m food=20k",
                           reason="Why (mandatory, audited)", source="Where the money comes from",
                           from_member="Source = a member: id, name, link or @user",
                           from_alliance="Source = an ownership account: alliance id",
                           reference="Optional ticket / link / PnW record",
                           override_reason="ADMIN only: justify crediting even though the source is short (creates a deficit)")
    @app_commands.choices(source=[app_commands.Choice(name="Alliance funds (default)", value="ALLIANCE"),
                                  app_commands.Choice(name="Another member's balance", value="MEMBER"),
                                  app_commands.Choice(name="An alliance ownership account", value="OWNERSHIP")])
    async def credit(interaction: discord.Interaction, nation: str, amounts: str, reason: str,
                     source: Optional[app_commands.Choice[str]] = None, from_member: str = "", from_alliance: str = "",
                     reference: str = "", override_reason: str = ""):
        if not await need(svc, interaction, "MINISTER"):
            return
        if override_reason.strip() and not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        from . import offshore_ledger as OL
        from . import transfers as T
        from .banks import live_holdings
        stype = source.value if source else "ALLIANCE"
        recipient = await nation_arg(svc, interaction, nation)
        if recipient is None:
            return
        try:
            amts = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        if not amts:
            return await reply(interaction, "Give the amounts to credit, e.g. `money=5m food=20k`.")
        if not reason.strip():
            return await reply(interaction, "A reason is mandatory.")
        sid = None
        if stype == "MEMBER":
            if not from_member.strip():
                return await reply(interaction, "Source is a member: fill in `from_member`.")
            sid = await nation_arg(svc, interaction, from_member)
            if sid is None:
                return
        elif stype == "OWNERSHIP":
            try:
                sid = int(from_alliance.strip())
            except ValueError:
                return await reply(interaction, "Source is an ownership account: give the alliance id in `from_alliance`.")
        elif from_member.strip() or from_alliance.strip():
            return await reply(interaction, "You gave a member/alliance but the source is alliance funds. Pick the matching source.")
        holdings = None
        if stype == "ALLIANCE":
            try:
                holdings, _ = await live_holdings(svc.pnw, svc.settings, svc.db)
            except (PnWRejected, PnWUncertain) as exc:
                log.warning("credit: could not read the real bank (%s)", exc)
        with svc.db.read() as conn:
            if not L.get_member(conn, recipient):
                return await reply(interaction, f"{identity.label(recipient)} has no TUN Bank account. Link the nation first; an account is never created silently.")
            if stype == "OWNERSHIP" and not OL.registered(conn, sid):
                return await reply(interaction, "That alliance is not registered for the shared offshore.")
            if stype == "MEMBER" and not L.get_member(conn, sid):
                return await reply(interaction, "The source member has no TUN Bank account.")
            avail = T.source_available(conn, stype, sid, holdings)
            short = T.shortfall(avail, amts)
            before_r = L.get_balances(conn, recipient, "AVAILABLE")
        override = bool(override_reason.strip())
        if short and not override:
            lines = "\n".join(f"{M.LABELS[r]}: short by {M.fmt_units(r, v)}" for r, v in short.items())
            more = ("The real bank could not be read, so alliance funds cannot be verified right now." if avail is None else lines)
            tail = ("" if stype == "OWNERSHIP" else "\nAn ADMIN may allow this deficit by repeating the command with `override_reason`.")
            return await reply(interaction, f"The source does not cover this credit, so nothing was done.\n{more}{tail}")
        snap = await svc.prices.get()
        val = value_amounts(amts, snap)
        payload = T.payload_for(recipient=recipient, amounts=amts, source_type=stype, source_id=sid, reason=reason,
                                reference=reference, override=override)
        def find():
            with svc.db.tx() as conn:
                return L.find_or_request_approval(conn, "ADJUSTMENT", uid(interaction), payload, reason)
        approval_id, approved_by = await asyncio.to_thread(find)
        src_text = {"ALLIANCE": "Alliance funds", "MEMBER": f"Member {identity.label(sid) if sid else ''}",
                    "OWNERSHIP": f"Ownership account of alliance #{sid}"}[stype]
        if not approved_by:
            c = A.Card("Credit needs approval", "A funded credit still needs a SECOND staff member.", A.ORANGE, kind="APPROVAL")
            c.add("Request", f"#{approval_id} by {actor_label(interaction)}")
            c.add("Credit", f"{identity.label(recipient)}\n{fmt.amount_lines(amts)}")
            c.add("Funded from", src_text, True)
            c.add("Reason", reason)
            if override:
                c.add("⚠️ DEFICIT OVERRIDE", f"Source is short. Justification: {override_reason}")
            c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
            await svc.alerts.econ(c)
            return await reply(interaction, f"Approval request #{approval_id} posted. After a different Minister approves it, "
                                            "run this same command again.")
        card = A.Card("Confirm CREDIT", "Both sides are recorded. This is permanent and audited.", A.ORANGE)
        card.add("Credit to", identity.label(recipient), True)
        card.add("Funded from", src_text, True)
        card.add("Amounts", fmt.amount_lines(amts) + "\n" + fmt.value_line(val))
        card.add("Recipient balance", "\n".join(f"{M.LABELS[r]}: {M.fmt_units(r, before_r.get(r, 0))} → {M.fmt_units(r, before_r.get(r, 0) + a)}"
                                                for r, a in amts.items()))
        if avail is not None and not short:
            card.add("Source after", "\n".join(f"{M.LABELS[r]}: {M.fmt_units(r, avail.get(r, 0))} → {M.fmt_units(r, avail.get(r, 0) - a)}"
                                                 for r, a in amts.items()))
        if short:
            card.add("⚠️ DEFICIT OVERRIDE", "The source is short by:\n" + "\n".join(f"{M.LABELS[r]}: {M.fmt_units(r, v)}" for r, v in short.items())
                     + f"\nThis creates a deficit that reconciliation will report. Justification: {override_reason}")
        card.add("Reason", reason)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled.")
        import uuid
        idem = f"credit:{uuid.uuid4().hex}"
        try:
            def do():
                with svc.db.tx() as conn:
                    return T.post_transfer(conn, recipient=recipient, amounts=amts, source_type=stype, source_id=sid, reason=reason,
                                           reference=reference, operator=uid(interaction), approval_id=approval_id, idem=idem,
                                           holdings=holdings, snapshot_id=snap.id if snap else None, override=override,
                                           override_reason=override_reason)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        out = A.Card(f"Credit {res['ref']}", f"{identity.label(recipient)} was credited. Both sides are recorded.", A.GREEN, kind="ADJUSTMENT")
        out.add("Amounts", fmt.amount_lines(amts) + "\n" + fmt.value_line(val))
        out.add("Funded from", src_text, True)
        out.add("Reason", reason)
        if res.get("overridden"):
            out.add("⚠️ Deficit override", f"By {actor_label(interaction)}: {override_reason}")
        out.add("Reference", f"{res['ref']} · approval #{approval_id}", True)
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
        await svc.alerts.flush_notifications()                 # recipient (and source member) DMs, queued with the change

    # ----------------------------------------------------------- approvals
    @bank.command(name="approve", description="ECON: approve a pending request made by someone else")
    async def approve(interaction: discord.Interaction, approval_id: int):
        if not await need(svc, interaction, "MINISTER"):
            return
        try:
            def do():
                with svc.db.tx() as conn:
                    ap = L.approve(conn, approval_id, uid(interaction))
                    return dict(ap)
            ap = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"Approved request #{approval_id}. The requester must now re-run their command.")
        await svc.alerts.econ(A.Card("Approval granted", f"Request #{approval_id} ({ap['kind']}) approved by "
                                     f"{actor_label(interaction)}.", A.GREEN))

    @bank.command(name="approvals", description="ECON: list pending approval requests")
    async def approvals(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            rows = conn.execute("SELECT * FROM approval_requests WHERE status='PENDING' ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            return await reply(interaction, card=A.Card(f"{icons.status('key')} Pending approvals", "No pending approvals.", A.BLUE))

        def render(r, i, n):
            c = A.Card(f"{icons.status('ok') if r['approved_by'] else icons.status('wait')} Approval #{r['id']} · {r['kind'].replace('_', ' ').title()}",
                       r["reason"] or "", A.GREEN if r["approved_by"] else A.ORANGE)
            c.add("Requested by", f"<@{r['requested_by']}>", True)
            c.add("Status", f"approved by <@{r['approved_by']}>" if r["approved_by"] else "waiting for a second approver", True)
            c.add("Expires", r["expires_at"][:16].replace("T", " "), True)
            try:
                pl = json.loads(r["payload_json"])
                bits = []
                if "amounts" in pl:
                    bits.append(fmt.short_amounts(pl["amounts"]))
                if "deltas" in pl:
                    bits.append(fmt.signed_lines(pl["deltas"]))
                if bits:
                    c.add("Details", "\n".join(bits))
            except (ValueError, TypeError):
                pass
            return c

        async def a_approve(i, r):
            await approve.callback(i, r["id"])

        async def a_revoke(i, r):
            await revoke.callback(i, r["id"])
        pager = ItemPager(interaction.user.id, list(rows), render,
                          [("Approve", "✅", "success", a_approve), ("Revoke", "❌", "danger", a_revoke)])
        await reply(interaction, card=pager.card(), view=pager)

    @bank.command(name="revoke", description="ECON: cancel a pending approval request")
    async def revoke(interaction: discord.Interaction, approval_id: int):
        if not await need(svc, interaction, "MINISTER"):
            return
        def do():
            with svc.db.tx() as conn:
                return L.revoke_approval(conn, approval_id, uid(interaction))
        ok = await asyncio.to_thread(do)
        await reply(interaction, "Revoked." if ok else "That request is not pending.")

    @bankset.command(name="requireapproval", description="Admin: transfers worth more than this ($) need a second approver (0 = off)")
    async def requireapproval(interaction: discord.Interaction, dollars: int):
        if not await need(svc, interaction, "ADMIN"):
            return
        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, "approval_threshold_value", str(max(0, dollars)), uid(interaction))
                L.audit(conn, uid(interaction), "CONFIG_CHANGED", "approval_threshold_value", {"value": dollars})
        await asyncio.to_thread(do)
        await reply(interaction, f"Approval threshold set to ${max(0, dollars):,}." if dollars > 0 else "Approval requirement switched off.")

    @bankset.command(name="gate", description="Admin: turn the reconciliation transaction gate ON or OFF (monitoring always stays on)")
    @app_commands.describe(mode="ON: known deficits block banking · OFF: they are reported but banking continues")
    @app_commands.choices(mode=[app_commands.Choice(name="ON (deficits block banking)", value="on"),
                                app_commands.Choice(name="OFF (report only, banking continues)", value="off"),
                                app_commands.Choice(name="Show current setting", value="show")])
    async def gate(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await need(svc, interaction, "ADMIN"):
            return
        with svc.db.read() as conn:
            now_on = L.reconcile_gate_enabled(conn)
            nblock = len(L.blocking_events(conn))
            nopen = L.integrity_state(conn)["open_events"]
        status = (f"{icons.status('lock')} **ON** — open reconciliation deficits block withdrawals, conversions and deposits-from-Discord."
                  if now_on else f"{icons.status('ok')} **OFF** — deficits are still detected, reported and audited, but they do NOT block banking.")
        if mode.value == "show":
            return await reply(interaction, f"Reconciliation transaction gate: {status}\nOpen findings: **{nopen}** · currently blocking: **{nblock}**")
        want_on = mode.value == "on"
        if want_on == now_on:
            return await reply(interaction, f"Already set. Reconciliation transaction gate: {status}")
        await thinking(interaction)
        c = A.Card("Change the reconciliation gate", "", A.ORANGE)
        c.add("New setting", "**ON** — deficits will block banking again" if want_on else "**OFF** — deficits will be reported but will not block banking", False)
        c.add("What stays ON either way", "Emergency lock, ECON pause, hash-chain and balance-integrity checks, uncertain/stuck PnW transfer protection, "
                                         "duplicate protection, confirmations, limits, approvals, permissions, audit log.", False)
        c.add("Never happens automatically", "No member balance and no ownership share is changed to make reconciliation pass.", False)
        if not await confirm(svc, interaction, c):
            return await interaction.followup.send("Cancelled. The gate was not changed.", ephemeral=True)
        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, "reconcile_gate_enabled", "1" if want_on else "0", uid(interaction))
                L.audit(conn, uid(interaction), "RECONCILE_GATE_CHANGED", "reconcile_gate_enabled", {"enabled": want_on})
        await asyncio.to_thread(do)
        await interaction.followup.send(f"Reconciliation transaction gate is now **{'ON' if want_on else 'OFF'}**.", ephemeral=True)

    # -------------------------------------------------------------- limits
    @bankset.command(name="limits", description="ECON: show all transfer limits")
    async def limits_list(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            rows = LIM.list_limits(conn)
        lines = [f"**{r['scope']}** `{r['scope_id']}`: per-transfer {fmt.dollars(r['per_tx_cents']) if r['per_tx_cents'] else 'none'}, "
                 f"daily {fmt.dollars(r['daily_cents']) if r['daily_cents'] else 'none'}" for r in rows]
        await reply(interaction, "\n".join(lines) or "No limits set. Limits are measured in Current Market Value.")

    async def _set_limit(interaction, scope, sid, amount, daily=False, force_daily=False):
        if not await need(svc, interaction, "ADMIN"):
            return
        try:
            cents = _cents(amount)
        except M.AmountError as exc:
            return await reply(interaction, str(exc))
        is_daily = daily or force_daily
        def do():
            with svc.db.tx() as conn:
                LIM.set_limit(conn, scope, sid, per_tx_cents=None if is_daily else cents,
                              daily_cents=cents if is_daily else None, actor=uid(interaction))
                L.audit(conn, uid(interaction), "LIMIT_SET", f"{scope}:{sid}", {"cents": cents, "daily": is_daily})
        await asyncio.to_thread(do)
        await reply(interaction, f"{'Daily' if is_daily else 'Per-transfer'} limit for {scope} {sid}: "
                                 f"{fmt.dollars(cents) if cents else 'removed'}.")

    @bankset.command(name="settransferlimit", description="Admin: global per-transfer limit for member self-withdrawals (e.g. 500m; 0 removes)")
    async def settransferlimit(interaction: discord.Interaction, amount: str):
        await _set_limit(interaction, "GLOBAL", "*", amount)

    @bankset.command(name="setdailylimit", description="Admin: global daily limit for member self-withdrawals")
    async def setdailylimit(interaction: discord.Interaction, amount: str):
        await _set_limit(interaction, "GLOBAL", "*", amount, force_daily=True)

    @bankset.command(name="setrolelimit", description="Admin: per-transfer (or daily) limit for a staff role's withdrawals")
    async def setrolelimit(interaction: discord.Interaction, role: discord.Role, amount: str, daily: bool = False):
        await _set_limit(interaction, "ROLE", str(role.id), amount, daily)

    @bankset.command(name="setnationlimit", description="Admin: limit for one member nation")
    async def setnationlimit(interaction: discord.Interaction, nation: str, amount: str, daily: bool = False):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        await _set_limit(interaction, "NATION", str(nation_id), amount, daily)

    # ------------------------------------------------------------- bankers
    @bankset.command(name="addbanker", description="Admin: give a user Banker access (in addition to roles)")
    async def addbanker(interaction: discord.Interaction, user: discord.User):
        if not await need(svc, interaction, "ADMIN"):
            return
        def do():
            with svc.db.tx() as conn:
                CA.record(conn, actor=uid(interaction), setting="banker_access", previous="no",
                          new="yes", target=f"user <@{user.id}>", category="PERMISSION")
                conn.execute("INSERT OR IGNORE INTO bankers(discord_id,added_by,added_at) VALUES(?,?,datetime('now'))",
                             (str(user.id), uid(interaction)))
                L.audit(conn, uid(interaction), "BANKER_ADDED", f"user:{user.id}", {})
        await asyncio.to_thread(do)
        await reply(interaction, f"{user.mention} is now a Banker.")

    @bankset.command(name="removebanker", description="Admin: remove a user's Banker access")
    async def removebanker(interaction: discord.Interaction, user: discord.User):
        if not await need(svc, interaction, "ADMIN"):
            return
        def do():
            with svc.db.tx() as conn:
                CA.record(conn, actor=uid(interaction), setting="banker_access", previous="yes",
                          new="no", target=f"user <@{user.id}>", category="PERMISSION")
                conn.execute("DELETE FROM bankers WHERE discord_id=?", (str(user.id),))
                L.audit(conn, uid(interaction), "BANKER_REMOVED", f"user:{user.id}", {})
        await asyncio.to_thread(do)
        await reply(interaction, f"{user.mention} is no longer a Banker.")

    @bankset.command(name="listbankers", description="ECON: list bankers and permission roles")
    async def listbankers(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            bs = conn.execute("SELECT discord_id FROM bankers").fetchall()
            rs = perms.list_roles(conn)
        lines = [f"Banker: <@{b['discord_id']}>" for b in bs] + [f"{r['level']}: <@&{r['role_id']}>" for r in rs]
        await reply(interaction, "\n".join(lines) or "Nobody configured yet. Use `/bankset setrole`.")

    @bankset.command(name="setrole", description="Admin: assign a Discord role to a permission level")
    @app_commands.choices(level=[app_commands.Choice(name=n, value=n) for n in ("AUDITOR", "BANKER", "MINISTER", "ADMIN")])
    async def setrole(interaction: discord.Interaction, level: app_commands.Choice[str], role: discord.Role, remove: bool = False):
        if not await need(svc, interaction, "ADMIN"):
            return
        def do():
            with svc.db.tx() as conn:
                perms.set_role(conn, level.value, str(role.id), add=not remove, actor=uid(interaction))
                L.audit(conn, uid(interaction), "ROLE_PERMISSION", f"role:{role.id}", {"level": level.value, "remove": remove})
        await asyncio.to_thread(do)
        await reply(interaction, f"{role.mention} {'no longer has' if remove else 'now has'} **{level.value}**.")

    CHANNEL_KINDS = [app_commands.Choice(name="ECON log (financial alerts)", value="econ_log_channel_id"),
                     app_commands.Choice(name="Tax alerts (one summary per turn)", value="tax_alert_channel_id"),
                     app_commands.Choice(name="Trade alerts (private)", value="trade_alert_channel_id"),
                     app_commands.Choice(name="Configuration audit (private)", value="config_audit_channel_id")]

    @bankset.command(name="setlogchannel", description="Admin: set the ECON log channel or the tax-alert channel")
    @app_commands.describe(channel="The channel (keep it private)", kind="Which feed this channel receives (default: ECON log)")
    @app_commands.choices(kind=CHANNEL_KINDS)
    async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel,
                            kind: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        key = kind.value if kind else "econ_log_channel_id"

        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, key, str(channel.id), uid(interaction))
                L.audit(conn, uid(interaction), "CONFIG_CHANGED", key, {"value": channel.id})
        await asyncio.to_thread(do)
        label = {"tax_alert_channel_id": "Tax alert", "trade_alert_channel_id": "Trade alert", "config_audit_channel_id": "Configuration audit"}.get(key, "ECON log")
        await reply(interaction, f"{label} channel set to {channel.mention}. Discord decides who can read it, so keep it private.")
        card = A.Card(f"{label} channel connected", "Alerts will appear here.", A.GREEN)
        if key == "tax_alert_channel_id":
            await svc.alerts.tax(card)
        elif key == "trade_alert_channel_id":
            await svc.alerts.trade(card)
        elif key == "config_audit_channel_id":
            await svc.alerts.flush_config_audit()         # queued entries are posted now
        else:
            await svc.alerts.econ(card)

    # ----------------------------------------------------- confidential-information access
    FLAG_CHOICES = [app_commands.Choice(name=f, value=f) for f in perms.FLAGS]

    @bankset.command(name="setaccess", description="Admin: give a role a special permission (confidential info, spending members' funds)")
    @app_commands.describe(permission="Which confidential permission", role="The Discord role", remove="Remove instead of add")
    @app_commands.choices(permission=FLAG_CHOICES)
    async def setaccess(interaction: discord.Interaction, permission: app_commands.Choice[str], role: discord.Role, remove: bool = False):
        if not await need(svc, interaction, "ADMIN"):
            return

        def do():
            with svc.db.tx() as conn:
                perms.set_flag_role(conn, permission.value, str(role.id), add=not remove, actor=uid(interaction))
                L.audit(conn, uid(interaction), "ACCESS_FLAG", permission.value, {"role": str(role.id), "remove": remove})
        await asyncio.to_thread(do)
        await reply(interaction, f"{role.mention} {'no longer has' if remove else 'now has'} `{permission.value}`.")

    @bankset.command(name="access", description="Admin: who can see confidential alliance information")
    async def access(interaction: discord.Interaction):
        if not await need(svc, interaction, "ADMIN"):
            return
        with svc.db.read() as conn:
            rows = perms.list_flag_roles(conn)
        c = A.Card(f"{icons.status('lock')} Confidential access",
                   "Seeing members' accounts (Auditor/Banker/Minister) is **separate** from seeing the alliance treasury. "
                   "Admins always have both.", A.BLUE)
        for flag, desc in perms.FLAGS.items():
            roles = [f"<@&{r['role_id']}>" for r in rows if r["flag"] == flag]
            c.add(f"`{flag}`", desc + "\n**Roles:** " + (", ".join(roles) if roles else "_none: Admins only_"))
        await reply(interaction, card=c)

    # --------------------------------------------------------------- link a nation (admin)
    @bank.command(name="linknation", description="Admin/ECON: link or relink a PnW nation to a Discord member")
    @app_commands.describe(member="The Discord member", nation="Nation: id, name or link", force="ADMIN only: replace an existing link")
    async def linknation(interaction: discord.Interaction, member: discord.User, nation: str, force: bool = False):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        try:
            info = await svc.pnw.fetch_nation(nid)
        except (PnWRejected, PnWUncertain) as exc:
            info = None
            verify_note = f"PnW could not be reached ({exc}); the nation could not be verified."
        else:
            verify_note = ""
        if info is not None and int(info.get("alliance_id") or 0) not in svc.settings.bank_ids:
            return await reply(interaction, f"{icons.status('bad')} Nation {nid} is not in our alliance, so it can't be linked.")
        if info is None and not verify_note:
            return await reply(interaction, f"{icons.status('bad')} PnW has no nation {nid}.")
        names = {member.name.lower(), str(member).lower()}
        verified = bool(info and (info.get("discord") or "").strip().lower() in names)
        with svc.db.read() as conn:
            nrow = L.get_member(conn, nid)
            mrow = L.member_by_discord(conn, member.id)
        owner = nrow["discord_id"] if nrow and nrow["discord_id"] else None
        other_nation = mrow["nation_id"] if mrow and mrow["nation_id"] != nid else None
        if owner == str(member.id):
            return await reply(interaction, f"{icons.status('ok')} {member.mention} is already linked to {identity.label(nid)}. Nothing to change.")
        conflict = bool(owner or other_nation)
        if conflict:
            lines = []
            if owner:
                lines.append(f"{identity.label(nid)} is already linked to <@{owner}>.")
            if other_nation:
                lines.append(f"{member.mention} is already linked to {identity.label(other_nation)}.")
            if not force or not await need_admin_quietly(interaction):
                c = A.Card(f"{icons.status('warn')} Link conflict: nothing was changed", "\n".join(lines), A.ORANGE)
                c.add("Why this matters", "A link decides who can withdraw a nation's deposit, so it is never changed silently.")
                c.add("To replace it", "An **Admin** runs the same command with `force:true`. Both people are shown in the confirmation and the change is logged.")
                return await reply(interaction, card=c)
        label = (info or {}).get("nation_name") or f"nation {nid}"
        card = A.Card(f"{icons.status('lock')} Confirm nation link", "A link gives this Discord account full control of the nation's TUN deposit.", A.ORANGE)
        card.add("Member", f"{member.mention} ({member.name})", True)
        card.add("Nation", f"{label} [#{nid}]", True)
        card.add("PnW Discord field", (f"{info.get('discord') or '—'} " + ("✅ matches" if verified else "⚠️ does not match")) if info else verify_note)
        if conflict:
            card.add("REPLACES", "\n".join(lines))
        card.add("Money", "Balances stay with the nation; only who may use them changes.")
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was changed.")
        actor = uid(interaction)

        def do():
            with svc.db.tx() as conn:
                L.ensure_member(conn, nid, (info or {}).get("nation_name"))
                CA.record(conn, actor=actor, setting="nation_link", previous=(f"<@{owner}>" if owner else "unlinked"), new=f"<@{member.id}>",
                          target=f"nation [#{nid}]" + (f" · member was on nation [#{other_nation}]" if other_nation else "") +
                                 ("" if verified else " · PnW's Discord field did not match"), category="LINK", only_if_changed=False)
                if owner:
                    conn.execute("UPDATE members SET discord_id=NULL, discord_name=NULL WHERE nation_id=?", (nid,))
                    CR.remove(conn, nid, actor, "nation relinked: the previous owner's saved API key was deleted")
                if other_nation:
                    conn.execute("UPDATE members SET discord_id=NULL, discord_name=NULL WHERE nation_id=?", (other_nation,))
                    CR.remove(conn, other_nation, actor, "member moved to another nation: the saved API key was deleted")
                    conn.execute("INSERT INTO nation_link_history(nation_id,discord_id,previous_discord_id,action,actor,verified,note,at) "
                                 "VALUES(?,?,?,?,?,?,?,?)", (other_nation, None, str(member.id), "UNLINKED_BY_RELINK", actor, int(verified), f"moved to nation {nid}", B_now()))
                conn.execute("UPDATE members SET discord_id=?, discord_name=?, linked_at=datetime('now') WHERE nation_id=?",
                             (str(member.id), member.name, nid))
                conn.execute("INSERT INTO nation_link_history(nation_id,discord_id,previous_discord_id,action,actor,verified,note,at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (nid, str(member.id), owner, "RELINKED" if conflict else "LINKED", actor, int(verified),
                                                         "admin link" + ("" if verified else " (PnW Discord field did not match)"), B_now()))
                L.audit(conn, actor, "NATION_LINKED_BY_ADMIN", f"nation:{nid}", {
                    "member": str(member.id), "previous_owner": owner, "member_previous_nation": other_nation,
                    "verified_with_pnw": verified, "forced": bool(conflict)})
        await asyncio.to_thread(do)
        out = A.Card(f"{icons.status('ok')} Nation linked", f"{member.mention} ↔ {label} [#{nid}]", A.GREEN, kind="LINK")
        out.add("By", actor_label(interaction), True)
        out.add("Verified against PnW", "yes" if verified else "no (recorded as an Admin decision)", True)
        await reply(interaction, card=out)
        await svc.alerts.econ(out)

    async def need_admin_quietly(interaction) -> bool:
        return await need(svc, interaction, "ADMIN")

    @bankset.command(name="config", description="Admin: view the bank settings in plain language, or change one")
    @app_commands.describe(setting="Which setting (type to search by name) - leave empty to browse them all",
                           value="The new value, e.g. on / off, 5m, 2h, #channel",
                           category="Jump to one page of settings")
    @app_commands.choices(category=[app_commands.Choice(name=g, value=g) for g in CV.GROUPS])
    async def config(interaction: discord.Interaction, setting: str = "", value: str = "",
                     category: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        if not setting:
            groups = list(CV.GROUPS)
            with svc.db.read() as conn:
                pages = [("overview", CV.render_overview(conn))] + [(g, CV.render_group(conn, g)) for g in groups]
            pager = ItemPager(interaction.user.id, pages, lambda item, i, n: item[1])
            if category is not None:
                pager.index = 1 + groups.index(category.value)
                pager._sync()
            return await reply(interaction, card=pager.card(), view=pager)
        key = CV.find_key(setting)
        if key is None:
            return await reply(interaction, "I don't know that setting. Run `/bankset config` with nothing filled in to browse them, "
                                            "or start typing a name and pick it from the list.")
        meta = CV.SETTINGS[key]
        if not value:
            with svc.db.read() as conn:
                cur = cfg_get(conn, key)
            c = A.Card(f"⚙️ {meta.label}", meta.help or "", A.BLUE)
            c.add("Now", CV.human(key, cur), True)
            c.add("Default", CV.human(key, DEFAULTS[key][0]), True)
            c.add("To change it", f"`/bankset config setting:{meta.label} value:…`")
            return await reply(interaction, card=c)
        try:
            new_value = CV.parse_value(key, value)
        except ValueError as exc:
            return await reply(interaction, f"Not changed. {exc}")
        if key == "reconcile_gate_enabled":
            return await reply(interaction, "Use **`/bankset gate`** for that one: it asks for a confirmation and explains what stays protected.")
        def do():
            with svc.db.tx() as conn:
                old = cfg_get(conn, key)
                cfg_set(conn, key, new_value, uid(interaction))
                L.audit(conn, uid(interaction), "CONFIG_CHANGED", key, {"value": new_value, "previous": old})
                if key.startswith("icon_"):
                    icons.load_from_db(conn)
                return old
        old = await asyncio.to_thread(do)
        c = A.Card(f"⚙️ {meta.label} changed", "", A.GREEN)
        c.add("Before", CV.human(key, old), True)
        c.add("Now", CV.human(key, new_value), True)
        await reply(interaction, card=c)

    @config.autocomplete("setting")
    async def config_setting_autocomplete(interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        out = []
        for g, keys in CV.grouped().items():
            for k in keys:
                lab = CV.SETTINGS[k].label
                if not cur or cur in lab.lower() or cur in k:
                    out.append(app_commands.Choice(name=f"{lab} · {g}"[:100], value=k))
        return out[:25]

    # ------------------------------------------------- reset CONFIGURATION (never financial data)
    @bankset.command(name="resetconfig", description="Admin / server owner: reset CONFIGURATION to defaults (never balances, members or history)")
    @app_commands.describe(scope="What to reset", reason="Why (mandatory, audited)",
                           confirm_phrase=f"Type {CRST.RESET_PHRASE} to confirm you understand")
    @app_commands.choices(scope=[app_commands.Choice(name="Settings only (keeps roles, bankers, limits)", value="settings"),
                                 app_commands.Choice(name="All configuration (settings, roles, bankers, limits, trade lists)", value="all")])
    async def resetconfig(interaction: discord.Interaction, scope: app_commands.Choice[str], reason: str, confirm_phrase: str):
        guild = getattr(interaction, "guild", None)
        owner = bool(guild and getattr(guild, "owner_id", None) == interaction.user.id)
        if not owner and not await need(svc, interaction, "ADMIN"):
            return
        if confirm_phrase.strip() != CRST.RESET_PHRASE:
            return await reply(interaction, f"Nothing was changed. To reset, type exactly `{CRST.RESET_PHRASE}` in the confirm_phrase box.")
        if not reason.strip():
            return await reply(interaction, "A reason is mandatory.")
        if scope.value == "all" and not svc.settings.owner_ids:
            return await reply(interaction, "Nothing was changed: no bot owner is set in the environment (OWNER_DISCORD_IDS), so resetting the "
                                            "roles too would leave nobody able to administer the bank. Set an owner first, or reset 'Settings only'.")
        await thinking(interaction)
        with svc.db.read() as conn:
            pv = CRST.preview(conn, scope.value)
        c = A.Card("Confirm: reset CONFIGURATION", "This changes how the bot is set up. It does NOT touch money.", A.RED)
        c.add("Will be reset", "\n".join(f"• {CRST.DESCRIPTION[t]}" + (f" ({n} changed)" if t == "bank_config" else f" ({n})") for t, n in pv["will_reset"].items()))
        c.add("Will NOT be touched, ever", "Member accounts and Discord/nation links · member API keys · balances · the ledger · transactions and PnW records · "
                                          "loans · offshore ownership · audit history · emergency lock and bank state.")
        if scope.value == "all":
            c.add("After this", "Only the bot owners in the environment (OWNER_DISCORD_IDS) can administer the bank until you set the roles again with `/bankset setrole`.")
        c.add("Safety", "A backup is taken first. The previous values are written to the audit log. If any protected record would change, the reset is cancelled.")
        c.add("Reason", reason)
        if not await confirm(svc, interaction, c):
            return await interaction.followup.send("Cancelled. Nothing was changed.", ephemeral=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            await asyncio.to_thread(svc.db.backup_to, svc.settings.backup_dir / f"pre-config-reset-{stamp}.db")
        except Exception as exc:  # noqa: BLE001
            log.exception("backup before config reset failed")
            return await interaction.followup.send(f"Nothing was changed: the safety backup failed ({exc}).", ephemeral=True)
        try:
            def do():
                with svc.db.tx() as conn:
                    done = CRST.reset(conn, scope=scope.value, actor=uid(interaction), reason=reason)
                    icons.load_from_db(conn)
                    return done
            done = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await interaction.followup.send(f"Not applied: {exc}", ephemeral=True)
        out = A.Card("Configuration reset", f"By {actor_label(interaction)} · {reason}", A.ORANGE, kind="CONFIG")
        out.add("Reset", "\n".join(f"• {CRST.DESCRIPTION[t]}: {n}" for t, n in done.items()))
        out.add("Untouched", "Balances, members, links, API keys, transactions, loans, ledgers and audit history.")
        out.add("Safety backup", f"`pre-config-reset-{stamp}.db`", True)
        await interaction.followup.send(embed=A.to_embed(out), ephemeral=True)
        await svc.alerts.config_audit(out)

    # ------------------------------------------------------------ icons
    RES_CHOICES = [app_commands.Choice(name=M.LABELS[r], value=r) for r in M.RESOURCES]

    @bankset.command(name="seticon", description="Admin: change a resource icon (emoji or custom server emoji)")
    @app_commands.describe(resource="Which resource", emoji="An emoji, a custom emoji like <:oil:123...>, or 'default'")
    @app_commands.choices(resource=RES_CHOICES)
    async def seticon(interaction: discord.Interaction, resource: app_commands.Choice[str], emoji: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        value = "" if emoji.strip().lower() == "default" else emoji.strip()
        if value and not icons.valid_emoji(value):
            return await reply(interaction, "That doesn't look like an emoji. Use a normal emoji, or a custom server emoji "
                                            "code such as `<:oil:123456789012345678>` (type `\\:oil:` in Discord to get it).")
        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, f"icon_{resource.value}", value, uid(interaction))
                L.audit(conn, uid(interaction), "ICON_CHANGED", resource.value, {"emoji": value or "default"})
                icons.load_from_db(conn)
        await asyncio.to_thread(do)
        await reply(interaction, f"{icons.resource(resource.value)} **{resource.name}** icon is now shown as {icons.resource(resource.value)}.")

    @bankset.command(name="iconpack", description="Admin: use the PnW resource icons uploaded to the bot (or go back to the defaults)")
    @app_commands.choices(action=[app_commands.Choice(name="Apply the PnW icon pack", value="apply"),
                                  app_commands.Choice(name="Go back to the default icons", value="revert")])
    async def iconpack(interaction: discord.Interaction, action: app_commands.Choice[str]):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        bot = svc.alerts.bot
        found = {}
        if action.value == "apply":
            try:
                found = icons.pack_from(await bot.fetch_application_emojis())
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read the bot's application emojis: %s", exc)
                return await reply(interaction, "I couldn't read the bot's emojis from Discord just now, so nothing was changed. Try again in a minute.")
            if not found:
                return await reply(interaction, "No PnW icons are uploaded to the bot yet (emojis named `pnw_money`, `pnw_food`...). "
                                                "Run `python scripts/install_pnw_icons.py` first (see docs/19_ICONS.md). Nothing was changed.")
        def do():
            with svc.db.tx() as conn:
                for res in M.RESOURCES:
                    new = found.get(res, "") if action.value == "apply" else ""
                    if cfg_get(conn, f"icon_{res}") != new:
                        cfg_set(conn, f"icon_{res}", new, uid(interaction))
                L.audit(conn, uid(interaction), "ICON_PACK", action.value, {"applied": sorted(found)})
                icons.load_from_db(conn)
        await asyncio.to_thread(do)
        missing = [M.LABELS[r] for r in M.RESOURCES if r not in found]
        text = ("\n".join(f"{icons.resource(r)}  {M.LABELS[r]}" for r in M.RESOURCES))
        c = A.Card(f"{icons.status('ok')} Icons updated", "PnW icons applied." if action.value == "apply" else "Default icons restored.", A.GREEN)
        c.add("Now showing", text)
        if action.value == "apply" and missing:
            c.add("Using the default instead (no PnW icon uploaded)", ", ".join(missing))
        await reply(interaction, card=c)

    @bankset.command(name="icons", description="Admin: preview every resource icon")
    async def show_icons(interaction: discord.Interaction):
        if not await need(svc, interaction, "ADMIN"):
            return
        c = A.Card(f"{icons.status('info')} Resource icons", "Change one with `/bankset seticon`. "
                   "Custom server emojis work too, so you can use the TUN set.", A.BLUE)
        c.add("Current icons", "\n".join(f"{icons.resource(r)}  {M.LABELS[r]}" for r in M.RESOURCES))
        await reply(interaction, card=c)

    # ------------------------------------------------------------ records / exports
    @bank.command(name="records", description="ECON: export records to Excel")
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in X.KINDS])
    async def records(interaction: discord.Interaction, kind: app_commands.Choice[str]):
        # alliance-wide money needs its own permission; member-level exports need only staff access
        required = {"configaudit": "ADMIN", "vault": "FLAG:bank_view_alliance_holdings", "offshore": "FLAG:bank_view_alliance_holdings",
                    "grants": "FLAG:bank_view_alliance_holdings", "tax": "FLAG:bank_view_tax"}.get(kind.value, "AUDITOR")
        if not await need(svc, interaction, required):
            return
        await thinking(interaction)
        snap = await svc.prices.get()
        def do():
            with svc.db.read() as conn:
                data = X.build(conn, kind.value, snap)
            with svc.db.tx() as conn:
                L.audit(conn, uid(interaction), "EXPORT", kind.value, {})
            return data
        data, name = await asyncio.to_thread(do)
        await reply(interaction, f"Export ready ({kind.value}). Prices as of {snap.fetched_at if snap else 'UNAVAILABLE'}.",
                    file=xlsx_file(data, name))

    @bank.command(name="resolvetx", description="ECON: settle an uncertain/stuck withdrawal by checking the PnW records")
    @app_commands.choices(action=[app_commands.Choice(name="check (look for the PnW record)", value="check"),
                                  app_commands.Choice(name="mark_failed (only if PnW shows nothing sent)", value="mark_failed")])
    async def resolvetx(interaction: discord.Interaction, tx_id: int, action: app_commands.Choice[str]):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        try:
            msg = await svc.wd.resolve(tx_id, action.value, uid(interaction))
        except (L.LedgerError, PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"Could not settle: {exc}")
        await reply(interaction, msg)
        await svc.alerts.flush_events()

    # --------------------------------------------------------------- review
    @bank.command(name="review", description="ECON: decide on a PnW record the bot could not classify (leave record_id empty to list)")
    @app_commands.choices(action=[app_commands.Choice(name="credit (it is a member deposit)", value="credit"),
                                  app_commands.Choice(name="alliance (alliance money, no credit)", value="alliance"),
                                  app_commands.Choice(name="withdrawal (charge it to the member who received it)", value="withdrawal"),
                                  app_commands.Choice(name="dismiss (acknowledge)", value="dismiss")])
    async def review(interaction: discord.Interaction, record_id: int = 0, action: Optional[app_commands.Choice[str]] = None,
                     nation: str = "", note: str = ""):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = 0
        if nation.strip():
            nation_id = await nation_arg(svc, interaction, nation)
            if nation_id is None:
                return
        if not record_id:
            with svc.db.read() as conn:
                rows = conn.execute("SELECT * FROM pnw_records WHERE status='AWAITING_REVIEW' ORDER BY id LIMIT 15").fetchall()
            if not rows:
                return await reply(interaction, card=A.Card(f"{icons.status('audit')} Records waiting for review",
                                                            "Nothing is waiting for review.", A.GREEN))
            snap0 = await svc.prices.get()

            def render(r, i, n):
                amounts = json.loads(r["amounts_json"])
                c = A.Card(f"{icons.status('warn')} PnW record #{r['id']} needs a decision",
                           "The bot could not safely classify this on its own.", A.ORANGE)
                c.add("Direction", r["direction"], True)
                c.add("From nation" if r["direction"] == "IN" else "To nation",
                      identity.label(r['sender_id'] if r['direction'] == 'IN' else r['receiver_id']), True)
                c.add("Looks like", r["classification"].replace("_", " ").title(), True)
                c.add("Contents", fmt.amounts_with_value(amounts, value_amounts(amounts, snap0)))
                c.add("Note on the record", f"`{r['note'] or '—'}`")
                c.add("Choose", "**Credit** = it is a member's deposit · **Alliance money** = no credit · **Dismiss** = just acknowledge")
                return c

            def decide(kind, needs_nation):
                async def cb(i, r):
                    fields = ([dict(label="Which nation gets the credit?", placeholder="id, name, link or @user", max=100)]
                              if needs_nation else []) + [dict(label="Why? (required)", max=200)]

                    async def done(i2, *vals):
                        nat, why = (vals[0], vals[1]) if needs_nation else ("", vals[0])
                        await review.callback(i2, r["id"], app_commands.Choice(name=kind, value=kind), nat, why)
                    await open_form(i, f"Record #{r['id']}: {kind}", fields, done)
                return cb
            pager = ItemPager(interaction.user.id, list(rows), render, [
                ("Credit to a nation…", "✅", "success", decide("credit", True)),
                ("Alliance money", "🛡️", "secondary", decide("alliance", False)),
                ("Charge to member", "📤", "danger", decide("withdrawal", False)),
                ("Dismiss", "🗑️", "secondary", decide("dismiss", False))])
            return await reply(interaction, card=pager.card(), view=pager)
        if action is None or not note:
            return await reply(interaction, "Give an `action` and a `note` explaining your decision.")
        snap = await svc.prices.get()
        try:
            def do():
                with svc.db.tx() as conn:
                    return REC.resolve_review(conn, record_id=record_id, action=action.value, actor=uid(interaction),
                                              note=note, nation_id=nation_id or None,
                                              snapshot_id=snap.id if snap else None,
                                              offshore_id=svc.settings.offshore.alliance_id if svc.settings.offshore else None)
            out = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        await reply(interaction, f"Record #{record_id} resolved ({action.value}).")
        if out.kind in ("CREDIT", "EXTERNAL_WITHDRAWAL"):
            out.valuation = value_amounts(out.amounts, snap)
            await post_outcomes(svc, [out])

    # --------------------------------------------------------- opening balances
    @bankset.command(name="importopening", description="Admin: import balances from a spreadsheet (opening, or restore after a reset)")
    @app_commands.describe(file=".xlsx or .csv: nation_id and/or nation_name, one column per resource, optional loan column",
                           note="Where this data came from")
    async def importopening(interaction: discord.Interaction, file: discord.Attachment, note: str):
        """ONE import command. If a /deposit reset is waiting for its balances it restores them (negatives and loans
        allowed); otherwise it is the normal first-time opening-balance import."""
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        if file.size > importer.MAX_BYTES:
            return await reply(interaction, "File is too large (max 5 MB).")
        from . import deposit_reset as DR
        from .cmds_deposit import import_preview_card
        with svc.db.read() as conn:
            rs = DR.open_reset(conn)
            nonzero = conn.execute("SELECT COUNT(*) FROM balances WHERE amount != 0").fetchone()[0] if rs else 0
        kind = "RESTORE" if rs else "OPENING"
        data = await file.read()
        snap = await svc.prices.get()
        try:
            members = await svc.pnw.fetch_alliance_members()
        except (PnWRejected, PnWUncertain):
            members = None
        p = await asyncio.to_thread(importer.preview, file.filename, data, members=members, snapshot=snap, mode=kind)
        c = import_preview_card(p, f"Restore PREVIEW (after deposit reset #{rs['id']})" if rs else "Opening-balance import PREVIEW",
                                current_nonzero=nonzero)
        c.add("Import type", (f"RESTORE: these become the balances after deposit reset #{rs['id']}. Negative amounts and loans are allowed."
                              if rs else "OPENING: first-time balances. Existing balances are never overwritten; negatives are not allowed here."))
        if p.errors:
            return await reply(interaction, card=c)
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Import cancelled. Nothing was changed.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return importer.commit(conn, p, admin_id=uid(interaction), note=note, kind=kind,
                                           reset_id=rs["id"] if rs else None, snapshot_id=snap.id if snap else None)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Import refused, nothing was changed: {exc}")
        if rs:
            out = A.Card("♻️ Balances restored", f"Batch #{res['batch_id']} (reset #{rs['id']}) by {actor_label(interaction)}", A.GREEN, kind="IMPORT")
            out.add("Nations / amounts", f"{res['nations']} / {res['rows']}", True)
            out.add("Net value", fmt.value_line(p.valuation), True)
            if res["loans"]:
                out.add("Outstanding loans stored", f"{res['loans']} nation(s) · ${res['loan_total_cents'] / 100:,.2f} (not deposits)")
            out.add("Next step", "Run `/ledger reconcile`, then `/bank unlock` to resume withdrawals.")
        else:
            out = A.Card("Opening balances imported", f"Batch #{res['batch_id']} by {actor_label(interaction)}", A.GREEN, kind="IMPORT")
            out.add("Rows / nations", f"{res['rows']} / {res['nations']}", True)
            out.add("Total", fmt.amounts_with_value(res["totals"], p.valuation))
            if res["loans"]:
                out.add("Outstanding loans stored", f"{res['loans']} nation(s) · ${res['loan_total_cents'] / 100:,.2f} (not deposits)")
        await reply(interaction, card=out)
        await svc.alerts.econ(out)

    RS.attach(svc, freeze, "nation")
    RS.attach(svc, unfreeze, "nation")
    RS.attach(svc, adjust, "nation")
    RS.attach(svc, setnationlimit, "nation")
    RS.attach(svc, review, "nation")

    svc.actions.update(freeze=freeze.callback, unfreeze=unfreeze.callback, review=review.callback,
                       approve=approve.callback, revoke=revoke.callback, records=records.callback)
