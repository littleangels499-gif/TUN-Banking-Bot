# 14 · Loans

A loan is money a member **owes the alliance**. It is kept apart from the deposit and is never a deposit.

## How a loan works
* **Principal + flat interest.** When a loan is recorded the interest is fixed: `interest = amount × rate%`. Nothing accrues in the background,
  so what is owed is always `principal + interest − paid − written off`. (If you want interest that grows daily, tell me.)
* **Repayments** (a real PnW payment with `#loan` in the note, e.g. `#loan repayment`) are applied **interest first, then principal**, oldest loan first.
  Anything **more than is owed** becomes the member's normal deposit. A repayment never touches the deposit otherwise.
* **Overdue:** once the due date has passed with something still owed, ECON gets one alert. **Nothing is ever taken automatically.**
* Every step is a permanent record (`loan_events`); loan amounts can't be edited or deleted. Reconciliation checks the loan books against that history.

## Commands
| Command | Who | What |
|---|---|---|
| `/loan add nation amount note [interest_percent] [due_days]` | Minister | Record a loan you gave. **Does not send money** — send it with the normal bank tools first. |
| `/loan adopt [due_days]` | Admin | Turn the loans carried in by a restore spreadsheet (guide 13) into real loans. No interest is added. |
| `/loan list` · `/loan view nation` | Auditor+ | Active loans, overdue first · one member's loans and history. |
| `/loan mine` | Member | Your own loans only. The loan also shows on `/bank dashboard`, separate from your deposit. |
| `/loan deduct nation amount note` | Minister | Pay a loan out of the member's **available** cash (locked funds and pending withdrawals are never used). |
| `/loan writeoff loan_id reason` | Admin | Forgive what is left of a loan (permanent, needs a reason). |
| `/loan applypayment record` | Banker | Apply a stored `#loan` PnW record that wasn't applied automatically (e.g. during an emergency lock). |

## Automatic repayments
When the scanner sees a new `#loan` payment it applies it straight away and tells ECON what happened. It does **not** apply history found on the very first scan.
If the member has no active loan, nothing is applied **and nothing is credited** (so a payment can never turn into free deposit by mistake); ECON is told.
Only the **money** part is applied; other resources in the same record are reported but not used.

## Migrating Locutus loans
1. Put the loan amounts in the `loan` column of the restore spreadsheet (guide 13). They are saved safely with the import.
2. After the restore, run `/loan adopt`. Each becomes an active loan with the outstanding amount.
