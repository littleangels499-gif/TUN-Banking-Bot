# 15 · Reconciliation: what the statuses mean and what to do

Reconciliation compares three things and **never rewrites a balance** to make numbers agree. Run it with `/bank reconcile` or `/ledger reconcile`;
the bot also runs it every minute.

## The three questions
1. **Resource level** – for each resource: what the PnW bank holds, what members' balances add up to (negative balances subtract), and the
   difference (`bank − members`). A negative difference means the bank holds *less* than members' net balances.
2. **Overall net market position** – every difference valued at current market prices (cash at face value). Members who owe a resource and
   alliance surplus in another resource are therefore counted together.
3. **Liquidity** – decided separately for every withdrawal: it is refused when the bank that pays does not physically hold the requested
   resource. A shortage of one resource never stops withdrawals of another.

## Statuses
| | Status | When | Effect |
|---|---|---|---|
| 🟢 | **NORMAL** | Everything agrees | – |
| 🟡 | **WARNING** | One or more resources are short in the bank, but the overall net position is not negative, and no integrity problem exists | Banking continues. Only a withdrawal of the short resource is refused. |
| 🟠 | **RECONCILIATION REQUIRED** | The overall net position is **negative** (members' net balances are worth more than the bank holds), or another unexplained discrepancy (stuck transaction, mass balance change…) | Withdrawals, conversions and imports are paused until it clears or ECON resolves it. **No emergency lock.** |
| 🔴 | **EMERGENCY LOCK** | A real integrity failure: broken hash chain, a balance changed without a ledger entry, a PnW record credited twice, an import that no longer matches its original, lost history | All financial changes halted. |

Position findings (`Resource shortfall`, `Net position shortfall`) **clear themselves** on the next run once the numbers agree.
If the PnW bank can't be read, nothing is cleared or invented. If a price is missing the net position is shown as *unavailable* – it is never guessed.

## Reading the report
```
TUN BANK RECONCILIATION        🟡 WARNING
Cash   · Bank $96.00K · Members $100.00K · Difference -$4.00K ⚠️
Steel  · Bank 0 · Members -500 (net of 500 owed to the alliance) · Difference +500 ✓
Food   · Bank 2K · Members 800 · Difference +1.2K ✓
Overall net market position: $900.00 ✓ positive
```
Staff without the `bank_view_alliance_holdings` permission see the status and which resources are short, but not the amounts.

## What to do
* **WARNING** – nothing is needed to keep banking running. To clear it: check `/bank review` for deposits not yet credited, move the short
  resource into the bank, or correct members' balances with `/bank adjust` if they are wrong.
* **RECONCILIATION REQUIRED** – `/bank review`, then `/bank records balances` to compare, fix the cause, run `/bank reconcile` again.
* **EMERGENCY LOCK** – `/ledger dashboard` shows why. Resolve the events with `/ledger resolve`, then lift the lock with `/bank emergency`.

## Setting
`/bankset config recon_net_tolerance` – overall net shortfall (in $) tolerated before RECONCILIATION REQUIRED. Default `0` (strictest).

## Upgrading from the earlier rule
Earlier versions raised a critical `LEDGER_EXCEEDS_BANK` for any single-resource shortfall and locked the bank automatically. On the first run
after the update that event is closed, and **if that was the only reason for the lock it is lifted automatically** (recorded in the audit
log). A lock set by hand, or one with any other critical cause, is never lifted by reconciliation.
