# TUN Bank — Discord banking bot for Politics & War

A Discord bot that keeps an **auditable ledger** of member deposits on top of your real PnW alliance bank.
Politics & War is always the source of truth: the bot never creates money. Every balance is explained by
either a real PnW bank record or a documented opening balance.

**Read the guides in order (each one is short and written for beginners):**

| # | Guide | What it covers |
|---|-------|----------------|
| 1 | [docs/01_INSTALL.md](docs/01_INSTALL.md) | Install Python + VS Code extras + the bot's dependencies |
| 2 | [docs/02_SETUP_ENV.md](docs/02_SETUP_ENV.md) | Discord bot, PnW keys, and the `.env` file |
| 3 | [docs/03_RUN_IN_VSCODE.md](docs/03_RUN_IN_VSCODE.md) | Run and test the bot on your PC |
| 4 | [docs/04_GITHUB_AND_RAILWAY.md](docs/04_GITHUB_AND_RAILWAY.md) | Put it on GitHub and host it on Railway (with a permanent Volume) |
| 5 | [docs/05_UPDATING.md](docs/05_UPDATING.md) | Safely update later — the database and `.env` are never overwritten |
| 6 | [docs/06_BACKUP_RESTORE.md](docs/06_BACKUP_RESTORE.md) | Back up and restore the database |
| 7 | [docs/07_FIRST_TIME_IN_DISCORD.md](docs/07_FIRST_TIME_IN_DISCORD.md) | Roles, log channel, importing opening balances, first tests |
| 8 | [docs/08_COMMANDS_AND_STATUS.md](docs/08_COMMANDS_AND_STATUS.md) | Command list and exactly what is / isn't built yet |
| 9 | [docs/09_BUTTONS_BULK_PRICES.md](docs/09_BUTTONS_BULK_PRICES.md) | Buttons, bulk transfers, market prices |
| 10 | [docs/10_OFFSHORE.md](docs/10_OFFSHORE.md) | Offshore bank, which PnW credentials are needed, grants |
| 12 | [docs/12_MEMBER_DEPOSITS_AND_AUDIT.md](docs/12_MEMBER_DEPOSITS_AND_AUDIT.md) | Depositing from Discord, deposit notes, the configuration audit log |
| 11 | [docs/11_CONFIDENTIALITY_TAX_LINKING.md](docs/11_CONFIDENTIALITY_TAX_LINKING.md) | Who sees what, tax alerts, linking, deposits |
| 16 | [docs/16_PANEL_AND_TRADE_MONITOR.md](docs/16_PANEL_AND_TRADE_MONITOR.md) | The TUN Bank panel (buttons) and trade monitoring |
| 15 | [docs/15_RECONCILIATION.md](docs/15_RECONCILIATION.md) | Reconciliation statuses: resource level, net position, liquidity |
| 14 | [docs/14_LOANS.md](docs/14_LOANS.md) | Loans: record, repay (#loan), deduct, write off, overdue alerts |
| 13 | [docs/13_DEPOSIT_RESET_AND_RESTORE.md](docs/13_DEPOSIT_RESET_AND_RESTORE.md) | Reset member balances and restore them from the Locutus spreadsheet |

## Where things live (important)
* **Code** → GitHub (this folder). Updating code never touches your data.
* **Secrets** → the `.env` file on your PC / the *Variables* tab on Railway. `.env` is in `.gitignore`, so it is never uploaded.
* **Database** → `data/tunbank.db` on your PC / the Railway **Volume** in production. `data/` is in `.gitignore`.

## Quick check that everything is in place
```
python scripts/check_setup.py
python -m unittest discover -s tests
```
