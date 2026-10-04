# 12. Depositing from Discord, and the configuration audit log

## Deposits started from Discord
Members can deposit from Discord when they have saved their **own** PnW API key:
1. `/nation link` (as before), then `/nation setkey`: a private form asks for the API key. It is checked against PnW (it must be *your*
   nation's key), stored **encrypted**, and only its last 4 characters are ever shown.
2. In Politics & War: **Account page → switch "Whitelisted access" ON.** (Without it PnW refuses deposits started by a bot.)
3. `/bank deposit amounts:"money=5m"` (or the **Deposit from Discord…** button) → a confirmation screen → the bot asks PnW to deposit from
   **your** nation into the TUN bank, using your key and the bot's verified key.
4. **Nothing is credited at that moment.** The balance changes only when the real PnW bank record appears. The record id is stored with the
   deposit (`TUN-DEP<number>` in the note ties them together). PnW refusing = no change; an unclear answer = never re-sent, the bank
   records settle it.

Safety: your key is used only for your nation, only after you press Confirm, never shown, never written to logs, and deleted if an Admin relinks
your nation or you run `/nation removekey`. A key that turns out to belong to a different nation is switched off and ECON is told.
If you ever suspect your key leaked, make a new key in PnW.

**Admin setup:** run `python scripts/make_credential_key.py`, put the printed `CREDENTIAL_ENCRYPTION_KEY=...` line in `.env` and Railway Variables.
Direct deposits also need a verified bot key (your `OFFSHORE_BOT_KEY` or `MAIN_BOT_KEY`). Switch the feature off with `/bankset config member_deposit_enabled 0`.

### One thing to test first (a $1 experiment)
PnW's documentation is ambiguous about whether a verified bot key may be combined with *another* nation's API key (the way Locutus does).
Run `python scripts/test_member_deposit.py` with your own main nation's API key. It tries a $1 deposit and shows PnW's exact answer:
success means member deposits work; a refusal tells you why (usually Whitelisted access is off). If PnW refuses it outright, the bot simply keeps
offering the manual steps; nothing else changes.

## Deposit notes
Money from a member is a **normal deposit** whatever the note says (no note, `#deposit`, "my savings", ...). Only `#loan` (loan repayment) and
`#ignore` (alliance donation) differ, plus PnW's own tax records, plus any tags an Admin lists in `system_tags`.

## The configuration audit log
Every change to a financial setting, permission, limit, link, lock, manual adjustment, manual classification, opening-balance import,
tax policy, key and restore is written permanently the moment it happens: **who** (name + Discord ID), **what**, **previous value**, **new value**,
**when**, **which command or button**, and **what it affected**. Entries can't be edited or deleted.
* Private channel: `/bankset setlogchannel channel:#config-audit kind:Configuration audit`. Until it is set, entries wait in the database and
  ECON is warned once; they are posted when the channel is set (and retried if Discord is down).
* Read it later: `/audit configlog` (Admin; optional `setting:` filter) or `/bank records kind:configaudit` for Excel.
* Changes made outside the bot (your `.env` / Railway variables) are noticed at the next start and logged; keys appear only as a short fingerprint.
