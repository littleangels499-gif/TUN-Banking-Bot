# 2. Keys and the `.env` file

The `.env` file holds your secrets. **Never post it, never upload it.** It is already in `.gitignore`.

## A. Create the Discord bot
1. Open https://discord.com/developers/applications → **New Application** → name it "TUN Bank".
2. Left menu **Bot** → **Reset Token** → **Copy**. This is your `DISCORD_TOKEN`. (You can't see it again; reset if lost.)
3. You do **not** need any "Privileged Gateway Intents".
4. Left menu **OAuth2 → URL Generator**: tick scopes **bot** and **applications.commands**.
   Under *Bot Permissions* tick: **View Channels, Send Messages, Embed Links, Attach Files**.
5. Open the generated URL at the bottom, pick your server, **Authorize**.

## B. Find your Discord IDs
Discord → **Settings → Advanced → Developer Mode ON**.
* Right-click **your name** → *Copy User ID* → `OWNER_DISCORD_IDS`. (Owners automatically get Administrator rights in the bot so you can set it up.)
* Right-click **your server icon** → *Copy Server ID* → `GUILD_ID` (recommended: commands then appear instantly).

## C. Politics & War
* `ALLIANCE_ID`: the number at the end of your alliance page URL.
* `PNW_API_KEY`: your PnW account page → API key. The nation must be in the alliance with permission to **view the bank**.
* `PNW_BOT_KEY`: the PnW *bot key* (needed to send bank withdrawals through the API). Only a nation with bank-withdraw
  permission in your alliance can use it. If the bot key belongs to a different nation than the API key, put that nation's API key in `PNW_BOT_KEY_API_KEY`.

## D. Create the file
1. In VS Code's left file list, find `.env.example`. Right-click → **Copy**, then right-click in the empty area → **Paste**,
   and rename the copy to exactly `.env`.
2. Open `.env` and replace every `paste-...` value. Leave `DATA_DIR` empty.
3. Save.

Check it: `python scripts/check_setup.py` — it tells you in plain English what is missing.

Next: [3. Run in VS Code](03_RUN_IN_VSCODE.md)
