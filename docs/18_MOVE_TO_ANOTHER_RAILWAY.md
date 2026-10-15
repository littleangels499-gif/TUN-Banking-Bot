# 18. Backups, and moving to another Railway account

## What a backup contains
`/bankset backup` (and the daily / pre-migration / pre-restore copies) is a **complete, consistent copy of the whole database**:

| Included | |
|---|---|
| Members and links | every member account, Discord ↔ nation link, link history, cached nation names, **members' API keys (stored encrypted)** |
| Money | balances, the complete ledger with its hash chain, locks, adjustments, funded credits (`/bank credit`), external withdrawals, conversions, opening-balance imports, deposit resets |
| Transactions | withdrawals, grants, bulk batches, member deposits, offshore transfers, the raw PnW bank records, price snapshots |
| Loans | loans, loan events, recoveries, imported loans |
| Shared offshore | registered alliances, ownership balances and the ownership ledger |
| History | the audit log (hash-chained), configuration audit, integrity findings, approvals, reconciliation runs, notifications and the alert log |
| Settings | every bank setting, role → permission mappings, named bankers, limits, trade-monitor lists, tax brackets/exemptions, system state (emergency lock, pause) |

**NOT in a backup** (keep these separately and safely):

| Not included | Why / what to do |
|---|---|
| `.env` / Railway **Variables** | Discord token, PnW keys, bot keys, owner IDs, `GUILD_ID`. Copy them from Railway → Variables (raw editor) into a password manager. **Never paste them into chat.** |
| **`CREDENTIAL_ENCRYPTION_KEY`** | The key that encrypts members' API keys. The backup holds the *encrypted* keys. **With a different key none of them can be read** and every member must run `/nation setkey` again. Keep the original. |
| The code | It is on GitHub / your zip. |
| Discord-side things | The posted panel message, and PnW icons uploaded to the bot application. They belong to the Discord bot application: if you keep the **same Discord application and token** they stay. A new application needs `scripts/install_pnw_icons.py` again. |
| Files in the `backups/` folder | Only the live database is copied by `/bankset backup`. Download the backups you want to keep. |

## Moving to another Railway account, step by step
The rule that keeps money safe: **never have two copies of the bot running against the same Discord token.** Two bots would each scan PnW and
diverge. So: pause → final backup → stop the old one → start the new one.

**A. Prepare (old Railway, bot still running)**
1. Write down / export every Variable (Railway → Variables → *Raw editor*). Make sure you have `CREDENTIAL_ENCRYPTION_KEY`.
2. In Discord: `/bank lock` (pause withdrawals), then `/ledger dashboard`. Wait until there are **no in-flight transactions** and the state is as expected.
3. `/bankset backup` → save the `.db` file (call it `old.db`).
4. On your PC: `python scripts/verify_database.py old.db --credentials` (set `CREDENTIAL_ENCRYPTION_KEY` in your `.env` first).
   You want `RESULT : PASS` and every member key `readable`. Note the `FINGERPRINT`.

**B. Stop the old bot.** Railway (old account) → the service → *Settings* → remove/stop it. (Do not delete the old Volume yet.)

**C. Create the new deployment (new Railway account)**
1. New project from the same GitHub repo / code. Add a **Volume** mounted at `/data`. Do not set `DATA_DIR` unless you know why.
2. Add the **same Variables**, including the **same `CREDENTIAL_ENCRYPTION_KEY`**.
3. Deploy. The bot starts with an empty database. (Check the logs for `Logged in as ...`.)
4. In Discord: `/bankset restorestage` and attach `old.db`. The bot validates it fully (not damaged, is a TUN Bank database, both history chains intact, not from a newer bot). Press **Confirm**.
5. Railway → *Deployments → ⋯ → Restart*. At start-up the bot saves the empty database as `pre-restore-….db`, swaps in your backup and applies any newer migrations (it makes a `pre-migration` backup first).

**D. Verify BEFORE you unlock anything**
1. `/ledger dashboard` → state, both chains `intact`.
2. `/bankset backup` on the **new** bot → save as `new.db`.
3. On your PC: `python scripts/verify_database.py old.db --compare new.db --credentials`
   * **IDENTICAL** → exactly the same data.
   * **COMPATIBLE** → everything from the old database is present and unchanged (every ledger and audit entry, all settings, roles, bankers and limits) and the new bot only added newer activity (it scans PnW every couple of minutes). This is the normal result if a few minutes passed.
   * **DIFFERENT** → **do not switch.** Read the reasons it lists and keep using the old bot (nothing has been lost; the old Volume is still there).
4. Check `/bankset config` (settings), `/bank balance` for a member, `/loan list`, and that members' API keys work (`readable` above).
5. When happy: `/bank unlock`. Then, after a few days of normal running, you may delete the old project.

**Roll back at any time before step D5** by simply turning the old bot back on.

## Making the verification part of routine
* Every `/bankset backup` is a valid full copy. Run `python scripts/verify_database.py backup.db` now and then: it proves the copy is healthy (integrity, chains, balances = ledger) without touching anything.
* The script only **reads** files and never prints a secret (only counts).
