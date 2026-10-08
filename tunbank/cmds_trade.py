"""/trade: review alerts and configure trade monitoring (price anomalies, Nationalists, embargoes).

Every configuration change goes through the same configuration audit log as the rest of the bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import configaudit as CA
from . import fmt
from . import ledger as L
from . import money as M
from . import trademon as TM
from .buttons import ActionView
from .config import cfg_get, cfg_set
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, chunk_cards, need, nation_arg, paginate, reply, thinking
from .util import now_iso

log = logging.getLogger("tunbank.trades")
KIND_ICON = {"EMBARGO": "⛔", "NATIONALIST": "🛡️", "PRICE": "💹"}


def parse_resources(text: str) -> str:
    """'all' / '*' / 'food coal oil' -> '*' or 'food,coal,oil'. Raises ValueError with a friendly message."""
    t = (text or "").strip().lower()
    if t in ("", "all", "*", "everything", "all resources"):
        return "*"
    names = []
    for part in t.replace(",", " ").split():
        try:
            names.append(M.resolve_resource(part))
        except M.AmountError as exc:
            raise ValueError(str(exc)) from exc
    if "money" in names:
        raise ValueError("Money isn't a tradable resource here.")
    return ",".join(dict.fromkeys(names))


def alert_line(r) -> str:
    kinds = " ".join(KIND_ICON.get(k, "") for k in r["kinds"].split(","))
    return (f"{'✅' if r['status'] == 'REVIEWED' else '🔴'} **#{r['id']}** {kinds} {r['member_name'] or 'Nation'} [#{r['member_nation_id']}] "
            f"{r['direction'].lower()} {r['quantity']:,} {M.LABELS[r['resource']]} @ ${r['price']:,.2f}"
            + (f" ({r['multiple']:.1f}×)" if r["multiple"] else "") + f" · {str(r['trade_date'] or '')[:10]}")


def register(trade: app_commands.Group, svc: Services):
    uid = lambda i: str(i.user.id)  # noqa: E731

    # ------------------------------------------------------------------ alerts
    @trade.command(name="alerts", description="Auditor: recent trade alerts (newest first)")
    @app_commands.describe(status="Which alerts to show (default: open ones)")
    @app_commands.choices(status=[app_commands.Choice(name="Open (not reviewed)", value="OPEN"),
                                  app_commands.Choice(name="Reviewed", value="REVIEWED"), app_commands.Choice(name="All", value="ALL")])
    async def alerts(interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        want = status.value if status else "OPEN"
        with svc.db.read() as conn:
            rows = conn.execute("SELECT * FROM trade_alerts " + ("" if want == "ALL" else "WHERE status=? ") + "ORDER BY id DESC LIMIT 200",
                                () if want == "ALL" else (want,)).fetchall()
        pages = chunk_cards(f"🔎 Trade alerts · {want.title()}", [alert_line(r) for r in rows], per_page=8,
                            empty="No trade alerts. Everything that was watched followed the rules.")
        await paginate(interaction, pages)

    def detail_card(r) -> A.Card:
        d = dict(r)
        d["kinds"] = r["kinds"].split(",")
        c = TM.alert_card(d)
        c.add("Status", ("✅ Reviewed by <@%s>: %s" % (r["reviewed_by"], r["review_note"])) if r["status"] == "REVIEWED" else "🔴 Open")
        return c

    @trade.command(name="alert", description="Auditor: everything about one trade alert")
    @app_commands.describe(alert_id="The alert number")
    async def alert(interaction: discord.Interaction, alert_id: int):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            r = conn.execute("SELECT * FROM trade_alerts WHERE id=?", (alert_id,)).fetchone()
        if not r:
            return await reply(interaction, f"There is no trade alert #{alert_id}.")

        async def mark(i):
            await review.callback(i, alert_id, "reviewed from the alert screen")
        actions = [("Mark reviewed", "✅", "success", mark)] if r["status"] == "OPEN" else []
        await reply(interaction, card=detail_card(r), view=ActionView(interaction.user.id, actions) if actions else None)

    @trade.command(name="review", description="Banker: mark a trade alert as looked at (with a note)")
    @app_commands.describe(alert_id="The alert number", note="What you found / decided")
    async def review(interaction: discord.Interaction, alert_id: int, note: str):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)

        def do():
            with svc.db.tx() as conn:
                r = conn.execute("SELECT status FROM trade_alerts WHERE id=?", (alert_id,)).fetchone()
                if not r:
                    return "missing"
                if r["status"] == "REVIEWED":
                    return "done"
                conn.execute("UPDATE trade_alerts SET status='REVIEWED', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                             (uid(interaction), now_iso(), note[:300], alert_id))
                L.audit(conn, uid(interaction), "TRADE_ALERT_REVIEWED", f"trade_alert:{alert_id}", {"note": note[:300]})
                return "ok"
        res = await asyncio.to_thread(do)
        await reply(interaction, {"missing": f"There is no trade alert #{alert_id}.", "done": f"Alert #{alert_id} was already reviewed.",
                                  "ok": f"Alert #{alert_id} marked as reviewed."}[res])

    @trade.command(name="status", description="Auditor: is trade monitoring running, and what is it watching?")
    async def status(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            g = lambda k: cfg_get(conn, k)  # noqa: E731
            last, err = L.get_state(conn, "trade_last_poll"), L.get_state(conn, "trade_last_error")
            n_open = conn.execute("SELECT COUNT(*) FROM trade_alerts WHERE status='OPEN'").fetchone()[0]
            n_all = conn.execute("SELECT COUNT(*) FROM trade_alerts").fetchone()[0]
            n_nat = conn.execute("SELECT COUNT(*) FROM trade_nationalists").fetchone()[0]
            n_emb = conn.execute("SELECT COUNT(*) FROM trade_embargoes").fetchone()[0]
        on = lambda k: "✅ on" if g(k) == "1" else "⛔ off"  # noqa: E731
        c = A.Card("🔎 Trade monitoring", "", A.GREEN if not err and g("trade_monitor_enabled") == "1" else A.ORANGE)
        c.add("Monitoring", on("trade_monitor_enabled"), True)
        c.add("Last check", (last or "never")[:19].replace("T", " "), True)
        c.add("Alerts", f"{n_open} open · {n_all} total", True)
        c.add("Price rule", f"{on('trade_price_enabled')} · alert at {g('trade_price_upper')}× higher or {g('trade_price_lower')}× lower · "
                            f"resources: {TM.resources_label(g('trade_price_resources'))}")
        c.add("Nationalists", f"{on('trade_nationalist_enabled')} · {n_nat} listed · scope {g('trade_nationalist_scope')}", True)
        c.add("Embargoes", f"{on('trade_embargo_enabled')} · {n_emb} alliance(s)", True)
        if err:
            c.add("⚠️ Last problem", err[:500])
        await reply(interaction, card=c)

    # ------------------------------------------------------------------ configuration
    @trade.command(name="config", description="ECON: view or change the trade-monitoring settings")
    @app_commands.describe(monitoring="Turn the whole monitor on/off", price_rule="Turn the abnormal-price rule on/off",
                           upper="Alert when price is this many × HIGHER than market", lower="Alert when price is this many × LOWER than market",
                           price_resources="Resources the price rule watches: all, or e.g. food steel",
                           min_value="Ignore price anomalies on trades worth less than this many $ (0 = watch all)",
                           nationalist_rule="Turn the Nationalist rule on/off", nationalist_scope="Which sales count for Nationalists",
                           embargo_rule="Turn the embargo rule on/off", poll_seconds="How often to check PnW (30 or more)")
    @app_commands.choices(nationalist_scope=[app_commands.Choice(name="Every sale", value="ALL"),
                                             app_commands.Choice(name="Global market sales only", value="GLOBAL")])
    async def config(interaction: discord.Interaction, monitoring: Optional[bool] = None, price_rule: Optional[bool] = None,
                     upper: Optional[float] = None, lower: Optional[float] = None, price_resources: Optional[str] = None,
                     min_value: Optional[float] = None, nationalist_rule: Optional[bool] = None,
                     nationalist_scope: Optional[app_commands.Choice[str]] = None, embargo_rule: Optional[bool] = None,
                     poll_seconds: Optional[int] = None):
        changes = {}
        for key, val in (("trade_monitor_enabled", monitoring), ("trade_price_enabled", price_rule),
                         ("trade_nationalist_enabled", nationalist_rule), ("trade_embargo_enabled", embargo_rule)):
            if val is not None:
                changes[key] = "1" if val else "0"
        for key, val, lo in (("trade_price_upper", upper, 1.01), ("trade_price_lower", lower, 1.01), ("trade_price_min_value", min_value, 0)):
            if val is not None:
                if val < lo:
                    return await reply(interaction, f"{key.replace('trade_', '').replace('_', ' ')} must be at least {lo:g}.")
                changes[key] = f"{val:g}"
        if poll_seconds is not None:
            if poll_seconds < 30:
                return await reply(interaction, "Checking more often than every 30 seconds isn't allowed (it would use up your PnW API quota).")
            changes["trade_poll_seconds"] = str(poll_seconds)
        if nationalist_scope is not None:
            changes["trade_nationalist_scope"] = nationalist_scope.value
        if price_resources is not None:
            try:
                changes["trade_price_resources"] = parse_resources(price_resources)
            except ValueError as exc:
                return await reply(interaction, str(exc))
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        if changes:
            def do():
                with svc.db.tx() as conn:
                    for k, v in changes.items():
                        cfg_set(conn, k, v, uid(interaction))
            await asyncio.to_thread(do)
        with svc.db.read() as conn:
            g = lambda k: cfg_get(conn, k)  # noqa: E731
        c = A.Card("🔎 Trade monitoring settings" + (" · saved" if changes else ""), "", A.GREEN if changes else A.BLUE)
        c.add("Monitor", "on" if g("trade_monitor_enabled") == "1" else "off", True)
        c.add("Check every", f"{g('trade_poll_seconds')}s", True)
        c.add("Price rule", f"{'on' if g('trade_price_enabled') == '1' else 'off'} · {g('trade_price_upper')}× higher / {g('trade_price_lower')}× lower\n"
                            f"Resources: {TM.resources_label(g('trade_price_resources'))} · ignore trades under ${g('trade_price_min_value')}")
        c.add("Nationalist rule", f"{'on' if g('trade_nationalist_enabled') == '1' else 'off'} · {g('trade_nationalist_scope')}", True)
        c.add("Embargo rule", "on" if g("trade_embargo_enabled") == "1" else "off", True)
        c.add("Alerts go to", "the trade-alert channel (`/bankset setlogchannel` → Trade alerts), or the ECON log if none is set")
        await reply(interaction, card=c)

    # ------------------------------------------------------------------ Nationalists
    ACTIONS = [app_commands.Choice(name="List", value="list"), app_commands.Choice(name="Add / change", value="add"),
               app_commands.Choice(name="Remove", value="remove")]

    @trade.command(name="nationalist", description="ECON: the Nationalist list (members barred from selling certain resources)")
    @app_commands.describe(action="What to do", nation="The member (id, link, name or @user)",
                           resources="Resources they may NOT sell: all, or e.g. food coal oil")
    @app_commands.choices(action=ACTIONS)
    async def nationalist(interaction: discord.Interaction, action: app_commands.Choice[str], nation: str = "", resources: str = "all"):
        if not await need(svc, interaction, "MINISTER" if action.value != "list" else "AUDITOR"):
            return
        await thinking(interaction)
        if action.value == "list":
            with svc.db.read() as conn:
                rows = conn.execute("SELECT * FROM trade_nationalists ORDER BY nation_id").fetchall()
                on = cfg_get(conn, "trade_nationalist_enabled") == "1"
            lines = []
            for r in rows:
                res = TM.split_resources(r["resources"])
                body = "✓ All resources" if "*" in res else " ".join(f"✓ {M.LABELS[x]}" for x in sorted(res))
                lines.append(f"**{r['nation_name'] or 'Nation'} [#{r['nation_id']}]** {body}\n-# changed by <@{r['updated_by']}> · {r['updated_at'][:10]}")
            pages = chunk_cards(f"🛡️ Nationalist policy · {'enabled' if on else 'DISABLED'}", lines, per_page=6,
                                empty="No Nationalists are listed. Add one with `/trade nationalist add`.")
            return await paginate(interaction, pages)
        nid = await nation_arg(svc, interaction, nation)
        if nid is None:
            return
        try:
            res_text = parse_resources(resources) if action.value == "add" else "*"
        except ValueError as exc:
            return await reply(interaction, str(exc))
        name = None
        try:
            info = await svc.pnw.fetch_nation(nid)
            name = (info or {}).get("nation_name")
        except (PnWRejected, PnWUncertain):
            pass

        def do():
            with svc.db.tx() as conn:
                old = conn.execute("SELECT * FROM trade_nationalists WHERE nation_id=?", (nid,)).fetchone()
                before = TM.resources_label(old["resources"]) if old else "not listed"
                if action.value == "add":
                    conn.execute("INSERT INTO trade_nationalists(nation_id,nation_name,resources,updated_by,updated_at) VALUES(?,?,?,?,?) "
                                 "ON CONFLICT(nation_id) DO UPDATE SET nation_name=COALESCE(excluded.nation_name,nation_name), "
                                 "resources=excluded.resources, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                                 (nid, name, res_text, uid(interaction), now_iso()))
                    after = TM.resources_label(res_text)
                else:
                    if not old:
                        return None
                    conn.execute("DELETE FROM trade_nationalists WHERE nation_id=?", (nid,))
                    after = "removed"
                CA.record(conn, actor=uid(interaction), setting="trade_nationalist", previous=before, new=after,
                          target=f"nation #{nid} {name or ''}".strip(), category="TRADE")
                return after
        res = await asyncio.to_thread(do)
        if res is None:
            return await reply(interaction, f"Nation #{nid} isn't on the Nationalist list.")
        await reply(interaction, f"**{name or 'Nation'} [#{nid}]**: " + (f"may not sell: {res}." if action.value == "add" else "removed from the Nationalist list."))

    # ------------------------------------------------------------------ embargoes
    @trade.command(name="embargo", description="ECON: the embargoed-alliance list (members must not trade with them)")
    @app_commands.describe(action="What to do", alliance="The alliance (id or exact name)",
                           resources="Optional: only these resources are embargoed (default: all)")
    @app_commands.choices(action=ACTIONS)
    async def embargo(interaction: discord.Interaction, action: app_commands.Choice[str], alliance: str = "", resources: str = "all"):
        if not await need(svc, interaction, "MINISTER" if action.value != "list" else "AUDITOR"):
            return
        await thinking(interaction)
        if action.value == "list":
            with svc.db.read() as conn:
                rows = conn.execute("SELECT * FROM trade_embargoes ORDER BY alliance_id").fetchall()
                on = cfg_get(conn, "trade_embargo_enabled") == "1"
            lines = [f"**{r['alliance_name'] or 'Alliance'} [#{r['alliance_id']}]** · {TM.resources_label(r['resources'])}\n"
                     f"-# changed by <@{r['updated_by']}> · {r['updated_at'][:10]}" for r in rows]
            pages = chunk_cards(f"⛔ Embargoed alliances · {'enabled' if on else 'DISABLED'}", lines, per_page=6,
                                empty="No alliances are embargoed. Add one with `/trade embargo add`.")
            return await paginate(interaction, pages)
        if not alliance.strip():
            return await reply(interaction, "Tell me which alliance (its id or exact name).")
        try:
            found = await svc.pnw.find_alliance(alliance)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't look that alliance up: {exc}")
        if not found:
            return await reply(interaction, f"I couldn't find an alliance matching '{alliance}'. Try its id.")
        if len(found) > 1:
            return await reply(interaction, "That matches several alliances, so I won't guess: " + ", ".join(f"{a['name']} [#{a['id']}]" for a in found[:6]))
        aid, aname = found[0]["id"], found[0]["name"]
        if aid == svc.settings.alliance_id:
            return await reply(interaction, "That is our own alliance. It can't be embargoed.")
        try:
            res_text = parse_resources(resources) if action.value == "add" else "*"
        except ValueError as exc:
            return await reply(interaction, str(exc))

        def do():
            with svc.db.tx() as conn:
                old = conn.execute("SELECT * FROM trade_embargoes WHERE alliance_id=?", (aid,)).fetchone()
                before = TM.resources_label(old["resources"]) if old else "not embargoed"
                if action.value == "add":
                    conn.execute("INSERT INTO trade_embargoes(alliance_id,alliance_name,resources,updated_by,updated_at) VALUES(?,?,?,?,?) "
                                 "ON CONFLICT(alliance_id) DO UPDATE SET alliance_name=excluded.alliance_name, resources=excluded.resources, "
                                 "updated_by=excluded.updated_by, updated_at=excluded.updated_at", (aid, aname, res_text, uid(interaction), now_iso()))
                    after = "embargoed: " + TM.resources_label(res_text)
                else:
                    if not old:
                        return None
                    conn.execute("DELETE FROM trade_embargoes WHERE alliance_id=?", (aid,))
                    after = "embargo lifted"
                CA.record(conn, actor=uid(interaction), setting="trade_embargo", previous=before, new=after,
                          target=f"alliance #{aid} {aname}", category="TRADE")
                return after
        res = await asyncio.to_thread(do)
        if res is None:
            return await reply(interaction, f"{aname} [#{aid}] isn't embargoed.")
        await reply(interaction, f"**{aname} [#{aid}]**: {res}.")
