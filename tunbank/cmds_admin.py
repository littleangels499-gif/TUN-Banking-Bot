"""Staff commands part 2: corrections, approvals, limits, freezes, imports, exports, settings."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
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
        await reply(interaction, f"Account #{nation_id} frozen." if ok else "No such account.")

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
        await reply(interaction, f"Account #{nation_id} unfrozen.")

    # ------------------------------------------------- pause / resume withdrawals
    @bank.command(name="lock", description="ECON: PAUSE all withdrawals (deposits keep being recorded)")
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
    @bank.command(name="adjust", description="ECON: documented correction of an ACCOUNTING ERROR (not a way to add money)")
    @app_commands.describe(nation="Member: id, name, link or @user", reason="What went wrong", evidence="Ticket, PnW record id, link...",
                           add="Amounts to ADD (needs a 2nd approver)", remove="Amounts to REMOVE (below zero = a debt, needs a 2nd approver)")
    async def adjust(interaction: discord.Interaction, nation: str, reason: str, evidence: str,
                     add: str = "", remove: str = ""):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        if not add.strip() and not remove.strip():
            return await reply(interaction, "Give `add` and/or `remove` amounts.")
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
                c.add("Nation", f"#{nation_id}", True)
                c.add("Change", fmt.amount_lines({r: abs(d) for r, d in deltas.items()}))
                c.add("Reason", reason)
                c.add("Evidence", evidence)
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} posted. After a different Minister "
                                                "approves it, run this same command again.")
        card = A.Card("Confirm ADJUSTMENT", "This corrects an accounting record. It is permanent and audited.", A.ORANGE)
        card.add("Nation", f"#{nation_id}", True)
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
        await svc.alerts.dm(svc.alerts.discord_id_for(nation_id), out)

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

    @bankset.command(name="setaccess", description="Admin: choose which roles may see confidential alliance information")
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
            return await reply(interaction, f"{icons.status('ok')} {member.mention} is already linked to nation [#{nid}]. Nothing to change.")
        conflict = bool(owner or other_nation)
        if conflict:
            lines = []
            if owner:
                lines.append(f"Nation [#{nid}] is already linked to <@{owner}>.")
            if other_nation:
                lines.append(f"{member.mention} is already linked to nation [#{other_nation}].")
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

    @bankset.command(name="config", description="Admin: view or change a bank setting")
    async def config(interaction: discord.Interaction, key: str = "", value: str = ""):
        if not await need(svc, interaction, "ADMIN"):
            return
        if not key:
            with svc.db.read() as conn:
                lines = [f"`{k}` = `{cfg_get(conn, k)}` — {d}" for k, (_, d) in DEFAULTS.items()]
            return await reply(interaction, "\n".join(lines)[:1900])
        if key not in DEFAULTS:
            return await reply(interaction, "Unknown setting. Run `/bankset config` with no options to see them all.")
        if not value:
            with svc.db.read() as conn:
                return await reply(interaction, f"`{key}` = `{cfg_get(conn, key)}`")
        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, key, value, uid(interaction))
                L.audit(conn, uid(interaction), "CONFIG_CHANGED", key, {"value": value})
        await asyncio.to_thread(do)
        await reply(interaction, f"`{key}` set to `{value}`.")

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

    @bank.command(name="transactions", description="ECON: recent withdrawals")
    async def transactions(interaction: discord.Interaction, status: str = ""):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            if status:
                rows = conn.execute("SELECT * FROM transactions WHERE status=? ORDER BY id DESC LIMIT 15", (status.upper(),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 15").fetchall()
        await paginate(interaction, chunk_cards(f"{icons.status('withdraw')} Transactions", [fmt.tx_line(r, True) for r in rows],
                                                empty="No transactions.", per_page=6))

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
                      f"[#{r['sender_id'] if r['direction'] == 'IN' else r['receiver_id']}]", True)
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
                                              snapshot_id=snap.id if snap else None)
            out = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not applied: {exc}")
        await reply(interaction, f"Record #{record_id} resolved ({action.value}).")
        if out.kind == "CREDIT":
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
