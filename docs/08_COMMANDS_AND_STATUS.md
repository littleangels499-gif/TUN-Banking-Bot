# 8. Commands and what is built

## Everyone
`/help` shows the commands you are allowed to use (staff see more than members).

## Members
| Command | What it does |
|---|---|
| `/nation link` | Link + verify your PnW nation |
| `/bank dashboard` · `/bank balance` | Available, Locked, total, Current Market Value, recent activity (**never shows tax**) |
| `/bank deposit` | How to make a real PnW deposit (never credits anything by itself) |
| `/bank withdrawself` | Withdraw AVAILABLE funds to your own nation — confirmation first |
| `/bank history` | Your deposits and withdrawals |
| `/chart mybalance` · `/chart mytrend` | Pictures of your own account: resource mix and value over time |

## ECON staff (Auditor = read, Banker = daily, Minister = reserve/adjust/approve, Admin = settings)
**`/bank`** `holdings` (vault, needs the treasury permission) · `linknation` · `offshore` (move funds main → offshore) · `scandeposits` · `reconcile` · `reserve` · `release` · `withdraw` (you pick the funding source) ·
`adjust` · `approve` / `approvals` / `revoke` · `freeze` / `unfreeze` · `lock` / `unlock` (pause withdrawals) ·
`transactions` · `resolvetx` · `review` · `records` (Excel exports)

**`/bankset`** (Admin settings; Discord allows only 25 commands per group, so these live in their own group)
`setrole` · `setaccess` / `access` · `setlogchannel` · `importopening` · `/deposit reset` · `conversionpanel` (see guide 13) · `/loan …` (see guide 14) · `panel` · `excess` · `/trade …` (see guide 16) · `/offshore …` (see guide 17) · `config` · `seticon` / `icons` · `addbanker` / `removebanker` / `listbankers` · `limits` · `settransferlimit` ·
`setdailylimit` · `setrolelimit` · `setnationlimit` · `requireapproval` · `backup` · `restorestage`

`/chart vault · members · deposits · tax · nation` (staff charts)

`/bank sync · dashboard · report · paid · profile · brackets · exemptions · export` (period filters: 7 / 30 / 90 days / all)

`/bulk template · send · status · resume`

`/grant send · list · view`

`/prices` (everyone) · `/audit transactions · stafflog · nation · run` ·
`/ledger reconcile · dashboard · emergencylock · resolve`

## Typing a nation (works in every staff command)
Wherever a command asks for a nation you can type: the **nation id** (`123456`), the **nation link**
(`https://politicsandwar.com/nation/id=123456`), the **nation name** (capitals don't matter), `@someone`
(pick them from Discord's list), or their **Discord username** (if they linked their nation).
Suggestions appear as you type. The bot never guesses: if a name is unclear or matches two nations it shows you
the options instead of choosing one.

## Look and feel
* Every resource has an icon (💵 Cash, 🛢️ Oil, 🥫 Aluminum, 💣 Munitions, 🍞 Food ...). Change any of them,
  even to your own TUN custom emojis, with `/bankset seticon resource:Oil emoji:<:oil:123456789012345678>`
  (type `\\:oil:` in Discord to see the code). `/bankset icons` previews them. Saved in the database, so updates never reset them.
* Dashboards, confirmations, alerts and reports are Discord embeds with sections, status icons (✅ ⏳ ⚠️ ⛔ 🔒),
  thousands separators, a value-mix bar, and a footer with the time.
* Long lists (transactions, review queue, approvals, staff log) are paginated with ◀ Previous / Next ▶ buttons.
* `/bank dashboard` and `/bank holdings` have a 🔄 Refresh button.
* Charts are drawn from real data only: resource mix, value over time, deposits per day, tax per day, alliance vs
  member-held funds. If there is no data, the bot says so instead of drawing something made up.

## What is finished and tested
Phase 1 (bank core) and most of Phase 2 (vault dashboard, tax records/reports/export, Excel exports, limits, two-person
approvals, security). 86 automated tests cover the accounting rules, failure cases (PnW rejecting, timeouts, retries,
crashes mid-transfer, tampering, deleted history) and the command logic.

## NOT built yet (honest list)
* **Warchest tiers and compliance** (next; needs your rules, see HANDOVER_ASSESSMENT.md §9): fully configurable tiers, green/yellow/red status, alerts; never confiscates anything.
* Automatic recovery of overdue loans from a member's **in-game nation** is NOT possible: the PnW API only lets the bot send money out of an alliance bank
  and deposit from the nation that owns the API key. Overdue loans are marked OVERDUE and ECON is alerted (nothing is taken automatically).

## Things you must verify yourself (I could not do this from here)
1. **Live Discord and live Politics & War were not available in my environment.** The accounting core and the command
   logic are tested with simulated PnW and a simulated Discord; the real Discord connection and real PnW calls are untested.
2. The PnW calls (reading bank records/holdings/prices/members and the `bankWithdraw` mutation with the bot key) follow PnW's
   documented API, but PnW can differ in small details. **Test with a tiny amount first** (guide 7, step 5). If PnW
   rejects something, the bot does nothing to balances and tells you the PnW error message.
3. The transfer note carries a `TUN-TX<number>` tag — that's how the bot proves a transfer happened after a timeout.
   Don't strip it.
