# 4. GitHub + Railway (production hosting)

## A. Put the code on GitHub (first time)
1. Create a **private** repository on github.com (e.g. `tunbank-bot`). Do not add a README/.gitignore there.
2. In VS Code click the **Source Control** icon (branch symbol, left bar) → **Initialize Repository** (or *Publish to GitHub*).
3. Before committing, look at the list of changed files. You must **NOT** see `.env` or anything in `data/` — they are
   ignored automatically. If you do see them, stop and ask for help.
4. Type a message like `first version`, click **Commit**, then **Publish Branch / Sync**.

## B. Create the Railway service
1. https://railway.app → **New Project → Deploy from GitHub repo** → pick `tunbank-bot`.
2. Railway builds it automatically (it reads `requirements.txt`, `.python-version` and `railway.json`).
   The first build will stop with a setup message until you finish steps C and D — that's expected and safe.

## C. Add the Volume (this is what keeps your database forever) — DO THIS BEFORE ANYTHING ELSE
1. Open your project canvas → click your service → **Volumes** tab (or right-click the canvas → *Add Volume*).
2. Create a volume and **attach it to the bot service**.
3. Set the **mount path** to exactly: `/data`
4. That's it. The bot detects the volume automatically and stores `tunbank.db` and its daily backups inside it.

**Built-in protection:** if the bot ever starts on Railway *without* a volume, it refuses to run and tells you why, so
your database can never be silently erased on a redeploy.

## D. Add the variables (your `.env` on Railway)
Service → **Variables** tab → add each of these (same values as your `.env`):
`DISCORD_TOKEN`, `OWNER_DISCORD_IDS`, `GUILD_ID`, `ALLIANCE_ID`, `PNW_API_KEY`, `PNW_BOT_KEY`
(and `PNW_BOT_KEY_API_KEY` only if you use it). **Do not add `DATA_DIR`.**

Then **Deployments → Deploy** (or push a commit). In the logs you should see `Database file: /data/tunbank.db` and `Logged in as ...`.

## E. Keep it to ONE instance
Leave replicas at **1** (already set in `railway.json`). Two copies of the bot on one database would be dangerous.

## Checklist
- [ ] Volume attached, mount path `/data`
- [ ] Variables filled in
- [ ] Logs show `Database file: /data/tunbank.db`
- [ ] Your PC copy of the bot is stopped (or uses a different test token)

Next: [5. Updating safely](05_UPDATING.md)
