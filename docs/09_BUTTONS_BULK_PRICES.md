# 9. Buttons, bulk transfers and prices

## Buttons you will see
* **Your account** (`/bank dashboard`, `/bank balance`): 💸 *Withdraw* (opens a small form: type `money=1m coal=5000`), 💵 *Withdraw all cash*,
  📜 *History*, 📊 *Chart*, 📥 *How to deposit* (with a "Check my deposit now" button), 🔄 *Refresh*.
  Every withdrawal still shows the confirmation screen first; nothing is sent until you press **Confirm**.
* **Vault** (`/bank holdings`): Chart, Reconcile now, Review queue, Export balances, Refresh.
* **One member** (`/audit nation`): charts, *Reserve funds…*, *Freeze / Unfreeze*.
* **Review queue** and **Approvals**: one item per page with ✅ / 🛡️ / 🗑️ or Approve / Revoke buttons.
* **Integrity** (`/ledger dashboard`): Reconcile now, Vault, Review queue, Emergency lock.
Buttons only work for the person who opened the screen, and they check permissions again when pressed.

## Bulk transfers (pay many nations at once)
1. `/bulk template` downloads an example file. Fill it in: one row per nation. `nation` can be an id, a name or a link;
   then one column per resource (`money`, `coal`, `oil`, ...) and an optional `note`.
2. `/bulk send file:<your file> reason:<why>`
3. The bot checks **everything first**: every nation must be in the alliance, no negatives or duplicates, enough
   **alliance-owned** funds (bulk never touches members' deposits), your limits, and the second-approver rule for big totals.
   If anything is wrong it lists every problem and sends nothing.
4. You see the preview with the total **Current Market Value** and press **Confirm**.
5. Payments go out one at a time. Each one is recorded with its PnW transaction id. A failure on one row does not
   stop the others; but the batch stops if the bank is locked/paused, a transfer can't be confirmed, or 3 fail in a row.
6. `/bulk status` shows results; `/bulk resume batch_id:<n>` continues a stopped batch. **Rows already sent are never repeated.**
   Sending the exact same file twice within 24 hours is refused.

## Prices
`/prices` shows the market price of every resource that all values are based on. If the bot can't load prices from
Politics & War, `/prices` shows the real reason. Run `python scripts/check_pnw.py` on your PC to test every PnW connection.
