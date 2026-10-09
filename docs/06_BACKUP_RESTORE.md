# 6. Backup and restore

## What is backed up automatically
* **Daily**: one consistent backup per day in the Volume (`/data/backups/daily-YYYY-MM-DD.db`), newest 30 kept.
* **Before every database upgrade**: `pre-migration-....db`.
* **Before every restore**: `pre-restore-....db`.

These live in the same Volume, so they protect against mistakes but **not against losing the whole Railway project.**
Also keep your own off-site copies, using the commands below.

## Make a copy you can keep (recommended weekly, and before every update)
**In Discord (works for production):** run `/bankset backup` (Administrator). The bot sends you the `.db` file privately.
Save it to a safe place (cloud drive / another disk). It contains every balance, so treat it like a password.

**On your PC:** `python scripts/backup_local.py` → saved in `data/backups/`.

## Restore a backup

### Production (Railway) — from Discord
1. Run `/bankset restorestage` and attach the backup file.
2. The bot checks the file thoroughly (not damaged, is a TUN Bank database, history chains intact, not from a newer bot).
   If anything is wrong it says so and changes nothing.
3. Press **Confirm**, then in Railway: **Deployments → ⋯ → Restart**.
4. On start-up the bot saves your current database as `pre-restore-....db`, swaps in the backup, and starts.
5. Afterwards run `/ledger reconcile` and `/bank review`. Anything that happened *after* the backup was made (new deposits,
   withdrawals) is not in the restored books: deposits are re-detected from PnW automatically; withdrawals sent after the backup
   will show up as "outside TUN Bank" for ECON to review.

### On your PC
1. **Stop the bot** (Ctrl + C).
2. `python scripts/restore_local.py path\to\backup.db`
3. Start the bot again (`python main.py`).

## Never do this
* Don't edit the `.db` file with other programs, and don't copy a `.db` file over the live one while the bot is running.
* Don't delete the Volume.
