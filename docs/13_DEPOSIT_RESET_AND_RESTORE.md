# 13 · Deposit reset and restoring balances from the Locutus spreadsheet

Use this once, to replace the current member balances with the verified ones from Locutus.
Nothing is ever deleted: a reset is a **recorded ledger event**, and the restore is a **recorded import batch**.

## Before you start
1. `/bankset backup` — download a backup (the bot also makes one automatically before the database upgrade).
2. `/bank lock` (reason: "balance migration") — pauses withdrawals. `/deposit reset` refuses to run otherwise.
3. Let any in-flight withdrawals finish (`/bank transactions`, `/bank resolvetx`).
4. Export your Locutus balances to `.xlsx` or `.csv` (see "Spreadsheet format").

## Step 1 — `/deposit reset`  (Admin)
`/deposit reset reason:"…" confirm_phrase:RESET DEPOSITS`
* Shows a confirmation card: accounts, totals, value at current market prices. Press **Confirm**.
* Writes a permanent record: who, when, reason, every previous balance (member / resource), total value, price snapshot, reset ID.
* Brings every balance (available **and** locked) to zero with one `RESET` ledger entry per balance.
* Keeps: PnW records, transactions, audit log, configuration audit, migration history, reconciliation history, the old ledger.
* Refuses if: not an Admin, wrong phrase, withdrawals not paused, withdrawals in flight, emergency lock / open reconciliation problem,
  a previous reset is still waiting for its restore, prices unavailable (the value must be recorded).

## Step 2 — `/deposit restore`  (Admin)
`/deposit restore file:<spreadsheet> note:"Locutus export 2026-10-06"`
* Reads the file and shows a **preview**: nations, positive deposits, negative balances, outstanding loans, net market value, errors, warnings. **Nothing changes until you press Confirm.**
* Any error blocks the whole import. Fix the file and upload again.
* Loads the balances as `RESTORE` ledger entries tied to the reset, and stores loans (see below).
* A reset can be restored **once**.

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

## Plain `/bankset importopening`
Still works for first-time opening balances, and now also understands nation names and a loan column. It still refuses negative amounts
(negatives are only loaded by a restore after a reset) and never overwrites an existing opening balance.

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
