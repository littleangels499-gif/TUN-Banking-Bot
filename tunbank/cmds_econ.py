"""Staff (ECON) commands for /bank."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from . import alerts as A
from . import exports as X
from . import fmt
from . import icons
from . import importer
from . import ledger as L
from . import limits as LIM
from . import money as M
from . import reconcile as R
from . import records as REC
from .pnw import PnWRejected, PnWUncertain
from . import resolve as RS
from .banks import live_holdings
from .buttons import ActionView
from .ui import (RefreshView, Services, actor_label, confirm, nation_arg, need, post_outcomes, reply,
                 role_ids, thinking, xlsx_file)
from .valuation import value_amounts

log = logging.getLogger("tunbank.econ")


def parse_or_msg(text):
    try:
        return M.parse_amounts(text), None
    except M.AmountError as exc:
        return None, f"I couldn't read those amounts: {exc}"


def register(bank: app_commands.Group, svc: Services):
    # ---------------------------------------------------------- /bank holdings
    async def build_vault():
        snap = await svc.prices.get()
        warn = None
        per_bank = None
        try:
            live, per_bank = await live_holdings(svc.pnw, svc.settings)
        except (PnWRejected, PnWUncertain) as exc:
            live = None
            warn = f"{icons.status('warn')} Could not read the live PnW bank ({exc}). Showing ledger-only figures."

        def load():
            with svc.db.read() as conn:
                pos = R.bank_position(conn, live)
                tax_rows = conn.execute("SELECT amounts_json FROM tax_records").fetchall()
                ign = conn.execute("SELECT amounts_json FROM pnw_records WHERE classification="
                                   "'ALLIANCE_DONATION'").fetchall()
                st = L.integrity_state(conn)
                last = L.get_state(conn, "last_scan_ok")
                lastrec = conn.execute("SELECT started_at, result FROM reconciliation_runs ORDER BY id DESC LIMIT 1").fetchone()
                return pos, tax_rows, ign, st, last, lastrec
        pos, tax_rows, ign, st, last, lastrec = await asyncio.to_thread(load)
        import json
        tax_total, ign_total = {}, {}
        for r in tax_rows:
            tax_total = M.add(tax_total, json.loads(r["amounts_json"]))
        for r in ign:
            ign_total = M.add(ign_total, json.loads(r["amounts_json"]))
        state_icon = icons.status("ok") if st["state"] == "NORMAL" else (icons.status("lock") if st["emergency_lock"] else icons.status("warn"))
        c = A.Card(f"{icons.status('bank')} Alliance vault", warn or "Real PnW bank compared with what members own.",
                   A.RED if st["emergency_lock"] else A.BLUE)
        if pos["bank"] is not None and per_bank and len(per_bank) > 1:
            c.add(f"{icons.status('bank')} Real PnW banks · combined", fmt.amounts_with_value(pos["bank"], value_amounts(pos["bank"], snap)))
            for name, held in per_bank.items():
                c.add(f"{'🏦' if name == 'main' else '🏝️'} {name.title()} bank", fmt.amounts_with_value(held, value_amounts(held, snap)), True)
        elif pos["bank"] is not None:
            c.add(f"{icons.status('bank')} Real PnW bank", fmt.amounts_with_value(pos["bank"], value_amounts(pos["bank"], snap)))
        c.add(f"{icons.status('money')} Member AVAILABLE", fmt.amounts_with_value(pos["available"], value_amounts(pos["available"], snap)), True)
        c.add(f"{icons.status('lock')} Member LOCKED", fmt.amounts_with_value(pos["locked"], value_amounts(pos["locked"], snap)), True)
        c.add(f"{icons.status('member')} Total member-held", fmt.amounts_with_value(pos["member_total"], value_amounts(pos["member_total"], snap)))
        if pos["in_flight_out"]:
            c.add(f"{icons.status('wait')} In-flight transfers · sent, not yet booked", fmt.amount_lines(pos["in_flight_out"]))
        if pos["alliance_owned"] is not None:
            c.add(f"{icons.status('alliance')} ALLIANCE-OWNED · bank − member-held",
                  fmt.amounts_with_value(pos["alliance_owned"], value_amounts(
                      {k: v for k, v in pos["alliance_owned"].items() if v > 0}, snap)))
        c.add(f"{icons.status('tax')} Taxes collected · alliance-owned", fmt.amounts_with_value(tax_total, value_amounts(tax_total, snap)), True)
        c.add(f"{icons.status('alliance')} #ignore donations", fmt.amounts_with_value(ign_total, value_amounts(ign_total, snap)), True)
        c.add("Outstanding grants / loans", "Not enabled yet (Phase 3).", True)
        c.add(f"{state_icon} Integrity", f"{st['state'].replace('_', ' ').title()} · {st['open_events']} open event(s)", True)
        c.add(f"{icons.status('refresh')} Last PnW sync", last or "never", True)
        c.add(f"{icons.status('audit')} Last reconciliation", f"{lastrec['started_at']} → {lastrec['result']}" if lastrec else "never", True)
        return c

    @bank.command(name="holdings", description="ECON: the full vault position (real bank vs member money)")
    async def holdings(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        card = await build_vault()

        async def b_chart(i):
            await svc.actions["chart_vault"](i)

        async def b_recon(i):
            await svc.actions["reconcile"](i)

        async def b_review(i):
            await svc.actions["review"](i)

        async def b_export(i):
            await svc.actions["records"](i, app_commands.Choice(name="balances", value="balances"))
        await reply(interaction, card=card, view=ActionView(interaction.user.id, [
            ("Chart", "📊", "secondary", b_chart), ("Reconcile now", "🔎", "primary", b_recon),
            ("Review queue", "⚠️", "secondary", b_review), ("Export balances", "📤", "secondary", b_export)],
            refresh=build_vault))

    # ------------------------------------------------------- /bank scandeposits
    @bank.command(name="scandeposits", description="ECON: scan PnW bank records now")
    async def scandeposits(interaction: discord.Interaction):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        res = await svc.scanner.scan()
        if not res.ok:
            return await reply(interaction, f"Scan failed: {res.error}")
        await post_outcomes(svc, res.outcomes)
        await svc.alerts.flush_events()
        kinds = {}
        for o in res.outcomes:
            kinds[o.kind] = kinds.get(o.kind, 0) + 1
        txt = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())) or "nothing new"
        extra = ("\n**First scan:** existing PnW history was stored as evidence but NOT credited "
                 "(opening balances cover it). Use `/bank review` to credit anything missing."
                 if res.baseline else "")
        await reply(interaction, f"Scanned {res.seen} PnW record(s): {txt}.{extra}")

    # ------------------------------------------------------- /bank reconcile
    async def do_reconcile(interaction_actor: str):
        snap = await svc.prices.get()
        try:
            live, per_bank = await live_holdings(svc.pnw, svc.settings)
        except (PnWRejected, PnWUncertain):
            live, per_bank = None, None

        def run():
            with svc.db.tx() as conn:
                return R.run_checks(conn, holdings=live, snapshot_id=snap.id if snap else None,
                                    triggered_by=interaction_actor, per_bank=per_bank)
        result = await asyncio.to_thread(run)
        await svc.alerts.flush_events()
        return result

    def recon_card(result) -> A.Card:
        ok = result["result"] == "OK"
        c = A.Card("Reconciliation " + result["result"], "", A.GREEN if ok else (A.ORANGE if result["result"] == "WARNING" else A.RED))
        if ok:
            c.description = "Ledger, PnW records and the real bank all agree. Hash chains intact."
        for f in result["findings"][:10]:
            c.add(f"{f['severity']}: {f['kind']}", f["message"])
        pos = result["position"]
        if pos["bank"] is not None:
            c.add("Real bank vs member-held", f"Bank cash {fmt.dollars(pos['bank'].get('money', 0))} vs "
                  f"members {fmt.dollars(pos['member_total'].get('money', 0))}")
        c.add("Run", f"#{result['run_id']}", True)
        return c

    @bank.command(name="reconcile", description="ECON: run a full reconciliation now")
    async def reconcile(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        result = await do_reconcile(f"discord:{interaction.user.id}")
        await reply(interaction, card=recon_card(result))

    svc.do_reconcile = do_reconcile  # used by the background loop

    # -------------------------------------------------- /bank reserve /release
    @bank.command(name="reserve", description="ECON: move a member's AVAILABLE funds to LOCKED (no PnW transfer)")
    @app_commands.describe(nation="Member: id, name, link or @user", amounts="e.g. money=500m", lock_type="e.g. WARCHEST",
                           reason="Why")
    async def reserve(interaction: discord.Interaction, nation: str, amounts: str, lock_type: str, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        parsed, err = parse_or_msg(amounts)
        if err:
            return await reply(interaction, err)
        snap = await svc.prices.get()
        val = value_amounts(parsed, snap)
        with svc.db.read() as conn:
            m = L.get_member(conn, nation_id)
            free = L.spendable(conn, nation_id) if m else {}
            before = L.snapshot_accounts(conn, nation_id)
        if not m:
            return await reply(interaction, "That nation has no TUN Bank account.")
        card = A.Card("Confirm: RESERVE funds", "Moves AVAILABLE → LOCKED. Nothing is sent in Politics & War.", A.ORANGE)
        card.add("Member", f"{m['nation_name'] or ''} [#{nation_id}]", True)
        card.add("Lock type", lock_type.upper()[:40], True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        card.add("Reason", reason)
        card.add("Available before", fmt.amount_lines(before["available"]), True)
        card.add("Available after", fmt.amount_lines(M.sub(before["available"], parsed)), True)
        if any(free.get(r, 0) < a for r, a in parsed.items()):
            return await reply(interaction, "The member doesn't have that much spendable AVAILABLE balance.", card=card)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return L.create_lock(conn, nation_id=nation_id, amounts=parsed, lock_type=lock_type.upper()[:40],
                                         reason=reason, actor=str(interaction.user.id),
                                         snapshot_id=snap.id if snap else None)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not reserved: {exc}")
        out = A.lock_card("LOCK", nation_id, parsed, val, reason, res["lock_id"], res, actor_label(interaction))
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
        await svc.alerts.dm(m["discord_id"], out)

    @bank.command(name="release", description="ECON: move LOCKED funds back to AVAILABLE")
    @app_commands.describe(lock_id="Lock ID", amounts="Leave empty to release everything left", reason="Why")
    async def release(interaction: discord.Interaction, lock_id: int, reason: str, amounts: str = ""):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        parsed = None
        if amounts.strip():
            parsed, err = parse_or_msg(amounts)
            if err:
                return await reply(interaction, err)
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            lk = conn.execute("SELECT * FROM locks WHERE id=?", (lock_id,)).fetchone()
            if not lk:
                return await reply(interaction, f"Lock #{lock_id} does not exist.")
            free = L.lock_spendable(conn, lock_id, lk["nation_id"])
            m = L.get_member(conn, lk["nation_id"])
        to_release = parsed or free
        val = value_amounts(to_release, snap)
        card = A.Card("Confirm: RELEASE funds", "Moves LOCKED → AVAILABLE. Nothing is sent in Politics & War.", A.ORANGE)
        card.add("Member", f"[#{lk['nation_id']}]", True)
        card.add("Lock", f"#{lock_id} ({lk['lock_type']})", True)
        card.add("Amount", fmt.amounts_with_value(to_release, val))
        card.add("Reason", reason)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled.")
        try:
            def do():
                with svc.db.tx() as conn:
                    return L.release_lock(conn, lock_id=lock_id, amounts=parsed, reason=reason,
                                          actor=str(interaction.user.id), snapshot_id=snap.id if snap else None)
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not released: {exc}")
        out = A.lock_card("RELEASE", lk["nation_id"], to_release, val, reason, lock_id, res, actor_label(interaction))
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
        if m:
            await svc.alerts.dm(m["discord_id"], out)

    # -------------------------------------------------------------- /bank withdraw
    SOURCES = [app_commands.Choice(name="ALLIANCE (alliance-owned funds)", value="ALLIANCE"),
               app_commands.Choice(name="MEMBER_AVAILABLE (a member's available deposit)", value="MEMBER_AVAILABLE"),
               app_commands.Choice(name="MEMBER_LOCKED (a member's locked funds, needs a lock id)", value="MEMBER_LOCKED")]

    @bank.command(name="withdraw", description="ECON: send resources from the alliance bank (you choose the funding source)")
    @app_commands.describe(source="Which money is being spent", destination="Receiving nation: id, name, link or @user",
                           amounts="e.g. money=10m coal=5000", reason="Why",
                           member="Whose deposit is spent (MEMBER_* sources)", lock_id="Required for MEMBER_LOCKED",
                           note="Note shown in PnW")
    @app_commands.choices(source=SOURCES)
    async def withdraw(interaction: discord.Interaction, source: app_commands.Choice[str], destination: str,
                       amounts: str, reason: str, member: str = "", lock_id: int = 0, note: str = ""):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        src = source.value
        destination_nation_id = await nation_arg(svc, interaction, destination)
        if destination_nation_id is None:
            return
        member_nation_id = 0
        if member.strip():
            member_nation_id = await nation_arg(svc, interaction, member)
            if member_nation_id is None:
                return
        parsed, err = parse_or_msg(amounts)
        if err:
            return await reply(interaction, err)
        if src != "ALLIANCE" and not member_nation_id:
            return await reply(interaction, "Fill in `member` (whose deposit is spent) for a member-funded withdrawal.")
        if src == "MEMBER_LOCKED":
            with svc.db.read() as conn:
                from .config import cfg_bool
                if not cfg_bool(conn, "econ_locked_withdraw_enabled"):
                    return await reply(interaction, "Withdrawing from LOCKED funds is disabled. An Administrator can enable it.")
            if not lock_id:
                return await reply(interaction, "Give the `lock_id` to spend from.")
        try:
            snap, val, alliance_free = await svc.wd.prepare(funding_source=src, amounts=parsed)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"Cannot read the live PnW bank, so nothing can be sent: {exc}")

        from .config import cfg_int
        with svc.db.read() as conn:
            threshold = cfg_int(conn, "approval_threshold_value") * 100
        # Unknown value (missing prices) on a configured threshold is treated as "needs approval".
        needs_approval = bool(threshold) and (val.total_cents is None or val.total_cents > threshold)
        approver = None
        approval_id = None
        if needs_approval:
            payload = {"src": src, "dest": destination_nation_id, "amounts": parsed,
                       "member": member_nation_id, "lock": lock_id}
            approval_id, approver, msg = await asyncio.to_thread(
                find_or_request_approval, str(interaction.user.id), payload, reason)
            if approver is None:
                c = A.Card("Approval needed",
                           f"Withdrawal worth {fmt.dollars(val.total_cents)} needs a SECOND staff member.",
                           A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by {actor_label(interaction)}")
                c.add("Amount", fmt.amounts_with_value(parsed, val))
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, msg)
        card = A.Card("Confirm ECON withdrawal", "Nothing is sent until you press Confirm.", A.ORANGE)
        label = {"ALLIANCE": "ALLIANCE-OWNED funds", "MEMBER_AVAILABLE": f"Member #{member_nation_id} AVAILABLE deposit",
                 "MEMBER_LOCKED": f"Member #{member_nation_id} LOCKED funds (lock #{lock_id})"}[src]
        card.add("FUNDING SOURCE (deducted from)", f"**{label}**")
        card.add("Destination nation", f"[#{destination_nation_id}]", True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        card.add("Reason", reason)
        if approver:
            card.add("Second approver", f"<@{approver}>", True)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")
        res = await svc.wd.request(
            tx_type="WITHDRAW_ECON", funding_source=src, member_nation_id=member_nation_id or None,
            lock_id=lock_id or None, dest_nation_id=destination_nation_id, amounts=parsed,
            actor=str(interaction.user.id), note=note or "TUN Bank", reason=reason,
            idempotency_key=f"econ-{interaction.id}", actor_role_ids=role_ids(interaction), approver=approver)
        if approval_id and res.status != "BLOCKED":
            def done():
                with svc.db.tx() as conn:
                    L.finish_approval(conn, approval_id, "EXECUTED", {"tx_id": res.tx_id, "status": res.status})
            await asyncio.to_thread(done)
        out = A.withdrawal_card(res, actor_label=actor_label(interaction), dest_nation_id=destination_nation_id,
                                source_label=label, amounts=parsed, note=note)
        await reply(interaction, card=out)
        await svc.alerts.econ(out)
        await svc.alerts.flush_events()
        if member_nation_id:
            await svc.alerts.dm(svc.alerts.discord_id_for(member_nation_id), out)

    def find_or_request_approval(requester: str, payload: dict, reason: str):
        """Return (approval_id, approver_id_or_None, message_for_requester)."""
        from .util import jdump, parse_iso, utcnow
        with svc.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE kind='ECON_WITHDRAW' AND status='PENDING' "
                "AND requested_by=? ORDER BY id DESC", (requester,)).fetchall()
            for ap in rows:
                if ap["payload_json"] != jdump(payload):
                    continue
                if parse_iso(ap["expires_at"]) < utcnow():
                    conn.execute("UPDATE approval_requests SET status='EXPIRED' WHERE id=?", (ap["id"],))
                    continue
                if ap["approved_by"]:
                    return ap["id"], ap["approved_by"], ""
                return ap["id"], None, (f"Approval request #{ap['id']} is still waiting for a second staff "
                                        "member. Run this same command again after they approve.")
            ap_id = L.create_approval(conn, "ECON_WITHDRAW", payload, requester, reason)
            return ap_id, None, (f"This transfer needs a second approver. Request #{ap_id} was posted to the "
                                 "ECON log. After they approve, run this same command again.")

    svc.actions.update(reserve=reserve.callback, reconcile=reconcile.callback, holdings=holdings.callback)
    RS.attach(svc, reserve, "nation")
    RS.attach(svc, withdraw, "destination", "member")
    _ = (LIM, REC, importer, X, xlsx_file)
