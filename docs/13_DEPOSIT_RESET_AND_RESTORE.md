# 13 · Deposit reset, restoring balances, negative balances, withdrawal limit and conversion

Use this once, to replace the current member balances with the verified ones from Locutus.
Nothing is ever deleted: a reset is a **recorded ledger event**, and the restore is a **recorded import batch**.

## Before you start
1. `/bankset backup` — download a backup (the bot also makes one automatically before the database upgrade).
2. `/bank lock` (reason: "balance migration") — pauses withdrawals. `/deposit reset` refuses to run otherwise.
3. Let any in-flight withdrawals finish (`/audit transactions`, `/bank resolvetx`).
4. Export your Locutus balances to `.xlsx` or `.csv` (see "Spreadsheet format").

## Step 1 — `/deposit reset`  (Admin)
`/deposit reset reason:"…" confirm_phrase:RESET DEPOSITS`
* Shows a confirmation card: accounts, totals, value at current market prices. Press **Confirm**.
* Writes a permanent record: who, when, reason, every previous balance (member / resource), total value, price snapshot, reset ID.
* Brings every balance (available **and** locked) to zero with one `RESET` ledger entry per balance.
* Keeps: PnW records, transactions, audit log, configuration audit, migration history, reconciliation history, the old ledger.
* Refuses if: not an Admin, wrong phrase, withdrawals not paused, withdrawals in flight, emergency lock / open reconciliation problem,
  a previous reset is still waiting for its restore, prices unavailable (the value must be recorded).

## Step 2 — `/bankset importopening`  (Admin) — the same import command you already know
`/bankset importopening file:<spreadsheet> note:"Locutus export 2026-10-06"`
* There is only ONE import command. Because a reset is waiting, it automatically runs as a **restore** (negatives and loans allowed, tied to the reset). With no reset waiting it is the normal first-time opening import (positives only).
* The preview says which of the two it is.
* Reads the file and shows a **preview**: nations, positive deposits, negative balances, outstanding loans, net market value, errors, warnings. **Nothing changes until you press Confirm.**
* Any error blocks the whole import. Fix the file and upload again.
* Loads the balances as `RESTORE` ledger entries tied to the reset, and stores loans (see below).
* A reset can be restored **once**. Uploading the same file again, or any file for nations that already have imported balances, is refused, so nothing is double-credited.

Then run `/ledger reconcile`, check `/bank dashboard` for a few members, and `/bank unlock`.

> **Deposits made after the reset:** real deposits keep being credited while withdrawals are paused. If the spreadsheet already
> includes them, they would be counted twice. The preview warns you when balances exist at restore time.

## Spreadsheet format
One header row, one row per nation (a `resource` + `amount` layout also works).

| column | meaning |
|---|---|
| `nation_id` | PnW nation id (optional if `nation_name` is present) |
| `nation_name` | PnW nation name — **case-insensitive**; also accepted as `nation` |
| `money`, `food`, `coal`, `oil`, `uranium`, `iron`, `bauxite`, `lead`, `gasoline`, `munitions`, `steel`, `aluminum` | amounts; **negative values are allowed** |
| `loan` (or `outstanding_loan`, `loan_balance`) | outstanding loan in dollars — **not** a deposit |

* ID only, name only, or both are fine. If both are given they must agree, otherwise that row is blocked.
* A name that is not in the alliance, or matches two nations, blocks the import. Nothing is guessed.
* Negative amounts stay negative (e.g. steel −30,000). They reduce the member's net deposit worth.
* Blank = 0. Duplicate rows/nations, non-numbers and more than 2 decimals are errors.

## What happens to loans
Loan amounts are saved in `imported_loans` (status `PENDING_LOAN_MODULE`) with the batch, nation and amount, and written to the audit log.
They are **never** added to a deposit balance. When the loan module is built it will read this table.

## Database upgrade (migration 007)
Adds the reset/restore/loan tables and lets balances go negative. SQLite cannot change a CHECK rule in place, so `balances`,
`ledger_entries` and `opening_balance_rows` are rebuilt with every row copied unchanged (same ids; the hash chain is identical).
A verified backup is taken first; if anything fails the upgrade is rolled back.

---

# Negative balances and the withdrawal limit

## Negative balances
A negative resource (e.g. steel −20,000) is a real part of the member's balance: it is stored in the ledger, shown on the dashboard
and in exports, valued at market price, and **never clamped to zero**. It means the member owes that resource to the alliance.
* They arrive from a restore spreadsheet or an approved deduction.
* `/bank adjust remove:steel=30000` on a member holding 10,000 steel → −20,000. Because that creates a debt, it needs a **second
  Minister's approval** (same flow as adding funds): run it, a different Minister runs `/bank approve`, then run it again.
  It is refused while a pending withdrawal holds that resource.
* A member can never *withdraw* a resource they owe.
* A debt is reported separately in reconciliation (`owed_to_alliance`) and never reduces what the bank must hold for other members.

## Net deposit worth and the withdrawal limit
Net deposit worth = (available + locked − pending withdrawals), every resource at the current market price, negatives subtracting.
A member's own withdrawal (`/bank withdrawself`) is **rejected** if its market value is above that:

```
Withdrawal rejected.
Current net deposit worth: $75,000,000.00
Requested withdrawal value: $90,000,000.00
Maximum allowed: $75,000,000.00
```
* Checked twice: before the confirmation screen, and again inside the same database transaction that places the hold.
* Applies to member self-withdrawals only. ECON / alliance / grant / bulk payments are not affected.
* If a needed price is missing or unreliable the withdrawal is refused (resources are never valued at zero).
* Existing per-resource availability and transfer limits still apply on top.
* Settings (`/bankset config`): `net_worth_withdraw_limit` (1/0) and `net_worth_include_locked` (1 = locked funds count, 0 = available only).


---

# Resource conversion (members, inside their bank balance)

A member swaps one resource for another **inside their TUN Bank balance**. Nothing is sent or changed in-game.

**Admin: post the panel once** — `/bankset conversionpanel channel:#convert` (default: the current channel). It is a message with a
**Convert resources** button that keeps working after the bot restarts.

**Member:** press the button → choose the resource to convert → choose the resource to receive → enter an amount (`90m`, `1,000,000` or `all`)
→ check the quote (amounts, market value, price snapshot, prices used) → **Confirm**.

**How it works**
* Amount received = market value of what you convert ÷ current price of what you receive, **rounded down** to 0.01.
* It is an ownership swap with the alliance inside the same bank: your resource becomes alliance-owned and an equal value of
  alliance-owned stock becomes yours. No resource is created. So the member can only receive what the alliance **really owns right now**
  (checked against the live bank). If it doesn't, the member sees a neutral "can't be processed right now" message (no treasury details) and ECON is told the exact reason.
* Prices are fetched again at the moment of confirmation; if they moved, nothing is converted and a new quote is shown.
* Only AVAILABLE funds (minus pending withdrawals) can be converted. Locked funds and negative balances can't. Receiving a resource you owe just pays the debt down.
* Refused when: conversion is switched off, withdrawals are paused, the account is frozen, emergency lock / open reconciliation problem, prices missing/stale/unusual, over the cap.
* Every conversion is a permanent record (`conversions` table) with two ledger entries, the price snapshot and prices used, and an audit-log entry. ECON gets a log card.
* Settings (`/bankset config`): `conversion_enabled` (1/0), `conversion_max_value` (largest single conversion in $, 0 = no cap).
* There is no fee or spread. The alliance carries the price risk between conversions; if you want a spread, tell me and I'll add a setting.
