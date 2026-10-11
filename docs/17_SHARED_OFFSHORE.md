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
1. `/offshore enable` (Admin). Nothing moves in PnW and **nothing is assigned**: every alliance starts at zero and the whole offshore shows as *unassigned*. (Until step 3, TUN cannot pay anything out of the offshore, and reconciliation warns.)
2. `/offshore registry action:Add alliance:15485` (Rising Nations). Optionally `role:@RN-ECON` (a role in *this* server) so those people can see **only** RN's account.
3. Record each alliance's **real starting share**, including TUN's own:
   `/offshore adjustownership alliance:<TUN> amounts:"money=6b steel=5000 aluminum=10000" reason:"Locutus export 2026-10-12"`, then the same for Rising Nations and any other alliance.
   It is an **accounting correction only: nothing moves in PnW.** You see before/after and how the combined shares compare with the real offshore, you confirm, and it is audited permanently (who, when, why, before, after, what the offshore held). Use `replace_all:True` to also zero every resource you didn't list.

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

## Transactions made outside this bot (e.g. by Locutus)
* Money leaving the offshore **to a registered alliance's own bank** reduces **that alliance's** share automatically (the destination proves whose it is). It is never taken from TUN.
* Money a registered alliance **sends into** the offshore credits that alliance.
* Anything else touching the offshore that can't be attributed (a nation paying in, a payout to a nation made outside the bot, an unregistered alliance) is **never assigned automatically**. It is listed as **unexplained** in `/ledger reconcile` and `/offshore status` until an Admin investigates and uses `/offshore attribute`.

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
* The shared-offshore part of `/ledger reconcile` reports three things **separately**: **A** resource by resource (owned > held, and held > owned, side by side, so a surplus never hides a shortage), **B** overall market value (held vs recorded ownership), and **C** unexplained transactions. Shares are never changed to make them match.

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

## RN and other alliances using other bots
RN currently uses Locutus, so RN's balance is tracked here from what really happens in the offshore bank, whoever starts it. Nothing is deducted from TUN for RN's activity.
Letting another alliance's own staff withdraw through this bot would need an authorization model (who may ask, with the offshore's own API credentials doing the sending); it is not built yet and `/offshore send` stays Admin-only.
