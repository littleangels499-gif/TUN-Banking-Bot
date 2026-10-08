"""Commands every linked member can use. Tax is deliberately NEVER shown here."""
from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord import app_commands

from . import alerts as A
from . import charts as C
from . import fmt
from . import icons
from . import credentials as CR
from . import intents as INT
from . import ledger as L
from . import money as M
from . import resolve as RS
from .config import cfg_bool
from .buttons import ActionView, open_form
from .ui import Services, actor_label, confirm, nation_arg, post_outcomes, reply, role_ids, thinking
from .valuation import value_amounts
from .pnw import PnWRejected, PnWUncertain

log = logging.getLogger("tunbank.member")

NOT_LINKED = "You have not linked your nation yet. Use `/nation link` first (your nation id, link or name)."


def _linked(svc, interaction):
    with svc.db.read() as conn:
        return L.member_by_discord(conn, interaction.user.id)


def loan_summary(conn, nation_id: int) -> str:
    """One short line about the member's active loans, or '' if they have none."""
    from . import loans as LN
    active = LN.active(conn, nation_id)
    if not active:
        return ""
    text = f"**{LN.dollars(sum(LN.owed(l) for l in active))}** owed on {len(active)} loan(s)."
    dues = sorted(l["due_at"][:10] for l in active if l["due_at"])
    if any(LN.is_overdue(l) for l in active):
        text += " ⚠️ **Overdue**: please contact ECON."
    elif dues:
        text += f" Next due: {dues[0]}."
    return text + " See `/loan mine`."


