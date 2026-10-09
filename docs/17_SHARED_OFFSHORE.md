# 17 · The shared offshore (several alliances, one physical bank)

**One physical PnW offshore → many registered alliances → one TUN-hosted bot doing the accounting.**
The bot is never invited to another alliance's Discord. Other alliances are entries in the **Offshore Registry**, managed from the TUN server.

## The idea
* The offshore's **real PnW balance is the physical truth**. The bot keeps an **ownership ledger** that says whose share of it is whose.
* Each alliance has its own: ID, name, share (per resource), history (deposits, withdrawals, transfers), and reconciliation view.
* **TUN can only count and spend its OWN share.** Holdings used for withdrawals, grants, conversions and reconciliation are
  *main bank + TUN's share of the offshore*, never the whole offshore.
* A share **never goes below zero**, and **the bot never changes a share to make numbers match**. If shares add up to more than the
  bank holds, reconciliation says so (below).
* Nothing is hard-coded to TUN: the host alliance is just the registered alliance marked as host. Registering a third, fourth… alliance is one command.

## Turning it on (once)
1. `/offshore enable` (Admin). Nothing moves in PnW. Everything the offshore holds right now is recorded as **TUN's share**, so nothing changes for you today.
2. `/offshore registry action:Add alliance:15485` (Rising Nations). Optionally `role:@RN-ECON` (a role in *this* server) so those people can see **only** RN's account.
3. Split what is there with `/offshore reassign from:<TUN> to:<RN> amounts:"money=3b steel=4000 aluminum=8000" reason:"initial split"`.
   To carve out a share for a new alliance from funds nobody owns: `/offshore release` (takes it out of one share → unassigned) then `/offshore assign`.

## How ownership changes (always from something real)
| Event | Effect on shares |
|---|---|
| An alliance **registered** in the registry sends money into the offshore (a real PnW record) | that alliance's share goes **up** |
| TUN moves funds main → offshore (`/bank offshore`, the panel's Offshore button) | TUN's share goes **up**, when PnW shows the record |
| A TUN member withdrawal / grant / bulk payment is paid **out of the offshore** | TUN's share goes **down**, only TUN's |
| `/offshore send` (Admin): the bot pays out of the offshore **for a registered alliance** | **that** alliance's share goes down, after PnW shows the record |
| `/offshore reassign` | one share down, another up; the physical bank is unchanged |
| `/offshore assign` | a share goes up, **only from funds physically there that nobody owns yet** |
| `/offshore release` | a share goes down (the funds become unassigned) |
| A deposit from an **unregistered** alliance, or an outflow the bot can't place | **nothing changes**; it is flagged. Register the alliance if needed, then `/offshore attribute record:<id> alliance:<…>` |

Every deposit and withdrawal entry must cite a real PnW record that matches its amount (the database refuses it otherwise),
and a record can only be counted once. The ownership entries are hash-chained and can never be edited or deleted.

## Reconciliation
Compared: the offshore's **real PnW holdings** vs the **sum of every registered share** (and, for TUN, main bank + TUN's share vs members' balances as before).
```
Actual offshore   $10,000,000,000
TUN               $6,000,000,000
Rising Nations    $3,000,000,000
Alliance C        $1,000,000,000
Unassigned        nothing        →  RECONCILED
```
* Shares **above** what the bank holds, in some resource → 🟡 WARNING (`OFFSHORE_SHORTFALL`); if the total is worth more than the bank at current
  prices → 🟠 RECONCILIATION REQUIRED (`OFFSHORE_NET_SHORTFALL`). **No share is touched.**
* A broken ownership hash chain, a stored share that doesn't match its entries, or an unbalanced transfer → 🔴 critical integrity problem.
* Funds in the bank that nobody owns yet show as **Unassigned** (not an error).
* Run it with `/ledger reconcile`; the report shows the per-alliance picture to people allowed to see it.

## Who can see what
| Person | Sees |
|---|---|
| Admin, or anyone with the `bank_view_alliance_holdings` permission | the **whole** picture: physical bank, every share, unassigned, reconciliation |
| Other TUN staff (Auditor and above) | the **host alliance's** account only |
| Holders of a registered alliance's role (set with `registry … role:`) | **that alliance's** account only |
| Everyone else | nothing |
Setting things up (enable, registry changes, assign, reassign, release, send, attribute) is **Admin only**.

## Commands
`/offshore status` · `account [alliance]` · `history [alliance]` · `registry list/add/remove` · `enable` · `assign` · `reassign` · `release` · `send` · `attribute`.
Moving TUN's own funds main → offshore is still `/bank offshore` (or the panel button).

## Good to know
* `/offshore send` needs the offshore's own PnW credentials (`OFFSHORE_API_KEY` + a verified `OFFSHORE_BOT_KEY`). Without them it prepares the payout and shows the in-game steps (note `TUN-OFF<id>`).
* An alliance can only be deactivated when its share is empty and it has no payout in progress.
* Migration 011 rebuilds the `offshore_transfers` table (every row copied; its "cannot be COMPLETED without a real PnW record" protection is kept).
