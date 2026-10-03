# 5. Updating the bot safely

## Why an update can never overwrite your data
* The database lives in the Railway **Volume** (`/data`) — it is not part of the code on GitHub.
* `.env` and `data/` are in `.gitignore`, so they are never uploaded or replaced.
* When the new version starts, it makes an automatic **pre-migration backup**, then applies any database changes
  inside a single all-or-nothing step. Old migration files are checksum-protected: an edited old migration is refused.
* The bot refuses to run an **older** version against a **newer** database (it says so and stops).

## Update steps
1. On your PC, replace the changed code files (or unzip the new version over the folder). **Do not delete `.env` or `data/`.**
2. If `requirements.txt` changed (this update added charts), run `pip install -r requirements.txt` again in the VS Code terminal.
3. Run the tests: `python -m unittest discover -s tests` — every line should end in `OK`.
4. VS Code → **Source Control** → check that `.env` / `data/` are NOT in the list → write a message → **Commit** → **Sync Changes**.
5. Railway notices the push and redeploys automatically. Watch **Deployments → View logs**:
   look for `Database upgraded: 002_...` (only if the update changed the database) and `Logged in as ...`.
6. In Discord run `/ledger dashboard` — it should say `NORMAL` and both chains `intact`.

## If something goes wrong
* Railway → **Deployments** → click the previous working deployment → **Redeploy** (code only; data is untouched).
* If the database itself needs to go back: [6. Backup & restore](06_BACKUP_RESTORE.md) (a `pre-migration-...db` backup was made for you).
