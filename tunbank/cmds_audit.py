"""/tax, /audit and /ledger commands (staff only)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import exports as X
from . import fmt
from . import icons
from . import ledger as L
from . import money as M
from . import resolve as RS
from . import taxutil as TX
from .pnw import PnWRejected, PnWUncertain
from .buttons import ActionView, open_form
from .ui import Services, RefreshView, chunk_cards, nation_arg, need, paginate, post_outcomes, reply, thinking, xlsx_file
from .valuation import value_amounts

log = logging.getLogger("tunbank.audit")


PERIOD_CHOICES = [app_commands.Choice(name="Last 7 days", value="7d"), app_commands.Choice(name="Last 30 days", value="30d"),
                  app_commands.Choice(name="Last 90 days", value="90d"), app_commands.Choice(name="All time", value="all")]
EXEMPT_ACTIONS = [app_commands.Choice(name="list (who is exempt)", value="list"),
                  app_commands.Choice(name="add (mark a nation exempt)", value="add"),
                  app_commands.Choice(name="remove (end an exemption)", value="remove")]


def register_tax(tax: app_commands.Group, svc: Services):
    def pick(p, default="30d"):
        return p.value if p is not None else default

    def choice(value):
        return next(c for c in PERIOD_CHOICES if c.value == value)

    @tax.command(name="sync", description="ECON: re-read PnW bank records now (tax collections are recorded automatically)")
    async def sync(interaction: discord.Interaction):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        res = await svc.scanner.scan()
        if not res.ok:
            return await reply(interaction, f"Sync failed: {res.error}")
        await post_outcomes(svc, res.outcomes)
        await reply(interaction, f"Synced {res.seen} PnW record(s); "
                                 f"{sum(1 for o in res.outcomes if o.kind in ('TAX',))} new tax record(s).")

    # ------------------------------------------------------------ dashboard
    @tax.command(name="dashboard", description="Confidential: taxes collected, top payers and brackets for a period")
    @app_commands.describe(period="Which period (default: last 30 days)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def dashboard(interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        per = pick(period)
        snap = await svc.prices.get()

        def load():
            with svc.db.read() as conn:
                return TX.rows(conn, per), TX.active_exemptions(conn), TX.stored_brackets(conn)
        rs, exempt, brackets_known = await asyncio.to_thread(load)
        grouped = TX.by_nation(rs)
        total = {}
        for g in grouped.values():
            total = M.add(total, g["amounts"])
        ranked = sorted(grouped.items(), key=lambda kv: -(value_amounts(kv[1]["amounts"], snap).total_cents or 0))[:10]
        c = A.Card(f"{icons.status('tax')} Tax dashboard · {TX.PERIOD_LABEL[per]}",
                   "Taxes are alliance-owned. Members never see this in their own dashboard.", A.BLUE)
        c.add("Total collected", fmt.amounts_with_value(total, value_amounts(total, snap)))
        c.add("Records", str(len(rs)), True)
        c.add("Paying nations", str(len(grouped)), True)
        c.add("Exempt (TUN policy)", str(len([n for n in grouped if n in exempt])) + " paying", True)
        c.add("Top payers · by Current Market Value", "\n".join(
            f"**{i + 1}.** [#{n}] {fmt.dollars(value_amounts(g['amounts'], snap).total_cents)}"
            + ("  🧾 exempt" if n in exempt else "") for i, (n, g) in enumerate(ranked)) or "_None yet_")
        by_bracket: dict = {}
        for n, g in grouped.items():
            for b in g["brackets"]:
                by_bracket.setdefault(b, set()).add(n)
        if by_bracket:
            c.add("Brackets seen", "\n".join(
                f"**#{b}** {(brackets_known.get(b) or {}).get('bracket_name') or ''} · {len(ns)} nation(s)"
                for b, ns in sorted(by_bracket.items())))

        async def b_period(value):
            async def go(i):
                await dashboard.callback(i, choice(value))
            return go

        async def b_export(i):
            await export.callback(i, choice(per))

        async def b_chart(i):
            await svc.actions["chart_tax"](i, 30)
        actions = [("7 days", "📅", "secondary", await b_period("7d")), ("30 days", "📅", "secondary", await b_period("30d")),
                   ("90 days", "📅", "secondary", await b_period("90d")), ("All time", "♾️", "secondary", await b_period("all")),
                   ("Export", "📤", "primary", b_export), ("Chart", "📊", "secondary", b_chart)]
        await reply(interaction, card=c, view=ActionView(interaction.user.id, actions))

    # --------------------------------------------------------------- report
    @tax.command(name="report", description="ECON: taxes paid by one nation (or all) for a period")
    @app_commands.describe(nation="Leave empty for everyone", period="Which period (default: last 30 days)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def report(interaction: discord.Interaction, nation: str = "", period: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        nation_id = 0
        if nation.strip():
            nation_id = await nation_arg(svc, interaction, nation)
            if nation_id is None:
                return
        per = pick(period)
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            rs = TX.rows(conn, per, nation_id or None)
            lab = RS.label(conn, nation_id) if nation_id else "everyone"
        total = {}
        for r in rs:
            total = M.add(total, r["amounts"])
        c = A.Card(f"{icons.status('tax')} Tax report · {lab} · {TX.PERIOD_LABEL[per]}", color=A.BLUE)
        c.add("Total", fmt.amounts_with_value(total, value_amounts(total, snap)))
        c.add("Records", str(len(rs)), True)
        c.add("PnW record references", ", ".join(f"#{r['pnw_record_id']}" for r in rs[-15:]) or "—")
        await reply(interaction, card=c)

    @tax.command(name="paid", description="ECON: taxes paid by one nation (same as /tax report)")
    async def paid(interaction: discord.Interaction, nation: str, period: Optional[app_commands.Choice[str]] = None):
        await report.callback(interaction, nation, period)

    # ---------------------------------------------------------------- turns
    @tax.command(name="turns", description="Confidential: tax collected in each recent 2-hour turn (totals only)")
    async def turns(interaction: discord.Interaction):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM tax_turns ORDER BY turn_key DESC LIMIT 48")]
        lines = []
        for r in rows:
            totals = json.loads(r["totals_json"])
            v = value_amounts(totals, snap)
            lines.append(f"**{r['turn_key']}:00 UTC** · {fmt.dollars(v.total_cents)}\n-# 💵 {fmt.compact_number(totals.get('money', 0))}"
                         + (" · " + fmt.short_amounts({k: x for k, x in totals.items() if k != 'money'}) if len(totals) > 1 else ""))
        await paginate(interaction, chunk_cards(f"{icons.status('tax')} Tax by turn", lines, per_page=8,
                                                empty="No tax turns recorded yet.",
                                                intro="Totals only. Member details are in `/tax report` and `/tax export`."))

    # -------------------------------------------------------------- profile
    @tax.command(name="profile", description="ECON: one nation's tax bracket, payments and exemption status")
    @app_commands.describe(nation="Member: id, name, link or @user")
    async def profile(interaction: discord.Interaction, nation: str):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        snap = await svc.prices.get()

        def load():
            with svc.db.read() as conn:
                allrs = TX.rows(conn, "all", nid)
                return (allrs, TX.rows(conn, "30d", nid), TX.active_exemptions(conn).get(nid), TX.stored_brackets(conn),
                        RS.label(conn, nid), L.get_member(conn, nid))
        allrs, r30, exempt_reason, known, lab, m = await asyncio.to_thread(load)
        c = A.Card(f"{icons.status('tax')} Tax profile · {lab}", color=A.ORANGE if exempt_reason else A.BLUE)
        if m and m["discord_id"]:
            c.description = f"{icons.status('member')} <@{m['discord_id']}>"
        latest_bracket = next((r["tax_id"] for r in reversed(allrs) if r["tax_id"]), None)
        if latest_bracket:
            b = known.get(latest_bracket)
            text = f"**#{latest_bracket}**"
            if b:
                text += f" {b.get('bracket_name') or ''} · cash {TX.rate_text(b.get('tax_rate'))} · resources {TX.rate_text(b.get('resource_tax_rate'))}"
            else:
                text += "\n-# Run `/tax brackets` to load the bracket's rates from PnW."
            c.add("Bracket · from the latest tax record", text)
        else:
            c.add("Bracket", "_No tax records for this nation yet._")
        tot30, totall = {}, {}
        for r in r30:
            tot30 = M.add(tot30, r["amounts"])
        for r in allrs:
            totall = M.add(totall, r["amounts"])
        c.add("Paid · last 30 days", fmt.amounts_with_value(tot30, value_amounts(tot30, snap)))
        c.add("Paid · all time", fmt.amounts_with_value(totall, value_amounts(totall, snap)), True)
        c.add("Records", f"{len(r30)} in 30 days · {len(allrs)} total", True)
        c.add("Exemption (TUN policy)", f"{icons.status('ok')} Exempt: {exempt_reason}\n-# This only marks them in reports; PnW still collects tax as set in-game." if exempt_reason else "Not exempt")
        recent = "\n".join(f"`{r['date']}` {fmt.short_amounts(r['amounts'])} · PnW #{r['pnw_record_id']}" for r in allrs[-5:][::-1])
        c.add("Latest payments", recent or "—")

        async def b_exempt(i):
            if not await need(svc, i, "MINISTER"):
                return
            if exempt_reason:
                async def done(i2, why):
                    await exemptions.callback(i2, EXEMPT_ACTIONS[2], str(nid), why, 0)
                await open_form(i, "End this exemption", [dict(label="Why?", max=200)], done)
            else:
                async def done(i2, why, days):
                    await exemptions.callback(i2, EXEMPT_ACTIONS[1], str(nid), why, int(days) if days.strip().isdigit() else 0)
                await open_form(i, "Mark exempt (TUN policy)", [dict(label="Reason", max=200),
                                                               dict(label="For how many days? (blank = until removed)", required=False, max=4)], done)

        async def b_export(i):
            await export.callback(i, choice("all"))
        await reply(interaction, card=c, view=ActionView(interaction.user.id, [
            ("End exemption" if exempt_reason else "Mark exempt…", "🧾", "danger" if exempt_reason else "secondary", b_exempt),
            ("Export", "📤", "secondary", b_export)]))

    # ------------------------------------------------------------- brackets
    @tax.command(name="brackets", description="ECON: the alliance's tax brackets, loaded live from Politics & War")
    async def brackets(interaction: discord.Interaction):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        warn = None
        try:
            data = await svc.pnw.fetch_tax_brackets()

            def save():
                with svc.db.tx() as conn:
                    TX.save_brackets(conn, data)
            await asyncio.to_thread(save)
        except (PnWRejected, PnWUncertain) as exc:
            warn = f"{icons.status('warn')} Could not load brackets from PnW right now: {exc}"

        def load():
            with svc.db.read() as conn:
                rs = TX.rows(conn, "30d")
                return TX.stored_brackets(conn), TX.by_nation(rs)
        known, grouped = await asyncio.to_thread(load)
        payers: dict = {}
        for n, g in grouped.items():
            for b in g["brackets"]:
                payers.setdefault(b, set()).add(n)
        c = A.Card(f"{icons.status('tax')} Tax brackets", warn or "Exactly as Politics & War reports them. Rates are never edited here.",
                   A.ORANGE if warn else A.BLUE)
        if not known:
            c.add("Brackets", "_None loaded yet._")
        for bid, b in list(known.items())[:20]:
            extra = {k: v for k, v in b.items() if k not in ("id", "bracket_name", "tax_rate", "resource_tax_rate", "_synced_at") and v not in (None, "")}
            text = f"cash {TX.rate_text(b.get('tax_rate'))} · resources {TX.rate_text(b.get('resource_tax_rate'))}"
            text += f"\n-# {len(payers.get(bid, ()))} nation(s) paid in the last 30 days"
            if extra:
                text += " · " + ", ".join(f"{k}: {v}" for k, v in list(extra.items())[:3])
            c.add(f"#{bid} {b.get('bracket_name') or ''}".strip(), text, True)
        c.footer = "Synced from PnW · TUN Bank"
        await reply(interaction, card=c, view=ActionView(interaction.user.id, [], refresh=lambda: _rebuild_brackets()))

    async def _rebuild_brackets():
        try:
            data = await svc.pnw.fetch_tax_brackets()

            def save():
                with svc.db.tx() as conn:
                    TX.save_brackets(conn, data)
            await asyncio.to_thread(save)
        except (PnWRejected, PnWUncertain):
            pass
        with svc.db.read() as conn:
            known = TX.stored_brackets(conn)
        c = A.Card(f"{icons.status('tax')} Tax brackets", "Refreshed from Politics & War.", A.BLUE)
        for bid, b in list(known.items())[:20]:
            c.add(f"#{bid} {b.get('bracket_name') or ''}".strip(),
                  f"cash {TX.rate_text(b.get('tax_rate'))} · resources {TX.rate_text(b.get('resource_tax_rate'))}", True)
        return c

    # ---------------------------------------------------------- exemptions
    @tax.command(name="exemptions", description="ECON: track TUN tax exemptions (does not change PnW taxation)")
    @app_commands.describe(action="What to do", nation="For add/remove", reason="Required for add/remove",
                           days="Optional: exemption ends after this many days")
    @app_commands.choices(action=EXEMPT_ACTIONS)
    async def exemptions(interaction: discord.Interaction, action: app_commands.Choice[str], nation: str = "",
                         reason: str = "", days: int = 0):
        level = "FLAG:bank_view_tax" if action.value == "list" else "MINISTER"
        if not await need(svc, interaction, level):
            return
        await thinking(interaction)
        if action.value == "list":
            with svc.db.read() as conn:
                active = conn.execute("SELECT * FROM tax_exemptions WHERE active=1 ORDER BY id DESC").fetchall()
                done = conn.execute("SELECT * FROM tax_exemptions WHERE active=0 ORDER BY id DESC LIMIT 10").fetchall()
                labels = {r["nation_id"]: RS.label(conn, r["nation_id"]) for r in list(active) + list(done)}
            lines = [f"{icons.status('tax')} **{labels[r['nation_id']]}**\n{r['reason']}\n-# set by <@{r['set_by']}> "
                     f"{r['set_at'][:10]}" + (f" · ends {r['expires_at'][:10]}" if r["expires_at"] else " · until removed")
                     for r in active]
            lines += [f"~~{labels[r['nation_id']]}~~ ended {r['removed_at'][:10]}\n-# {r['removal_note']}" for r in done]
            return await paginate(interaction, chunk_cards(f"{icons.status('tax')} Tax exemptions (TUN policy)", lines,
                                                           empty="No exemptions recorded.", per_page=6,
                                                           intro="Bookkeeping only: PnW taxation is not changed."))
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        actor = str(interaction.user.id)

        def do():
            with svc.db.tx() as conn:
                if action.value == "add":
                    TX.add_exemption(conn, nid, reason, actor, days or None)
                    return "added"
                return "removed" if TX.remove_exemption(conn, nid, reason, actor) else "none"
        try:
            outcome = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, f"Not changed: {exc}")
        if outcome == "none":
            return await reply(interaction, "That nation has no active exemption.")
        card = A.Card(f"{icons.status('tax')} Tax exemption {outcome}", f"[#{nid}] · by <@{actor}>", A.ORANGE, kind="TAX")
        card.add("Reason", reason)
        card.add("Remember", "This is TUN bookkeeping only. PnW still collects tax as set in-game.")
        await reply(interaction, card=card)
        await svc.alerts.econ(card)

    # --------------------------------------------------------------- export
    @tax.command(name="export", description="Confidential: Excel workbook of tax collections, per member and per record")
    @app_commands.describe(period="Which period (default: all time)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def export(interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "FLAG:bank_view_tax"):
            return
        await thinking(interaction)
        per = pick(period, "all")
        snap = await svc.prices.get()

        def do():
            with svc.db.read() as conn:
                data = X.build(conn, "tax", snap, per)
            with svc.db.tx() as conn:
                L.audit(conn, str(interaction.user.id), "EXPORT", "tax", {"period": per})
            return data
        data, name = await asyncio.to_thread(do)
        await reply(interaction, f"{icons.status('tax')} Tax export ready ({TX.PERIOD_LABEL[per]}). Values use prices as of "
                                 f"{snap.fetched_at if snap else 'UNAVAILABLE'}.", file=xlsx_file(data, name))

    RS.attach(svc, report, "nation")
    RS.attach(svc, paid, "nation")
    RS.attach(svc, profile, "nation")
    RS.attach(svc, exemptions, "nation")
    svc.actions["tax_export"] = export.callback


def register_audit(audit: app_commands.Group, ledger_grp: app_commands.Group, svc: Services):
    @audit.command(name="transactions", description="Recent withdrawals with status and PnW references")
    async def transactions(interaction: discord.Interaction, status: str = ""):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            q = "SELECT * FROM transactions " + ("WHERE status=? " if status else "") + "ORDER BY id DESC LIMIT 15"
            rows = conn.execute(q, (status.upper(),) if status else ()).fetchall()
        await paginate(interaction, chunk_cards(f"{icons.status('withdraw')} Transactions", [fmt.tx_line(r, True) for r in rows],
                                                empty="No transactions.", per_page=6))

    @audit.command(name="stafflog", description="What staff members did (audit trail)")
    async def stafflog(interaction: discord.Interaction, user: Optional[discord.User] = None):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            if user:
                rows = conn.execute("SELECT * FROM audit_log WHERE actor=? ORDER BY id DESC LIMIT 20", (str(user.id),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM audit_log WHERE actor NOT LIKE 'system%' ORDER BY id DESC LIMIT 20").fetchall()
        lines = [f"`{r['ts'][5:16].replace('T', ' ')}` {'<@' + r['actor'] + '>' if r['actor'].isdigit() else r['actor']} · "
                 f"**{r['action'].replace('_', ' ').title()}** {r['target'] or ''}" for r in rows]
        await paginate(interaction, chunk_cards(f"{icons.status('audit')} Staff activity", lines, per_page=10,
                                                empty="Nothing logged yet."))

    @audit.command(name="nation", description="Everything the bank knows about one nation's account")
    async def nation(interaction: discord.Interaction, nation: str):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        snap = await svc.prices.get()
        with svc.db.read() as conn:
            m = L.get_member(conn, nation_id)
            acc = L.snapshot_accounts(conn, nation_id)
            sums = conn.execute("SELECT bucket, resource, SUM(delta) s FROM ledger_entries WHERE nation_id=? GROUP BY bucket, resource",
                                (nation_id,)).fetchall()
            recent = conn.execute("SELECT ts, entry_type, bucket, resource, delta, pnw_record_id, tx_id, actor FROM ledger_entries "
                                  "WHERE nation_id=? ORDER BY id DESC LIMIT 10", (nation_id,)).fetchall()
        if not m:
            return await reply(interaction, "No account for that nation.")
        ok = all(acc["available" if s["bucket"] == "AVAILABLE" else "locked"].get(s["resource"], 0) == s["s"]
                 for s in sums if s["s"])
        c = A.Card(f"Audit: {m['nation_name'] or ''} [#{nation_id}]", color=A.GREEN if ok else A.RED)
        c.add("Available", fmt.amounts_with_value(acc["available"], value_amounts(acc["available"], snap)))
        c.add("Locked", fmt.amounts_with_value(acc["locked"], value_amounts(acc["locked"], snap)))
        c.add("Ledger matches balance", "yes" if ok else "NO - MISMATCH", True)
        c.add("Frozen", "yes" if m["frozen"] else "no", True)
        c.add("Discord", f"<@{m['discord_id']}>" if m["discord_id"] else "not linked", True)
        c.add("Latest entries", "\n".join(
            f"`{r['ts'][:16]}` {r['entry_type']} {r['resource']} {M.fmt_units(r['resource'], r['delta'])} "
            f"{'PnW#'+str(r['pnw_record_id']) if r['pnw_record_id'] else ''}{' tx#'+str(r['tx_id']) if r['tx_id'] else ''}"
            for r in recent) or "-")
        async def b_chart(kind, name):
            async def go(i):
                await svc.actions["chart_nation"](i, str(nation_id), app_commands.Choice(name=name, value=kind))
            return go

        async def b_reserve(i):
            if not await need(svc, i, "MINISTER"):
                return

            async def done(i2, amounts, lock_type, why):
                await svc.actions["reserve"](i2, str(nation_id), amounts, lock_type, why)
            await open_form(i, "Reserve funds (AVAILABLE → LOCKED)", [
                dict(label="Amounts to lock", placeholder="e.g. money=500m", max=200),
                dict(label="Lock type", placeholder="WARCHEST", max=40), dict(label="Reason", max=200)], done)

        async def b_freeze(i):
            if not await need(svc, i, "MINISTER"):
                return
            name = "unfreeze" if m["frozen"] else "freeze"

            async def done(i2, why):
                await svc.actions[name](i2, str(nation_id), why)
            await open_form(i, f"{name.title()} this account", [dict(label="Reason", max=200)], done)
        actions = [("Resource mix", "📊", "secondary", await b_chart("mix", "Resource mix")),
                   ("Value over time", "📈", "secondary", await b_chart("trend", "Value over time")),
                   ("Reserve funds…", "🔒", "primary", b_reserve),
                   ("Unfreeze" if m["frozen"] else "Freeze", "🧊", "danger" if not m["frozen"] else "success", b_freeze)]
        await reply(interaction, card=c, view=ActionView(interaction.user.id, actions))

    @audit.command(name="configlog", description="Admin: every configuration and security change, newest first")
    @app_commands.describe(setting="Optional: only entries about this setting (or part of its name)")
    async def configlog(interaction: discord.Interaction, setting: str = ""):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            if setting.strip():
                rows = conn.execute("SELECT * FROM config_audit WHERE setting LIKE ? ORDER BY id DESC LIMIT 100", (f"%{setting.strip()}%",)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM config_audit ORDER BY id DESC LIMIT 100").fetchall()
        lines = [f"`{r['ts'][5:16].replace('T', ' ')}` <@{r['actor_id']}> · **{r['setting']}**\n"
                 f"-# {r['previous'] or '—'} → {r['new'] or '—'} · {r['action']}" + (f" · {r['target']}" if r["target"] else "") for r in rows]
        await paginate(interaction, chunk_cards("⚙️ Configuration & security audit", lines, per_page=7,
                                                empty="No configuration changes recorded yet.",
                                                intro="Permanent record. Entries can't be edited or deleted."))

    @audit.command(name="run", description="Run a full reconciliation audit now")
    async def run(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        result = await svc.do_reconcile(f"discord:{interaction.user.id}")
        ok = result["result"] == "OK"
        c = A.Card("Audit " + result["result"], "", A.GREEN if ok else A.RED)
        for f in result["findings"][:10]:
            c.add(f"{f['severity']}: {f['kind']}", f["message"])
        if ok:
            c.description = "All checks passed."
        await reply(interaction, card=c)

    # ----------------------------------------------------------------- /ledger
    @ledger_grp.command(name="reconcile", description="Run reconciliation and show the result")
    async def lreconcile(interaction: discord.Interaction):
        await run.callback(interaction)

    @ledger_grp.command(name="dashboard", description="Financial integrity status and open problems")
    async def ldashboard(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            st = L.integrity_state(conn)
            events = L.open_events(conn, 10)
            chain = L.verify_chain(conn, "ledger_entries")
            achain = L.verify_chain(conn, "audit_log")
            last_ok = L.get_state(conn, "last_scan_ok")
            err = L.get_state(conn, "last_scan_error")
            rec = conn.execute("SELECT started_at, result FROM reconciliation_runs ORDER BY id DESC LIMIT 1").fetchone()
        c = A.Card("Ledger integrity", "", A.GREEN if st["state"] == "NORMAL" else (A.RED if st["emergency_lock"] else A.ORANGE))
        c.add("State", st["state"], True)
        c.add("Withdrawals", "PAUSED" if st["bank_paused"] else "enabled", True)
        c.add("Ledger chain", f"intact ({chain['count']} entries)" if chain["ok"] else f"BROKEN at #{chain.get('bad_id')}", True)
        c.add("Audit chain", f"intact ({achain['count']} entries)" if achain["ok"] else f"BROKEN at #{achain.get('bad_id')}", True)
        c.add("Last PnW sync", last_ok or "never", True)
        c.add("Last reconciliation", f"{rec['started_at']} → {rec['result']}" if rec else "never", True)
        if err:
            c.add("Last sync error", err[:300])
        if st["emergency_lock"]:
            c.add("EMERGENCY LOCK", st["emergency_reason"] or "on")
        c.add("Open events", "\n".join(f"#{e['id']} **{e['severity']}** {e['kind']}" for e in events) or "None")

        async def b_recon(i):
            await svc.actions["reconcile"](i)

        async def b_vault(i):
            await svc.actions["holdings"](i)

        async def b_review(i):
            await svc.actions["review"](i)

        async def b_lock(i):
            on = not st["emergency_lock"]
            if not await need(svc, i, "MINISTER" if on else "ADMIN"):
                return

            async def done(i2, why):
                await svc.actions["emergencylock"](i2, app_commands.Choice(name="on" if on else "off", value="on" if on else "off"), why)
            await open_form(i, "Engage EMERGENCY LOCK" if on else "Lift the emergency lock",
                            [dict(label="Reason", max=200)], done)
        await reply(interaction, card=c, view=ActionView(interaction.user.id, [
            ("Reconcile now", "🔎", "primary", b_recon), ("Vault", "🏛️", "secondary", b_vault),
            ("Review queue", "⚠️", "secondary", b_review),
            ("Lift lock" if st["emergency_lock"] else "Emergency lock", "🔓" if st["emergency_lock"] else "🔒",
             "success" if st["emergency_lock"] else "danger", b_lock)]))

    @ledger_grp.command(name="emergencylock", description="Halt (or resume) ALL financial changes")
    @app_commands.choices(state=[app_commands.Choice(name="ON (halt everything)", value="on"),
                                 app_commands.Choice(name="OFF (resume)", value="off")])
    async def emergencylock(interaction: discord.Interaction, state: app_commands.Choice[str], reason: str):
        on = state.value == "on"
        if not await need(svc, interaction, "MINISTER" if on else "ADMIN"):
            return
        await thinking(interaction)

        def do():
            with svc.db.tx() as conn:
                L.set_emergency_lock(conn, on, reason, str(interaction.user.id))
        await asyncio.to_thread(do)
        await reply(interaction, "EMERGENCY LOCK is now ON." if on else "Emergency lock lifted. Held-back deposits are "
                                                                        "credited at the next scan; open integrity events must still be resolved.")
        await svc.alerts.econ(A.lock_state_card(on, reason, f"{interaction.user} ({interaction.user.id})"))

    @ledger_grp.command(name="resolve", description="Admin: close an integrity event after you investigated it")
    async def resolve(interaction: discord.Interaction, event_id: int, note: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        def do():
            with svc.db.tx() as conn:
                return L.resolve_event(conn, event_id, str(interaction.user.id), note)
        try:
            ok = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, "Event resolved." if ok else "That event is not open.")

    RS.attach(svc, nation, "nation")

    svc.actions.update(emergencylock=emergencylock.callback, ledger=ldashboard.callback)
