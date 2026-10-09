# 7. First-time setup inside Discord

Do these in order. Everything here is done with slash commands typed in Discord (you, as an owner, are Administrator automatically).

## 1. Roles and log channel
1. Make a **private** channel only ECON can see, e.g. `#econ-log`.
2. `/bankset setlogchannel channel:#econ-log` — a test message should appear.
3. Give your staff roles permissions (repeat for each role):
   `/bankset setrole level:BANKER role:@Econ Officer` · `level:MINISTER role:@Econ Minister` · `level:AUDITOR role:@Auditor` · `level:ADMIN role:@Admin`
   * **Auditor** read-only · **Banker** daily transfers within limits · **Minister** reserve/release/adjust/approve · **Admin** settings & security.
4. `/bankset listbankers` shows what is configured.

## 2. Test the connection to PnW (with tiny amounts!)
1. The very first time the bot starts, it automatically does a "baseline" scan: all existing PnW bank history is stored as
   evidence **without crediting anyone** (your opening balances are what count for the past). Run `/bank sync`
   to confirm the scanner works — you should see no errors.
   *If a member deposited after your Locutus export but before the first start, that deposit was stored as history and
   not credited. Find its record number with `/bank records kind:deposits` (Status = BASELINE) and credit it with
   `/bank review record_id:<number> action:credit nation:<id, name or @user> note:<why>`.*
2. `/bank holdings` — should show your real PnW bank contents.
3. **Before trusting it with real money**, test a withdrawal with a tiny amount (see step 5).

## 3. Import your opening balances (from the Locutus export)
1. Ask members to **pause deposits/withdrawals** while you switch. Export balances from Locutus.
2. Make a spreadsheet (.xlsx or .csv) with a header row: `nation_id, money, food, coal, oil, uranium, iron, bauxite, lead, gasoline, munitions, steel, aluminum`
   (only the columns you need; one row per nation; blank = zero). A long layout `nation_id, resource, amount` also works.
3. `/bankset importopening file:<your file> note:"Locutus export 2026-xx-xx"`
4. The **preview** shows members found/missing, totals, current market value and any errors. Bad rows (duplicates, negatives,
   unknown nations, malformed numbers) **block the import**: fix the file and upload it again. Nothing is written until you press **Confirm**.
5. Each nation/resource can get an opening balance only once, and it can never be edited afterwards.
6. Run `/ledger reconcile`. Compare `/bank holdings` — member-held totals must not exceed what the PnW bank really holds.

## 4. Members link their nation
Each member runs `/nation link nation:123456` (their PnW Discord username must match; the bot explains if not), then `/bank dashboard`.

## 5. Safe first tests
* Deposit something small in-game with **no note** → within ~2 min it is credited, ECON log + member DM appear.
* Deposit with note `#ignore` → not credited to the member (alliance donation). Note `#loan repayment` → recorded, not credited.
* `/bank withdrawself amounts:"money=1000"` → confirmation screen first; only after **Confirm** does PnW move anything.
* `/bank reserve` then try `/bank withdrawself` for that amount → refused. `/bank release` to undo.

## 6. Recommended settings (Admin)
* `/bankset settransferlimit amount:500m` and `/bankset setdailylimit amount:1b` (limits use Current Market Value).
* `/bankset requireapproval dollars:1000000000` → ECON transfers worth more than $1B need a second Minister.
* `/bankset config` lists every other setting.

## If something looks wrong
`/ledger dashboard` shows the integrity state. If the books and PnW ever disagree, the bot raises an alert and can engage the
**Emergency Lock** (all financial changes halt). It never "fixes" balances by itself. Investigate with `/audit nation`, `/bank review`,
then close the event with `/ledger resolve` and lift the lock with `/ledger emergencylock state:OFF` (Admin).
