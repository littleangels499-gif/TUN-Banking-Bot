# 21. Update notes: funded credits, external withdrawals, DMs, reconciliation gate, settings tools

## What is new
| Area | What it does |
|---|---|
| **Bug fix** | The nation-name refresh loop crashed every cycle (`AttributeError: 'TunBankBot' object has no attribute 'pnw'`). Fixed. A test now catches this whole class of bug. |
| **Deposit failures are diagnosable** | Every reason a Discord deposit is refused (bank-side block, missing key, PnW refusal) is now logged on the server (keys redacted). The common cause of "error although my key is set" is the reconciliation gate below. |
| **`/bankset gate on/off/show`** | Reconciliation gate. **ON (default, unchanged):** an open known deficit/ownership shortfall blocks banking. **OFF:** those findings are still detected, reported and audited but do not block. Always on regardless: emergency lock, ECON pause, hash-chain and balance-integrity checks, uncertain/stuck PnW transfers, duplicate protection, limits, approvals, permissions. Dashboards and reports show the gate state and a new `DISCREPANCY · NOT BLOCKING` status. |
| **`/bank credit`** (Minister) | A *funded* credit: the amount comes from **alliance funds**, **another member**, or **another alliance's ownership account**, and both sides are recorded (journal + ledger). Second approver required; insufficient source refused; only an Admin can override a deficit with a written reason (flagged everywhere); an ownership share can never go below zero; never creates an account; idempotent; never takes funds held for a pending withdrawal. **`/bank adjust` can no longer ADD money** (that created funds from nothing); it still removes. |
| **External withdrawals** | Money leaving the **offshore** for a **linked member who is a current alliance member**, without TUN Bank, is verified against the real PnW record, processed once, and deducted from that member (the balance may go negative, recorded as a real debt). Anything uncertain (note mentions grant/payment/etc., exempt nation, unlinked, not in the alliance, member list unavailable, emergency lock) goes to `/bank review`, where staff can **Charge to member**. Shared offshore: the host share is debited; payouts to another registered alliance or to non-members are never charged to a member. Shown everywhere as **External withdrawal** (history, audit view, Excel export has a dedicated sheet and the stored type). |
| **Member DMs** | After a *recorded* withdrawal, adjustment, credit or external withdrawal the member gets a DM: resources, before → after balance, reason, reference, and whether it went through TUN Bank or was detected externally. The DM is queued in the same database transaction and sent afterwards: a failed DM never touches the transaction, and failures stay listed for staff (`member_notifications`, status `FAILED`). |
| **`/bankset config`** | Plain-language pages with labels and readable values (✅ On, 2 minutes, $2,000,000,000, #channel), ✏️ marks what you changed, search the setting by name, input is validated (typos are refused). Raw setting codes still work. |
| **`/bankset resetconfig`** (Admin or server owner) | Resets **configuration only**: *Settings only* or *All configuration* (+ roles, bankers, limits, trade lists). Typed phrase + confirmation + automatic backup + previous values written to the audit log. Never touches members, links, API keys, balances, ledgers, transactions, loans, audit history or the emergency lock; if any protected record would change the whole reset is rolled back. |
| **Icons** | `scripts/install_pnw_icons.py` + `/bankset iconpack` use the real PnW icons as application emojis, with automatic fallback to the defaults (guide 19). |
| **Backups / migration** | `scripts/verify_database.py` proves a backup is healthy and that a copy on another Railway account holds the same data (guide 18). |
| **Guild lock** | With `GUILD_ID` set, commands from any other Discord server are refused. |
| **Start-up check** | `python scripts/check_startup.py` builds the real bot offline and validates every command with discord.py. |

## New commands
`/bank credit` · `/bankset gate` · `/bankset resetconfig` · `/bankset iconpack` · `/bank review` has a new action *withdrawal* · `/bankset config` has new options (`setting`, `value`, `category`).
`/bankset` is now at Discord's limit of 25 subcommands: the next admin command needs a new group.

## New settings (all have safe defaults; nothing to do unless you want to change them)
`reconcile_gate_enabled` (1) · `external_withdrawal_mode` (auto | review | off) · `external_withdrawal_exclude_words` · `external_withdrawal_exempt_nations`

## Migrations (additive, automatic, with an automatic pre-migration backup)
`014_balance_transfers.sql` · `015_external_withdrawals_notifications.sql`. No existing table, row, trigger or migration is modified.

## Environment variables
**None added.** `requirements.txt`: `discord.py>=2.5` (needed for application emojis). Railway installs it automatically; locally run `pip install -r requirements.txt`.

## Applying the update
**Replace** everything in the project folder with the contents of the new zip (code, `docs/`, `scripts/`, `tests/`, `icons/`, `requirements.txt`).
**Preserve (the zip contains none of these, so extracting over the folder cannot overwrite them):** `.env`, `data/` (the database `tunbank.db`, its backups), `.venv/`.
Then: `pip install -r requirements.txt` → `python -m unittest discover -s tests` → `python scripts/check_startup.py` → commit/push → watch the Railway logs for `Database upgraded: 014_..., 015_...` and `Logged in as`.
Finish in Discord: `/ledger dashboard` (state NORMAL, chains intact).
The database in your **production Volume** is never touched by the zip; the new bot upgrades it itself on start-up, after backing it up.

## Things to know / limitations
* **Multi-alliance support is NOT implemented.** See guide 20 for the assessment, options and plan, and the decisions I need.
* External-withdrawal detection covers the **offshore** only (as specified). Grants/payments are recognised by the note words and the exempt list, which you can tune. When unsure it asks; it does not guess.
* Funded credits whose source is *alliance funds* need the real bank to be readable; if PnW is unreachable they are refused (or need the explicit admin override).
* The upgrade from an older database has been tested on a copy of your 006-level test database (all migrations apply, history and balances identical). The practical Discord and PnW behaviour (real DMs, real PnW records, real icons) is for you to confirm; the code paths are covered by automated tests with simulated Discord and PnW.
* The PnW icon download URL in `install_pnw_icons.py` is best-effort and untested; saving the PNGs by hand always works.
