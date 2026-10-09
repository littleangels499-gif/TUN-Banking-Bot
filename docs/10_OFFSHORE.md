> **Several alliances share your offshore?** See guide 17 (shared offshore). This guide describes the basic main → offshore transfer.

# 10. Offshore, credentials and grants

## The flow
**Member deposit → main TUN bank → (ECON moves funds) → offshore bank → member withdrawal**

* Deposits are detected in the **main** bank (as before).
* **Member withdrawals and payments are sent from the offshore bank.** If the offshore doesn't physically hold enough, the
  bot refuses *before* sending and tells you to move funds with `/bank offshore`.
* `/bank offshore money=5b ...` moves funds from main to offshore. It changes **where money sits only**. No member balance,
  lock, tax record or `#ignore` donation is touched. Every move is tied to a real PnW bank record.
* `/bank offshore` with nothing typed shows both banks, how transfers are sent, who may use it, and recent transfers.
* Who may use it: `/bankset config key:offshore_access value:STAFF` (default: Banker and above), `ADMIN`, or `MEMBERS`.
  `offshore_keep_in_main` (e.g. `money=100m`) keeps a reserve that can never be moved out of the main bank.
* The vault (`/bank holdings`) and every reconciliation use **both banks combined** to prove members' balances are covered.

## Which credentials are needed (checked against PnW's API documentation)
What PnW's documentation says, in plain words:
1. A withdrawal needs the `X-Bot-Key` **and** the `X-Api-Key` of **the account linked to that bot key**.
2. **The nation that performs the action is the nation that owns the API key.** A bot key is linked to one account.
3. The withdrawal request has **no "from" field**: money leaves the bank of the alliance that nation belongs to.
4. That nation must have bank-withdraw permission in its alliance, and "whitelisted access" switched on in its account page.

What that means for you:

| What you want | Whose credentials it needs | Do you have it? |
|---|---|---|
| Pay members **from the offshore** | the **offshore-operating nation's** API key + its bot key | Yes: your verified bot key |
| Move funds **main → offshore** automatically | a nation **inside the main alliance** with withdraw permission, its API key + **its own** bot key | **No.** A bot key from the offshore nation cannot take money out of the main bank |

So the answer to your four options is **option 3: separate credentials for each**; one bot key cannot do both, because each key
acts as one nation in one alliance. (Points 3 and 4 above come from the documentation and the API's argument list; the one thing I could not
test from here is the live behaviour, so do the tiny tests below before relying on it.)

**Until you have a main-alliance bot key the bot runs `/bank offshore` in MANUAL mode:** it prepares the transfer, shows the exact
in-game steps and a tag (`TUN-OFF7`) to put in the note; when the real transfer appears in PnW it completes the record. Nothing is
faked and nothing is recorded as done before PnW shows it. When you get a second bot key, add `MAIN_BOT_KEY` and `MAIN_BOT_API_KEY`
and the same command becomes automatic.

## Setup
1. Add to `.env` (and Railway variables): `OFFSHORE_ALLIANCE_ID`, `OFFSHORE_API_KEY`, `OFFSHORE_BOT_KEY` (see `.env.example`).
   `ALLIANCE_ID` stays your **main** alliance. They must be different.
2. `python scripts/check_pnw.py` now tests both banks.
3. Restart the bot.

## Before using real money (a 10-minute test)
1. `/bank offshore` (no amounts): both banks should show real contents.
2. Move **$1** main → offshore. In manual mode: send it in-game with the tag; check it turns ✅ Completed. In auto mode: check it completes.
3. With a test member balance of $1, `/bank withdrawself money=1` → it must be paid **from the offshore** and show Completed.
4. `/ledger reconcile` → should say OK.
If PnW rejects the "alliance" receiver in step 2 (automatic mode), tell me the exact message: the receiver type for an alliance is
a setting (`ALLIANCE_RECEIVER_TYPE`, default 2) that I could not confirm offline.

## Grants (`/grant send | list | view`)
A grant is an **alliance expenditure** for an alliance purpose, paid from alliance-owned funds through the normal confirmation and
PnW transfer. It is not a deposit and not a loan. `/grant send nation:<> amounts:<> purpose:<> project:<optional>`.
Each grant records: recipient, amounts, purpose, requester, approving officer (when a second approver was required), date,
PnW transaction id, current market value at the time, and status. Who may give grants: `grant_min_level` (default Minister).
Above `approval_threshold_value` a second Minister must approve, like any large transfer.
