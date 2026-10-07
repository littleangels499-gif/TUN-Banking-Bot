# TUN Bank — development assessment (before coding) and status

Baseline reviewed: the ZIP as delivered. 151 tests passed on Python 3.12 before any change.

## 1. Already implemented (solid, reused unchanged)
Hash-chained append-only ledger with DB triggers as the guard; PnW record scanner/classifier (immutable, never deleted);
withdrawals with at-most-once send, holds, reconciliation and recovery; two-person approvals; limits in market value;
single `PriceService` with stored snapshots; reconciliation checks + emergency lock; config audit; encrypted member keys;
member-initiated deposits; offshore, grants, bulk, tax, exports, charts, backup/restore; additive migrations 001–006 with checksums and automatic pre-migration backup.

## 2. Partially implemented
* `#loan` is recognised and classified (`LOAN_REPAYMENT`, no credit) but nothing tracks a loan.
* Opening-balance import exists but: IDs only, positives only, no loan, one opening per nation/resource forever.
* Withdrawals already require each resource to be individually available; there is no net-worth rule.
* Valuation (`value_amounts`) already handles negative quantities arithmetically.
* Warchest/compliance: not started (only the LOCK mechanism exists).

## 3. What the four requests need
| Request | Needs |
|---|---|
| 1 Deposit reset | New formal event + records + new ledger entry type; Admin + typed phrase + button; refuses unless withdrawals paused. **DONE** |
| 2 Restoration spreadsheet | Names/IDs, consistency check, negatives, loan column, full preview, tied to the reset. **DONE** |
| 3 Negative balances + withdrawal limit | **DONE**: negatives in ledger/cache/import/adjustments/dashboard/valuation/reconciliation/exports; approved deduction (2-person) below zero; net-worth withdrawal rule (pre-check + enforced in the hold transaction). `LOAN_DEDUCTION` entry type is reserved for the loan module |
| 4 Conversion panel | **DONE** (ledger-only, as you specified). Migration 008, `conversion.py`, `/bankset conversionpanel`; see §6 |

## 4. Migrations
`007_negative_balances_reset_restore_loans.sql` (done, tested on a populated pre-007 database: identical hash chain, balances, row counts; triggers intact; rollback proven on failure).
* `balances`, `ledger_entries`, `opening_balance_rows` are **rebuilt** (SQLite cannot drop a CHECK in place). Every row is copied with its original id; the chain hash does not include the new column.
* New: `deposit_resets`, `deposit_reset_items`, `imported_loans`; `import_batches.kind/reset_id`; ledger entry types `RESET`, `RESTORE` (live) and `LOAN_DEDUCTION`, `CONVERSION` (reserved, blocked by the guard until enabled — avoids a second ledger rebuild later).
* Later migrations (008+) should be small: net-worth withdrawal config, conversion tables, loan module.

## 5. Conflicts with the existing ledger/security architecture (found and resolved)
1. **Reconciliation would have locked the bank.** `NEGATIVE_BALANCE` was CRITICAL and `auto_emergency_lock` is on, so the first negative balance would have frozen all money movement. Now only a negative LOCKED balance is critical.
2. **Debts must not net against other members' funds.** `bank_position` summed balances, so a member owing steel would have *reduced* member-held steel and inflated "alliance-owned" steel, letting ECON spend steel that belongs to other members. Now only positive balances count as claims on the bank; debts are reported separately (`owed_to_alliance`).
3. **OPENING is one-per-nation-forever**, which would block restoring any nation that already had an opening balance. Restores use their own `RESTORE` type tied to a reset.
4. `get_balances` hid anything `<= 0`; a negative balance would have been invisible. Now shown everywhere.
5. `apply_adjustment` refuses to remove more than is spendable — it must change for "approved deduction → negative" (Request 3, not yet done).

## 6. Conversion design (Request 4) — built as an ownership swap
The bot's client, and the public wrapper libraries, expose only two bank mutations: `bankDeposit` and `bankWithdraw`. I found no bank-level "convert resource" operation. Please also confirm in the PnW GraphQL playground.
An internal ledger swap (debit food, credit steel) would create steel the bank may not hold, i.e. a fake balance. The only honest design I see is an **ownership swap against alliance-owned stock**: the member's food becomes alliance-owned, an equal market value of alliance-owned steel becomes the member's, and it is allowed only if the live bank position shows enough alliance-owned steel. No PnW transaction, no new resources — but the alliance takes price risk and holds the inventory, so this is a policy decision for you before I build it.

## 7. Implementation order
1. Reset + restore + schema (done) → 2. Net-worth rule + approved negative deduction (done) → 3. Conversion (done) → 4. Loan module → 5. Warchest/compliance.

Note: `/deposit restore` was merged into the existing `/bankset importopening` (it detects a waiting reset). `/deposit` now only has `reset`.

## 8. Defaults chosen for Request 3 (change if you disagree)
* Net deposit worth **includes locked** funds (handover says to account for them); switch `net_worth_include_locked` to 0 for available-only.
* The limit applies to **self-withdrawals only**; ECON payments out of a member's deposit are not limited by it.
* A deduction that creates a debt needs two-person approval; a deduction within the balance does not (as before).

* Conversion has no fee/spread; the alliance carries price risk. A member can only receive what the alliance really owns (live bank check).
