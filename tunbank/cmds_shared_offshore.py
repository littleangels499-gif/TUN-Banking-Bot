"""/offshore: the shared-offshore registry and each alliance's own account inside it.

One physical PnW offshore -> many registered alliances -> one TUN-hosted bot doing the accounting and the permissions.
The bot is never invited to another alliance's server: other alliances are just entries in the registry, and may be given a
Discord ROLE in the TUN server that lets its holders see ONLY that alliance's account.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import fmt
from . import ledger as L
from . import money as M
from . import offshore_ledger as OL
from . import perms
from .config import cfg_get
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, chunk_cards, confirm, levels, need, paginate, reply, thinking
from .valuation import value_amounts

log = logging.getLogger("tunbank.shared_offshore")
NOT_ON = "The shared offshore isn't switched on yet. An Admin can turn it on with `/offshore enable`."
ACTIONS = [app_commands.Choice(name="List", value="list"), app_commands.Choice(name="Add / update", value="add"),
           app_commands.Choice(name="Deactivate", value="remove")]


def bal_text(b: dict) -> str:
    return ", ".join(f"{M.LABELS[r]} {(fmt.compact_money if r == 'money' else fmt.compact_number)(v)}" for r, v in b.items()) or "nothing"


def register(off_grp: app_commands.Group, svc: Services):
    uid = lambda i: str(i.user.id)  # noqa: E731

    # ------------------------------------------------------------------ who may see what
    def visible(interaction) -> tuple[bool, set]:
        """(sees_everything, alliance ids this person may see). Overall picture = Admin or the holdings permission.
        Other TUN staff see the host alliance only; a registered alliance's role sees only that alliance."""
        lv = levels(svc, interaction)
        with svc.db.read() as conn:
            regs = OL.alliances(conn)
        if perms.has(lv, "ADMIN") or "FLAG:bank_view_alliance_holdings" in lv:
            return True, {a["alliance_id"] for a in regs}
        ids = set()
        if perms.has(lv, "AUDITOR"):
            ids |= {a["alliance_id"] for a in regs if a["is_host"]}
        mine = {str(r.id) for r in getattr(interaction.user, "roles", [])}
        ids |= {a["alliance_id"] for a in regs if a["role_id"] and a["role_id"] in mine}
        return False, ids

    def find(text: str | None, allowed: set | None = None):
        """An alliance from the registry by id or (case-insensitive) name."""
        with svc.db.read() as conn:
            regs = OL.alliances(conn)
        t = (text or "").strip().lstrip("#")
        hits = [a for a in regs if str(a["alliance_id"]) == t or a["name"].lower() == t.lower()]
        if not hits and t:
            hits = [a for a in regs if t.lower() in a["name"].lower()]
        if allowed is not None:
            hits = [a for a in hits if a["alliance_id"] in allowed]
        return hits[0] if len(hits) == 1 else None

    async def physical():
        o = svc.settings.offshore
        if o is None:
            raise PnWRejected("No offshore bank is configured for this bot.")
        return await svc.pnw.fetch_bank_holdings(o)

    def parse(text: str) -> dict:
        try:
            p = M.parse_amounts(text)
        except M.AmountError as exc:
            raise L.LedgerError(f"I couldn't read that: {exc}\nUse e.g. `money=500m steel=3000 food=50k`.") from exc
        if not p:
            raise L.LedgerError("Tell me what, e.g. `money=500m steel=3000`.")
        return p

    # ------------------------------------------------------------------ status / account / history
    @off_grp.command(name="status", description="The shared offshore: who owns what (you only see what you're allowed to)")
    async def status(interaction: discord.Interaction):
        await thinking(interaction)
        with svc.db.read() as conn:
            on = OL.shared_enabled(conn)
        if not on:
            return await reply(interaction, NOT_ON)
        everything, ids = visible(interaction)
        if not ids:
            return await reply(interaction, "You don't have access to any alliance's offshore account.")
        try:
            phys = await physical()
        except (PnWRejected, PnWUncertain) as exc:
            phys, err = None, str(exc)
        with svc.db.read() as conn:
            summ = OL.summary(conn, phys or {})
        backed = phys is not None and not any(v < 0 for v in summ["unassigned"].values())
        c = A.Card("🏝️ Shared offshore", "", A.GREEN if backed else A.ORANGE)
        if everything:
            c.add("Physical offshore (real PnW balance)", bal_text(phys) if phys is not None else f"unavailable: {err}")
        for o in summ["owners"]:
            if o["alliance_id"] in ids:
                c.add(f"{'🏠' if o['is_host'] else '🤝'} {o['name']} [#{o['alliance_id']}]",
                      "Owns: " + bal_text(o["balances"]) + (f"\nPayouts in progress: {bal_text(o['held'])}" if o["held"] else ""))
        if everything and phys is not None:
            un = summ["unassigned"]
            pos = {r: v for r, v in un.items() if v > 0}
            neg = {r: -v for r, v in un.items() if v < 0}
            c.add("Unassigned (in the bank, owned by nobody yet)", bal_text(pos))
            if neg:
                c.add("⚠️ Shares add up to MORE than the bank holds", bal_text(neg))
            c.add("Reconciliation", "✅ RECONCILED: shares are fully backed" if backed else "⚠️ NOT fully backed. Run `/ledger reconcile`.")
        else:
            c.add("Is my share backed?", "✅ The offshore's real balance covers every registered share." if backed
                  else "⚠️ Not confirmed. Please contact TUN ECON.")
        await reply(interaction, card=c)

    @off_grp.command(name="account", description="One alliance's share of the shared offshore, with recent activity")
    @app_commands.describe(alliance="Alliance id or name (default: yours)")
    async def account(interaction: discord.Interaction, alliance: Optional[str] = None):
        await thinking(interaction)
        with svc.db.read() as conn:
            if not OL.shared_enabled(conn):
                return await reply(interaction, NOT_ON)
        everything, ids = visible(interaction)
        a = find(alliance, ids) if alliance else (find(str(next(iter(ids))), ids) if len(ids) == 1 else None)
        if a is None:
            return await reply(interaction, "Tell me which alliance: " + (", ".join(
                f"{x['name']} [#{x['alliance_id']}]" for x in svc_regs(ids)) or "you have no access to any account."))
        with svc.db.read() as conn:
            bal, held = OL.balances(conn, a["alliance_id"]), OL.held(conn, a["alliance_id"])
            hist = OL.history(conn, a["alliance_id"], 8)
            snap = None
        snap = await svc.prices.get()
        val = value_amounts(bal, snap)
        c = A.Card(f"{'🏠' if a['is_host'] else '🤝'} {a['name']} [#{a['alliance_id']}] · offshore account", "", A.BLUE)
        c.add("Owned", fmt.amounts_with_value(bal, val) if bal else "nothing")
        if held:
            c.add("Payouts in progress", bal_text(held))
        c.add("Recent activity", "\n".join(
            f"`{h['ts'][:10]}` {h['entry_type'].title()} {'+' if h['delta'] > 0 else '−'}{M.fmt_units(h['resource'], abs(h['delta']))} {M.LABELS[h['resource']]}"
            for h in hist) or "none yet")
        await reply(interaction, card=c)

    def svc_regs(ids):
        with svc.db.read() as conn:
            return [a for a in OL.alliances(conn) if a["alliance_id"] in ids]

    @off_grp.command(name="history", description="Every deposit and withdrawal recorded for one alliance's share")
    @app_commands.describe(alliance="Alliance id or name (default: yours)")
    async def history(interaction: discord.Interaction, alliance: Optional[str] = None):
        await thinking(interaction)
        everything, ids = visible(interaction)
        a = find(alliance, ids) if alliance else (find(str(next(iter(ids))), ids) if len(ids) == 1 else None)
        if a is None:
            return await reply(interaction, "Tell me which alliance you mean (and you can only see your own).")
        with svc.db.read() as conn:
            rows = OL.history(conn, a["alliance_id"], 200)
        lines = [f"`{r['ts'][:16].replace('T', ' ')}` **{r['entry_type'].title()}** {'+' if r['delta'] > 0 else '−'}"
                 f"{M.fmt_units(r['resource'], abs(r['delta']))} {M.LABELS[r['resource']]}"
                 + (f" · PnW #{r['pnw_record_id']}" if r["pnw_record_id"] else "") + (f" · {r['note'][:60]}" if r["note"] else "") for r in rows]
        await paginate(interaction, chunk_cards(f"🏝️ {a['name']} · offshore history", lines, per_page=10, empty="Nothing recorded yet."))

    # ------------------------------------------------------------------ registry + setup (Admin)
    @off_grp.command(name="registry", description="Admin: the alliances that share the offshore (list / add / deactivate)")
    @app_commands.describe(action="What to do", alliance="Alliance id or exact name", role="Optional: a role in THIS server whose members may see only this alliance's account",
                           note="Optional note")
    @app_commands.choices(action=ACTIONS)
    async def registry(interaction: discord.Interaction, action: app_commands.Choice[str], alliance: str = "",
                       role: Optional[discord.Role] = None, note: str = ""):
        if not await need(svc, interaction, "ADMIN" if action.value != "list" else "AUDITOR"):
            return
        await thinking(interaction)
        if action.value == "list":
            everything, ids = visible(interaction)
            with svc.db.read() as conn:
                regs = [a for a in OL.alliances(conn, active_only=False) if a["alliance_id"] in ids or everything]
            lines = [f"{'🏠' if a['is_host'] else '🤝'} **{a['name']}** [#{a['alliance_id']}]" + ("" if a["active"] else " · *inactive*")
                     + (f" · role <@&{a['role_id']}>" if a["role_id"] else "") for a in regs]
            return await paginate(interaction, chunk_cards("🏝️ Offshore registry", lines, per_page=10, empty="No alliances registered yet."))
        if not alliance.strip():
            return await reply(interaction, "Tell me which alliance (id or exact name).")
        try:
            if alliance.strip().lstrip("#").isdigit() and find(alliance):
                hits = [{"id": find(alliance)["alliance_id"], "name": find(alliance)["name"]}]
            else:
                hits = await svc.pnw.find_alliance(alliance)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't look that alliance up: {exc}")
        if len(hits) != 1:
            return await reply(interaction, "I couldn't find exactly one alliance for that. Try its id." if not hits
                               else "That matches several alliances: " + ", ".join(f"{h['name']} [#{h['id']}]" for h in hits[:6]))
        aid, name = hits[0]["id"], hits[0]["name"]

        def do():
            from . import configaudit as CA
            with svc.db.tx() as conn:
                if action.value == "add":
                    if not OL.shared_enabled(conn):
                        raise OL.OffshoreError(NOT_ON)
                    host = OL.host_id(conn)
                    OL.register(conn, alliance_id=aid, name=name, actor=uid(interaction), role_id=str(role.id) if role else None,
                                is_host=(aid == host), note=note or None)
                    CA.record(conn, actor=uid(interaction), setting="offshore_registry", previous="—", new="registered",
                              target=f"{name} [#{aid}]", category="CONFIG")
                else:
                    OL.deactivate(conn, alliance_id=aid, actor=uid(interaction))
                    CA.record(conn, actor=uid(interaction), setting="offshore_registry", previous="registered", new="deactivated",
                              target=f"{name} [#{aid}]", category="CONFIG")
        try:
            await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"**{name} [#{aid}]** " + ("is registered in the shared offshore (share starts at nothing; use `/offshore assign` or "
                                                          "`/offshore reassign` to give it funds)." if action.value == "add" else "was deactivated."))

    @off_grp.command(name="enable", description="Admin: switch the offshore to SHARED mode (everything in it starts as this alliance's share)")
    async def enable(interaction: discord.Interaction):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        try:
            phys = await physical()
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't read the offshore bank, so nothing was changed: {exc}")
        with svc.db.read() as conn:
            if OL.shared_enabled(conn):
                return await reply(interaction, "Shared mode is already on.")
        c = A.Card("Switch the offshore to SHARED mode?", "Nothing moves in PnW. Everything the offshore holds right now is recorded as THIS "
                   "alliance's share, so nothing changes for you today. Afterwards you register the other alliances and split it with `/offshore reassign`.",
                   A.ORANGE)
        c.add("Currently in the offshore", bal_text(phys))
        c.add("From now on", "• this alliance can spend only its OWN share of the offshore\n• other alliances' deposits are credited to them\n"
                              "• reconciliation compares the real balance with the sum of all shares (and never edits a share)")
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")
        host_id, host_name = svc.settings.alliance_id, f"Alliance {svc.settings.alliance_id}"
        try:
            info = await svc.pnw.find_alliance(str(host_id))
            host_name = info[0]["name"] if info else host_name
        except (PnWRejected, PnWUncertain):
            pass

        def do():
            with svc.db.tx() as conn:
                res = OL.enable_shared(conn, physical=phys, actor=uid(interaction), host_alliance_id=host_id, host_name=host_name)
                from . import configaudit as CA
                CA.record(conn, actor=uid(interaction), setting="offshore_shared", previous="off", new="on", target="offshore", category="CONFIG")
                return res
        try:
            await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Shared mode is ON. {host_name} [#{host_id}] holds {bal_text(phys)} as its share. "
                                 "Next: `/offshore registry action:Add alliance:<id>` for each other alliance, then `/offshore reassign`.")

    @off_grp.command(name="assign", description="Admin: give an alliance a share of funds that are in the offshore but owned by nobody yet")
    @app_commands.describe(alliance="Alliance id or name", amounts="e.g. money=500m steel=3000", note="Why / source of the funds")
    async def assign(interaction: discord.Interaction, alliance: str, amounts: str, note: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        a = find(alliance)
        if not a:
            return await reply(interaction, "I couldn't find exactly one registered alliance for that.")
        try:
            parsed, phys = parse(amounts), await physical()
        except (L.LedgerError, PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, str(exc))
        with svc.db.read() as conn:
            free = {r: max(0, v) for r, v in OL.unassigned(phys, OL.totals(conn)).items()}
        c = A.Card("Assign these funds?", "Only funds physically in the offshore that nobody owns yet can be assigned.", A.ORANGE)
        c.add("To", f"{a['name']} [#{a['alliance_id']}]", True)
        c.add("Amount", bal_text(parsed), True)
        c.add("Unassigned right now", bal_text(free))
        c.add("Note", note[:300])
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")

        def do():
            with svc.db.tx() as conn:
                OL.assign(conn, alliance_id=a["alliance_id"], amounts=parsed, physical=phys, actor=uid(interaction), note=note)
        try:
            await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Assigned {bal_text(parsed)} to **{a['name']}**.")

    @off_grp.command(name="reassign", description="Admin: move part of one alliance's share to another (the bank itself doesn't change)")
    @app_commands.describe(from_alliance="Alliance giving up the share", to_alliance="Alliance receiving it", amounts="e.g. money=1b steel=500",
                           reason="Why (kept permanently)")
    async def reassign(interaction: discord.Interaction, from_alliance: str, to_alliance: str, amounts: str, reason: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        a, b = find(from_alliance), find(to_alliance)
        if not (a and b):
            return await reply(interaction, "I couldn't find exactly one registered alliance for each of those.")
        try:
            parsed = parse(amounts)
        except L.LedgerError as exc:
            return await reply(interaction, str(exc))
        c = A.Card("Move this share?", "Ownership changes; no PnW transaction happens and the total owned stays the same.", A.ORANGE)
        c.add("From", f"{a['name']} [#{a['alliance_id']}]", True)
        c.add("To", f"{b['name']} [#{b['alliance_id']}]", True)
        c.add("Amount", bal_text(parsed))
        c.add("Reason", reason[:300])
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")

        def do():
            with svc.db.tx() as conn:
                OL.reassign(conn, from_id=a["alliance_id"], to_id=b["alliance_id"], amounts=parsed, actor=uid(interaction), note=reason)
        try:
            await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Moved {bal_text(parsed)} from **{a['name']}** to **{b['name']}**.")

    @off_grp.command(name="release", description="Admin: take funds out of an alliance's share (they become unassigned). A documented correction.")
    @app_commands.describe(alliance="Alliance id or name", amounts="e.g. money=1b", reason="Why (kept permanently)")
    async def release(interaction: discord.Interaction, alliance: str, amounts: str, reason: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        a = find(alliance)
        if not a:
            return await reply(interaction, "I couldn't find exactly one registered alliance for that.")
        try:
            parsed = parse(amounts)
        except L.LedgerError as exc:
            return await reply(interaction, str(exc))
        c = A.Card("Release this from the share?", "The funds stay in the offshore but stop being this alliance's.", A.RED)
        c.add("Alliance", f"{a['name']} [#{a['alliance_id']}]", True)
        c.add("Amount", bal_text(parsed), True)
        c.add("Reason", reason[:300])
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was changed.")

        def do():
            with svc.db.tx() as conn:
                OL.release(conn, alliance_id=a["alliance_id"], amounts=parsed, actor=uid(interaction), note=reason)
        try:
            await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Released {bal_text(parsed)} from **{a['name']}**.")

    # ------------------------------------------------------------------ paying out for an alliance (Admin)
    @off_grp.command(name="send", description="Admin: send funds OUT of the offshore for a registered alliance (reduces that alliance's share)")
    @app_commands.describe(alliance="Whose share the money comes from", destination="Receiving nation id (or alliance id with destination_type)",
                           amounts="e.g. money=500m steel=3000", reason="Why (kept permanently)",
                           destination_type="Is the destination a nation or an alliance bank?")
    @app_commands.choices(destination_type=[app_commands.Choice(name="Nation", value=1), app_commands.Choice(name="Alliance bank", value=2)])
    async def send(interaction: discord.Interaction, alliance: str, destination: str, amounts: str, reason: str,
                   destination_type: Optional[app_commands.Choice[int]] = None):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        off = svc.offshore
        a = find(alliance)
        if not a or off is None:
            return await reply(interaction, "I couldn't find exactly one registered alliance for that." if off else "Offshore isn't configured.")
        d_type = destination_type.value if destination_type else 1
        if not destination.strip().lstrip("#").isdigit():
            return await reply(interaction, "The destination must be a nation id (or an alliance id with destination_type).")
        d_id = int(destination.strip().lstrip("#"))
        try:
            parsed = parse(amounts)
            phys = await physical()
        except (L.LedgerError, PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, str(exc))
        with svc.db.read() as conn:
            free = OL.spendable(conn, a["alliance_id"])
        over = [M.LABELS[r] for r, v in parsed.items() if v > free.get(r, 0) or v > phys.get(r, 0)]
        if over:
            return await reply(interaction, "Not sent. The share (or the real offshore balance) doesn't cover: " + ", ".join(over))
        snap = await svc.prices.get()
        val = value_amounts(parsed, snap)
        c = A.Card("Send from the shared offshore?", "Real money leaves the offshore bank. Only that alliance's share is reduced, and only after PnW "
                   "shows the record.", A.RED)
        c.add("From the share of", f"{a['name']} [#{a['alliance_id']}]", True)
        c.add("To", f"{'Alliance bank' if d_type == 2 else 'Nation'} #{d_id}", True)
        c.add("Amount", fmt.amounts_with_value(parsed, val))
        c.add("Reason", reason[:300])
        if not await confirm(svc, interaction, c):
            return await reply(interaction, "Cancelled. Nothing was sent.")
        try:
            tid, created = await asyncio.to_thread(
                off.create, actor=uid(interaction), amounts=parsed, reason=reason, key=f"offpay-{interaction.id}",
                value_cents=val.total_cents, snapshot_id=snap.id if snap else None, direction="PAYOUT", alliance_id=a["alliance_id"],
                dest=(d_type, d_id))
        except L.LedgerError as exc:
            return await reply(interaction, f"Not sent: {exc}")
        row = off.get(tid)
        if row["mode"] == "AUTO":
            res = await off.execute_auto(tid)
            msg = f"Offshore payout **#{tid}**: {res}"
        else:
            msg = (f"Offshore payout **#{tid}** is prepared. In PnW, send it from the offshore bank with the note `TUN-OFF{tid}`; "
                   "the share is reduced when the bot sees that record.")
        await reply(interaction, msg)
        await svc.alerts.econ(A.Card("🏝️ Offshore payout", f"#{tid} for {a['name']} by <@{uid(interaction)}>", A.BLUE, kind="OFFSHORE"))

    @off_grp.command(name="attribute", description="Admin: say whose share a PnW offshore record belongs to (one the bot couldn't place)")
    @app_commands.describe(record_id="The PnW bank record number", alliance="The alliance it belongs to")
    async def attribute(interaction: discord.Interaction, record_id: int, alliance: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        a = find(alliance)
        if not a:
            return await reply(interaction, "I couldn't find exactly one registered alliance for that.")
        off_id = svc.settings.offshore.alliance_id if svc.settings.offshore else None

        def do():
            from . import bankrec as B
            with svc.db.tx() as conn:
                if not OL.shared_enabled(conn):
                    raise OL.OffshoreError(NOT_ON)
                r = B.get_record(conn, record_id)
                if not r:
                    raise OL.OffshoreError(f"I have no PnW record #{record_id}.")
                n = {"id": r["id"], "amounts": __import__("json").loads(r["amounts_json"])}
                if r["receiver_id"] == off_id:
                    done = OL.credit_record(conn, n, alliance_id=a["alliance_id"], actor=uid(interaction))
                    verb = "credited to"
                elif r["sender_id"] == off_id:
                    short = OL.debit_record(conn, n, alliance_id=a["alliance_id"], actor=uid(interaction))
                    if short:
                        raise OL.OffshoreError("That alliance's share doesn't cover the whole record (" + bal_text(short)
                                               + " missing). Assign or reassign funds first.")
                    done, verb = n["amounts"], "taken from"
                else:
                    raise OL.OffshoreError("That record doesn't involve the offshore bank.")
                if not done:
                    raise OL.OffshoreError("That record was already attributed.")
                conn.execute("UPDATE pnw_records SET classification='OTHER', status='NO_CREDIT' WHERE id=? AND status='AWAITING_REVIEW'", (record_id,))
                L.audit(conn, uid(interaction), "OFFSHORE_ATTRIBUTED", f"pnw:{record_id}", {"alliance_id": a["alliance_id"], "amounts": done})
                return verb, done
        try:
            verb, done = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not done: {exc}")
        await reply(interaction, f"Record #{record_id}: {bal_text(done)} {verb} **{a['name']}**'s share.")