def register(bank: app_commands.Group, nation: app_commands.Group, svc: Services):
    # ------------------------------------------------------------ /nation link
    @nation.command(name="link", description="Link and verify your Politics & War nation")
    @app_commands.describe(nation="Your nation: id, link or name")
    async def link(interaction: discord.Interaction, nation: str):
        await thinking(interaction)
        nation_id = await nation_arg(svc, interaction, nation)
        if nation_id is None:
            return
        try:
            n = await svc.pnw.fetch_nation(nation_id)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I could not reach Politics & War right now ({exc}). Try again shortly.")
        if not n:
            return await reply(interaction, f"Nation {nation_id} was not found.")
        if int(n.get("alliance_id") or 0) != svc.settings.alliance_id:
            return await reply(interaction, "That nation is not in our alliance.")
        names = {interaction.user.name.lower(), str(interaction.user).lower()}
        if (n.get("discord") or "").strip().lower() not in names:
            return await reply(
                interaction,
                "I can't verify you own this nation. In Politics & War open **Account → Settings** and "
                f"set your Discord username to **{interaction.user.name}**, then run this command again.")

        def write():
            with svc.db.tx() as conn:
                row = conn.execute("SELECT nation_id, discord_id FROM members WHERE nation_id=?", (nation_id,)).fetchone()
                other = L.member_by_discord(conn, interaction.user.id)
                if other and other["nation_id"] != nation_id:
                    return f"Your Discord account is already linked to nation {other['nation_id']}. Ask ECON to unlink it first."
                if row and row["discord_id"] and row["discord_id"] != str(interaction.user.id):
                    return "That nation is already linked to a different Discord account. Contact ECON."
                from . import configaudit as CA
                CA.record(conn, actor=interaction.user.id, setting="nation_link", previous="unlinked" if not row or not row["discord_id"] else f"<@{row['discord_id']}>",
                          new=f"<@{interaction.user.id}>", target=f"nation [#{nation_id}] · self-service link, verified against PnW",
                          category="LINK", only_if_changed=False)
                L.ensure_member(conn, nation_id, n.get("nation_name"))
                conn.execute("UPDATE members SET discord_id=?, discord_name=?, linked_at=datetime('now') WHERE nation_id=?",
                             (str(interaction.user.id), interaction.user.name, nation_id))
                L.audit(conn, interaction.user.id, "NATION_LINKED", f"nation:{nation_id}", {})
            return None

        err = write()
        if err:
            return await reply(interaction, err)
        await reply(interaction, f"Linked! **{n.get('nation_name')}** [#{nation_id}] is now connected to your Discord account.")

    # --------------------------------------------------------- /bank dashboard
    async def account_card(interaction, title):
        m = _linked(svc, interaction)
        if not m:
            await reply(interaction, NOT_LINKED)
            return None
        snap = await svc.prices.get()

        def load():
            if m["discord_name"] != interaction.user.name:
                with svc.db.tx() as conn:
                    conn.execute("UPDATE members SET discord_name=? WHERE nation_id=?", (interaction.user.name, m["nation_id"]))
            with svc.db.read() as conn:
                return (L.get_balances(conn, m["nation_id"], "AVAILABLE"),
                        L.get_balances(conn, m["nation_id"], "LOCKED"),
                        L.holds(conn, m["nation_id"], "MEMBER_AVAILABLE"),
                        L.integrity_state(conn),
                        loan_summary(conn, m["nation_id"]))
        import asyncio
        av, lk, hold, st, loan_text = await asyncio.to_thread(load)
        va, vl = value_amounts(av, snap), value_amounts(lk, snap)
        total = value_amounts(M.add(av, lk), snap)
        c = A.Card(f"{icons.status('bank')} {title}", f"{icons.status('member')} **{m['nation_name'] or 'Your nation'}** [#{m['nation_id']}]", A.BLUE)
        c.add(f"{icons.status('money')} Available · you can withdraw this", fmt.amounts_with_value(av, va))
        c.add(f"{icons.status('lock')} Locked · reserved by ECON", fmt.amounts_with_value(lk, vl))
        if hold:
            c.add(f"{icons.status('wait')} Pending withdrawals · on hold", fmt.amount_lines(hold))
        owes = any(v < 0 for v in M.add(av, lk).values()) or any(v < 0 for v in av.values())
        c.add(f"{icons.status('chart')} " + ("Net deposit" if owes else "Total deposit"), fmt.amounts_with_value(M.add(av, lk), total))
        if owes:
            c.add("➖ Negative balances",
                  "A negative amount is owed to the alliance. It reduces your net deposit worth, which is the most a single "
                  "withdrawal may be worth. Your withdrawals are still limited to what you hold.")
        mix = fmt.composition(M.add(av, lk), total)
        if mix:
            c.add("Where your value sits", mix)
        if loan_text:
            c.add("🤝 Loan · owed to the alliance (separate from your deposit)", loan_text)
        if m["frozen"]:
            c.add(f"{icons.status('freeze')} Account status", "FROZEN by ECON: withdrawals are disabled.")
            c.color = A.ORANGE
        if st["state"] != "NORMAL" and st["state"] != "WARNING":
            c.add(f"{icons.status('warn')} Bank status", "Some banking functions are temporarily restricted for review.")
        return c, m

    async def build_dashboard(interaction):
        got = await account_card(interaction, "Your TUN Bank account")
        if not got:
            return None
        card, m = got
        with svc.db.read() as conn:
            rows = conn.execute(
                "SELECT ts, entry_type, bucket, resource, delta FROM ledger_entries WHERE nation_id=? "
                "AND NOT (entry_type IN ('LOCK','RELEASE') AND bucket='LOCKED') ORDER BY id DESC LIMIT 6",
                (m["nation_id"],)).fetchall()
        if rows:
            card.add(f"{icons.status('time')} Recent activity", "\n".join(
                f"`{r['ts'][5:16].replace('T', ' ')}` {icons.resource(r['resource'])} {r['entry_type'].title()} · "
                f"{'+' if r['delta'] > 0 else '−'}{M.fmt_units(r['resource'], abs(r['delta']))}" for r in rows))
        return card

    # ------------------------------------------------------------- buttons
    _last_check: dict = {}

    def member_actions():
        return [("Withdraw", "💸", "primary", act_withdraw_form),
                ("Withdraw all cash", "💵", "secondary", act_withdraw_cash),
                ("History", "📜", "secondary", act_history),
                ("Chart", "📊", "secondary", act_chart),
                ("How to deposit", "📥", "secondary", act_deposit_help)]

    async def show_dashboard(interaction, simple=False):
        if simple:
            got = await account_card(interaction, "Your balance")
            card = got[0] if got else None
        else:
            card = await build_dashboard(interaction)
        if card is None:
            return

        async def refresh():
            if simple:
                g = await account_card(interaction, "Your balance")
                return g[0] if g else None
            return await build_dashboard(interaction)
        await reply(interaction, card=card, view=ActionView(interaction.user.id, member_actions(), refresh=refresh))

    @bank.command(name="dashboard", description="Your bank account: available, locked, total and recent activity")
    async def dashboard(interaction: discord.Interaction):
        await thinking(interaction)
        await show_dashboard(interaction)

    @bank.command(name="balance", description="Quick view of your balance")
    async def balance(interaction: discord.Interaction):
        await thinking(interaction)
        await show_dashboard(interaction, simple=True)

    async def act_dashboard(interaction):
        await thinking(interaction)
        await show_dashboard(interaction)

    # ---------------------------------------------------------------- deposit
    def deposit_card():
        with svc.db.read() as conn:
            ign, loan = conn_tags(conn)
        c = A.Card(f"{icons.status('deposit')} How to deposit", "Deposits are only credited when a REAL transfer shows up in the "
                   "alliance bank. I never create a balance from a command.", A.BLUE)
        c.add("Steps", "1. In Politics & War open the **Alliance** page → **Bank** → **Deposit**.\n"
                       "2. Choose the money/resources and make the deposit **with no note**.\n"
                       "3. Within a couple of minutes I detect it and credit your **Available** balance. "
                       "You will get a DM. Or press **Check my deposit now** below.")
        c.add("Special notes", f"`{ign}` = donation to the alliance (NOT added to your balance)\n"
                               f"`{loan}` = loan repayment")
        return c

    async def act_deposit_help(interaction):
        m = _linked(svc, interaction)
        direct = bool(m and can_direct(m))

        async def b_plan(i):
            await open_form(i, "Deposit from Discord" if direct else "Plan my deposit",
                            [dict(label="What will you deposit?", placeholder="e.g. money=5m coal=2000", max=200)],
                            submit_direct_form if direct else submit_plan_form)
        await reply(interaction, card=deposit_card(),
                    view=ActionView(interaction.user.id, [("Deposit from Discord…" if direct else "Plan my deposit…", "📥" if direct else "📝", "primary", b_plan),
                                                          ("Check my deposit now", "✅", "success", act_check_deposit)]))

    async def plan_deposit(interaction, amounts_text: str):
        """Guided deposit: exact in-game steps for what the member said. Creates NO balance and sends NOTHING."""
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        try:
            parsed = M.parse_amounts(amounts_text)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        if can_direct(m):
            return await direct_deposit(interaction, m, parsed)
        snap = await svc.prices.get()
        val = value_amounts(parsed, snap)

        def make():
            with svc.db.tx() as conn:
                return INT.create(conn, m["nation_id"], parsed)
        iid = await asyncio.to_thread(make)
        c = A.Card(f"{icons.status('deposit')} Your deposit plan #{iid}",
                   "Here are the exact steps to deposit from inside Politics & War. "
                   "Your balance changes only after the REAL deposit shows up.", A.BLUE)
        c.add("Deposit exactly", fmt.amounts_with_value(parsed, val))
        c.add("In game", "Alliance page → **Bank** → **Deposit**. Enter those amounts and leave the **note empty**.\n"
                         "Use the alliance **TUN** (the main bank), from your own nation.")
        c.add("After you deposit", "I detect it within a couple of minutes, credit your **Available** balance and DM you. "
                                   "Or press **Check my deposit now**.")
        c.add("Want the bot to do this for you?", "Save your own API key with `/nation setkey` (needs *Whitelisted access* switched on in your "
                                                  "PnW account). Then `/bank deposit` starts the deposit from your nation after you confirm. "
                                                  "The bot can never act for your nation without a key you chose to give it.")
        c.footer = f"Plan valid for {INT.HOURS} hours · TUN Bank"
        await reply(interaction, card=c, view=ActionView(interaction.user.id, [("Check my deposit now", "✅", "success", act_check_deposit)]))

    # ------------------------------------------------ direct deposit from Discord (member's own key)
    def can_direct(m) -> bool:
        with svc.db.read() as conn:
            return bool(svc.deposits and svc.deposits.usable_for(conn, m["nation_id"], m["discord_id"]))

    async def direct_deposit(interaction, m, parsed, skip_confirm=False):
        nid = m["nation_id"]
        snap = await svc.prices.get()
        val = value_amounts(parsed, snap)
        with svc.db.read() as conn:
            hint_ = CR.get_row(conn, nid)["key_hint"]
        card = A.Card(f"{icons.status('deposit')} Confirm deposit from your nation",
                      "The bot will start this deposit FROM YOUR NATION into the TUN bank, using the API key you saved. "
                      "It cannot be undone.", A.ORANGE)
        card.add("From", f"Your nation [#{nid}]", True)
        card.add("To", "TUN main bank", True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        card.add("Your key", f"saved key `{hint_}` · never shown or logged", True)
        card.add("Crediting", "Your TUN balance changes only after PnW's real bank record appears. If PnW refuses, nothing changes.")
        if not skip_confirm and not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")
        res = await svc.deposits.start(nation_id=nid, discord_id=interaction.user.id, amounts=parsed,
                                       idem=f"mdep-{interaction.id}", value_cents=val.total_cents, snapshot_id=snap.id if snap else None)
        await post_outcomes(svc, res.get("outcomes") or [])
        await svc.alerts.flush_events()
        ok = res["status"] in ("CREDITED", "SENT")
        icon = {"CREDITED": "ok", "SENT": "wait", "UNCERTAIN": "warn"}.get(res["status"], "bad")
        out = A.Card(f"{icons.status(icon)} Deposit {res['status'].title()}", res["message"],
                     A.GREEN if res["status"] == "CREDITED" else (A.ORANGE if ok or res["status"] == "UNCERTAIN" else A.RED), kind="DEPOSIT")
        out.add("Amount", fmt.amounts_with_value(parsed, val))
        if res.get("record_id"):
            out.add("PnW record", f"#{res['record_id']}", True)
        if res.get("deposit_id"):
            out.add("Deposit", f"#{res['deposit_id']}", True)
        await reply(interaction, card=out, view=ActionView(interaction.user.id, [("My dashboard", "🏦", "primary", act_dashboard),
                                                                                  ("Check my deposit now", "✅", "secondary", act_check_deposit)]))

    async def submit_direct_form(interaction, amounts):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        try:
            parsed = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        if not can_direct(m):
            return await reply(interaction, "Direct deposits are not set up for your account. Use `/nation setkey`, or `/bank deposit` for the manual steps.")
        await direct_deposit(interaction, m, parsed)

    async def submit_plan_form(interaction, amounts):
        await thinking(interaction)
        await plan_deposit(interaction, amounts)

    API_NOT_SET = ("**Your PnW API access is not configured.**\n\n"
                   "Please link your nation and enable the required API access before using this feature:\n"
                   "1. Link your nation: `/nation link`\n"
                   "2. In Politics & War open your **Account** page, copy your API key and switch **Whitelisted access** ON.\n"
                   "3. Press **Set up my API key** below (the key is stored encrypted and never shown).")

    async def api_help(interaction):
        if not (svc.deposits and svc.deposits.available()):
            return await reply(interaction, "Deposits from Discord are not switched on for this bot yet. ECON can enable them; "
                                            "meanwhile `/bank deposit` shows the exact in-game steps.")
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        await reply(interaction, API_NOT_SET, view=ActionView(interaction.user.id, [("Set up my API key", "🔑", "primary", open_key_form)]))

    async def open_key_form(interaction):
        m = _linked(svc, interaction)
        if not m:
            return await interaction.response.send_message(NOT_LINKED, ephemeral=True)
        if not (svc.deposits and svc.deposits.available()):
            return await interaction.response.send_message("Direct deposits are not switched on for this bot.", ephemeral=True)
        await open_form(interaction, "Your PnW API key", [dict(label="Paste your API key (it is hidden from everyone)", placeholder="your PnW API key", max=64)], submit_key)

    async def panel_deposit(interaction, parsed):
        """Deposit Funds from the panel. `interaction` is already deferred."""
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        if not can_direct(m):
            return await api_help(interaction)
        await direct_deposit(interaction, m, parsed)

    async def act_excess(interaction):
        """Deposit Excess: work out what the member's nation holds above ECON's configured limits and deposit it."""
        await thinking(interaction)
        from .config import cfg_get
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        with svc.db.read() as conn:
            limits_text = (cfg_get(conn, "excess_holdings") or "").strip()
        if not limits_text:
            return await reply(interaction, "ECON hasn't set the excess-holdings limits yet, so there is nothing to compare against. "
                                            "(Admins: `/bankset excess`.)")
        try:
            limits = M.parse_amounts(limits_text)
        except M.AmountError:
            return await reply(interaction, "The excess-holdings limits are set up incorrectly. Please tell ECON.")
        if not can_direct(m):
            return await api_help(interaction)
        try:
            held = await svc.deposits.holdings(nation_id=m["nation_id"], discord_id=interaction.user.id)
        except CR.CredentialError as exc:
            return await reply(interaction, str(exc))
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't read your nation's holdings from Politics & War, so nothing was deposited: {exc}\n"
                                            "Check that **Whitelisted access** is on for your API key.")
        excess = {r: held.get(r, 0) - lim for r, lim in limits.items() if held.get(r, 0) > lim}
        if not excess:
            return await reply(interaction, f"{icons.status('ok')} **No excess found.** Everything in your nation is within ECON's limits.")
        snap = await svc.prices.get()
        val = value_amounts(excess, snap)
        card = A.Card(f"{icons.status('deposit')} EXCESS FUNDS FOUND",
                      "These are above ECON's limits for what a nation should keep. Press Confirm to deposit all of it.", A.ORANGE)
        card.add("Excess", fmt.amounts_with_value(excess, val))
        card.add("Your nation holds → limit", "\n".join(
            f"{M.LABELS[r]}: {M.fmt_units(r, held.get(r, 0))} → {M.fmt_units(r, limits[r])}" for r in excess)[:1000])
        card.add("Crediting", "Your TUN balance changes only after PnW's real bank record appears. If PnW refuses, nothing changes.")
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")
        await direct_deposit(interaction, m, excess, skip_confirm=True)

    # ----------------------------------------------------------- the member's own API key
    async def submit_key(interaction, api_key):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        api_key = api_key.strip()
        CR.register_secret(api_key)
        if not CR.KEY_RE.match(api_key):
            return await reply(interaction, "That doesn't look like a PnW API key (letters and numbers only). Nothing was saved.")
        try:
            owner = await svc.pnw.fetch_key_owner(api_key)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"PnW didn't accept that key, so it was not saved: {CR.redact(exc)}")
        if owner is not None and owner != m["nation_id"]:
            return await reply(interaction, f"{icons.status('bad')} That key belongs to a different nation. A key can only be saved for **your own** nation. Nothing was saved.")

        def store():
            with svc.db.tx() as conn:
                CR.save(conn, svc.crypto, nation_id=m["nation_id"], discord_id=str(interaction.user.id), api_key=api_key, verified=owner is not None)
        try:
            await asyncio.to_thread(store)
        except CR.CredentialError as exc:
            return await reply(interaction, str(exc))
        c = A.Card(f"{icons.status('ok')} Your API key is saved", "Stored encrypted. The bot never shows it and never writes it to logs.", A.GREEN)
        c.add("Key", f"`{CR.hint(api_key)}`", True)
        c.add("Checked against PnW", "yes, it is your nation's key" if owner is not None else "PnW can't tell the bot who owns a key; it is checked on your first deposit", True)
        c.add("Do this once in Politics & War", "Open your **Account** page and switch **Whitelisted access** ON. Without it PnW refuses deposits started by a bot.")
        c.add("How it is used", "Only when YOU run `/bank deposit` and press **Confirm**, and only to deposit from your own nation into the TUN bank. "
                                "Remove it any time with `/nation removekey`. If you ever think the key leaked, make a new one in PnW.")
        await reply(interaction, card=c)

    @nation.command(name="setkey", description="Let TUN Bank start deposits from YOUR nation with your own PnW API key")
    async def setkey(interaction: discord.Interaction):
        await open_key_form(interaction)

    @nation.command(name="removekey", description="Delete the API key you saved with TUN Bank")
    async def removekey(interaction: discord.Interaction):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)

        def do():
            with svc.db.tx() as conn:
                return CR.remove(conn, m["nation_id"], str(interaction.user.id), "removed by the member")
        removed = await asyncio.to_thread(do)
        await reply(interaction, "Your saved API key was deleted." if removed else "You had no saved API key.")

    @bank.command(name="deposit", description="Deposit: start it from Discord (with your own API key) or get the exact in-game steps")
    @app_commands.describe(amounts="What you plan to deposit, e.g. money=5m coal=2000 (leave empty for the general guide)")
    async def deposit(interaction: discord.Interaction, amounts: str = ""):
        if amounts.strip():
            await thinking(interaction)
            return await plan_deposit(interaction, amounts)
        await act_deposit_help(interaction)

    async def act_check_deposit(interaction):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        wait = 60 - (time.monotonic() - _last_check.get(interaction.user.id, -1e9))
        if wait > 0:
            return await reply(interaction, f"{icons.status('wait')} Please wait {int(wait) + 1}s before checking again. "
                                            "New deposits are also detected automatically every couple of minutes.")
        _last_check[interaction.user.id] = time.monotonic()
        res = await svc.scanner.scan()
        if not res.ok:
            return await reply(interaction, f"{icons.status('warn')} I couldn't reach Politics & War just now. "
                                            "Your deposit will still be picked up automatically. Try again in a few minutes.")
        await post_outcomes(svc, res.outcomes)
        await svc.alerts.flush_events()
        mine = [o for o in res.outcomes if o.kind == "CREDIT" and o.nation_id == m["nation_id"]]
        await reply(interaction, f"{icons.status('ok')} Found **{len(mine)}** new deposit(s) for you." if mine else
                    f"{icons.status('info')} No new deposits from you yet. They can take a minute or two to appear in Politics & War.")
        await show_dashboard(interaction)

    # --------------------------------------------------------------- withdraw
    async def do_withdraw(interaction, parsed, note, dest=None):
        """Everything after the amounts are known. `interaction` is already deferred.
        dest=(nation_id, label) sends the member's own AVAILABLE funds to ANOTHER nation (Send Funds); the balance check,
        limits, net-worth rule, confirmation, PnW confirmation and ledger are exactly the same."""
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        with svc.db.read() as conn:
            if not cfg_bool(conn, "self_withdraw_enabled"):
                return await reply(interaction, "Self-withdrawals are currently switched off by ECON.")
        nid = m["nation_id"]
        dest_id, dest_label = (dest if dest else (nid, f"Your nation [#{nid}]"))
        try:
            snap, val, _ = await svc.wd.prepare(funding_source="MEMBER_AVAILABLE", amounts=parsed)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"Could not check live data: {exc}")
        with svc.db.read() as conn:
            free = L.spendable(conn, nid)
            locked = L.get_balances(conn, nid, "LOCKED")
        short = [f"{M.LABELS[r]} (you have {M.fmt_units(r, free.get(r, 0))})" for r, a in parsed.items() if free.get(r, 0) < a]
        if short:
            msg = "You don't have enough **available** funds for: " + ", ".join(short) + "."
            if any(locked.get(r) for r in parsed):
                msg += " Locked (reserved) funds cannot be withdrawn; ask ECON."
            return await reply(interaction, msg)
        from . import limits as LIM
        try:
            with svc.db.read() as conn:
                LIM.check_net_worth(conn, tx_type="WITHDRAW_SELF", nation_id=nid, amounts=parsed, snapshot=snap)
        except LIM.LimitExceeded as exc:
            return await reply(interaction, str(exc))
        card = A.Card(f"{icons.status('withdraw')} Confirm " + ("transfer to another nation" if dest else "withdrawal"),
                      "Check everything carefully. Nothing is sent until you press Confirm.", A.ORANGE)
        card.add(f"{icons.status('money')} Funding source", "YOUR AVAILABLE DEPOSIT", True)
        card.add(f"{icons.status('member')} Destination", dest_label, True)
        card.add("Amount", fmt.amounts_with_value(parsed, val))
        card.add("Available before", fmt.amount_lines(free), True)
        card.add("Available after", fmt.amount_lines(M.sub(free, parsed)), True)
        if note:
            card.add("Note", note[:100])
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")
        res = await svc.wd.request(
            tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=nid, lock_id=None,
            dest_nation_id=dest_id, amounts=parsed, actor=str(interaction.user.id),
            note=note or ("TUN Bank transfer" if dest else "TUN Bank withdrawal"),
            reason="member transfer to another nation" if dest else "member self-withdrawal",
            idempotency_key=f"{'send' if dest else 'self'}-{interaction.id}",
            actor_role_ids=role_ids(interaction))
        out = A.withdrawal_card(res, actor_label=actor_label(interaction), dest_nation_id=dest_id,
                                source_label="Member AVAILABLE", amounts=parsed, note=note)
        await reply(interaction, card=out, view=ActionView(interaction.user.id, [
            ("My dashboard", "🏦", "primary", act_dashboard), ("Withdraw more", "💸", "secondary", act_withdraw_form)]))
        await svc.alerts.econ(out)
        if res.internal:
            await svc.alerts.econ(A.Card(f"{icons.status('warn')} A member withdrawal could not be paid",
                                         f"{actor_label(interaction)} · nation [#{nid}]\n{res.internal}", A.ORANGE, kind="WITHDRAWAL"))
        await svc.alerts.flush_events()

    async def do_send(interaction, recipient_text, parsed, note):
        """Send Funds: the member's own available balance to another nation. `interaction` is already deferred."""
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        with svc.db.read() as conn:
            if not cfg_bool(conn, "member_send_enabled"):
                return await reply(interaction, "Sending funds to other nations is currently switched off by ECON.")
        try:
            rid = await RS.resolve(svc, recipient_text)
        except RS.ResolveError as exc:
            return await reply(interaction, str(exc))
        if rid == m["nation_id"]:
            return await reply(interaction, "That is your own nation. Use **Withdraw to Me** instead.")
        try:
            info = await svc.pnw.fetch_nation(rid)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't check that nation with Politics & War, so nothing was sent: {exc}")
        if not info:
            return await reply(interaction, f"There is no nation with id {rid} in Politics & War. Nothing was sent.")
        label = f"{info.get('nation_name') or 'Nation'} [#{rid}]" + (f" · alliance #{info['alliance_id']}" if info.get("alliance_id") else " · no alliance")
        await do_withdraw(interaction, parsed, note or f"Sent via TUN Bank by nation #{m['nation_id']}", dest=(rid, label))

    async def submit_withdraw_form(interaction, amounts, note):
        await thinking(interaction)
        try:
            parsed = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        await do_withdraw(interaction, parsed, note)

    async def act_withdraw_form(interaction):
        await open_form(interaction, "Withdraw from your deposit", [
            dict(label="What to withdraw", placeholder="e.g. money=1m coal=5000 aluminum=2k", max=200),
            dict(label="Note (optional)", required=False, max=100)], submit_withdraw_form)

    async def act_withdraw_cash(interaction):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        with svc.db.read() as conn:
            free = L.spendable(conn, m["nation_id"])
        if not free.get("money"):
            return await reply(interaction, "You have no available cash to withdraw.")
        await do_withdraw(interaction, {"money": free["money"]}, "")

    @bank.command(name="withdrawself", description="Withdraw your AVAILABLE funds to your own nation")
    @app_commands.describe(amounts="e.g. money=1m coal=5000 aluminum=2k", note="Optional note")
    async def withdrawself(interaction: discord.Interaction, amounts: str, note: str = ""):
        await thinking(interaction)
        try:
            parsed = M.parse_amounts(amounts)
        except M.AmountError as exc:
            return await reply(interaction, f"I couldn't read those amounts: {exc}")
        await do_withdraw(interaction, parsed, note)

    # ---------------------------------------------------------------- history
    async def send_history(interaction):
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        with svc.db.read() as conn:
            rows = conn.execute(
                "SELECT id, created_at, status, funding_source, value_cents, note FROM transactions "
                "WHERE member_nation_id=? ORDER BY id DESC LIMIT 10", (m["nation_id"],)).fetchall()
            deps = conn.execute(
                "SELECT ts, resource, delta, pnw_record_id FROM ledger_entries WHERE nation_id=? AND "
                "entry_type='DEPOSIT' ORDER BY id DESC LIMIT 10", (m["nation_id"],)).fetchall()
        c = A.Card(f"{icons.status('time')} Your history", color=A.BLUE)
        c.add(f"{icons.status('deposit')} Recent deposits", "\n".join(
            f"`{d['ts'][5:16].replace('T', ' ')}` {icons.resource(d['resource'])} +{M.fmt_units(d['resource'], d['delta'])} · PnW #{d['pnw_record_id']}"
            for d in deps) or "_None yet_")
        c.add(f"{icons.status('withdraw')} Recent withdrawals", "\n".join(fmt.tx_line(r) for r in rows) or "_None yet_")
        await reply(interaction, card=c, view=ActionView(interaction.user.id, [("My dashboard", "🏦", "primary", act_dashboard)]))

    async def act_history(interaction):
        await thinking(interaction)
        await send_history(interaction)

    @bank.command(name="history", description="Your own transaction history")
    async def history(interaction: discord.Interaction):
        await thinking(interaction)
        await send_history(interaction)

    # ------------------------------------------------------------------ chart
    async def act_chart(interaction):
        await thinking(interaction)
        m = _linked(svc, interaction)
        if not m:
            return await reply(interaction, NOT_LINKED)
        snap = await svc.prices.get()

        def build():
            with svc.db.read() as conn:
                return C.composition(conn, snap, m["nation_id"], RS.label(conn, m["nation_id"]))
        try:
            png = await asyncio.to_thread(build)
        except C.ChartError as exc:
            return await reply(interaction, f"{icons.status('info')} {exc}")
        card = A.Card(f"{icons.status('chart')} Your resource mix", "Share of your deposit's value per resource.", A.BLUE)
        card.image = "attachment://composition.png"
        await reply(interaction, card=card, file=discord.File(io.BytesIO(png), filename="composition.png"))

    svc.actions.update(dashboard=dashboard.callback)
    RS.attach(svc, link, "nation")

    svc.actions["member_withdraw"] = do_withdraw
    svc.actions["member_send"] = do_send
    svc.actions["member_deposit"] = panel_deposit
    svc.actions["member_excess"] = act_excess
    svc.actions["member_dashboard"] = act_dashboard
    svc.actions["member_api_help"] = api_help


def conn_tags(conn):
    from .config import cfg_get
    return cfg_get(conn, "tag_ignore"), cfg_get(conn, "tag_loan")
