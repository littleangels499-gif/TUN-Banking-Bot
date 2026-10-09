# 11. Who sees what, tax alerts, linking and deposits

## Three separate levels of visibility
| What | Who | How it is controlled |
|---|---|---|
| **My own account** | the member | automatic (your linked nation only) |
| **Other members' accounts** (`/audit nation`, exports of balances) | ECON staff: Auditor, Banker, Minister | staff roles (`/bankset setrole`) |
| **The alliance treasury**: real bank contents (main + offshore), alliance-owned funds, vault, vault charts/exports, offshore status | **only roles you choose, plus Admins** | permission `bank_view_alliance_holdings` |
| **Alliance tax**: dashboards, reports, brackets, profiles, exports, tax chart | **only roles you choose, plus Admins** | permission `bank_view_tax` |

Being on the ECON team does **not** include the last two. Give them with `/bankset setaccess permission:<name> role:@Role`
(add `remove:true` to take it away). `/bankset access` shows who has what. If nobody is mapped, only Admins see them.

What this protects: members cannot use `/bank holdings`, `/bank offshore`, the vault charts/exports, or any error message to learn what the
banks hold. A member whose withdrawal can't be paid because a bank is short just sees "can't be processed right now"; ECON gets the detail
in the ECON log. Staff without the treasury permission also get messages that say "not sufficient" without the figures.
**Discord channel permissions still decide who can read the log channels**, so keep `#econ-log` and the tax channel private.

## Tax alerts (one message per turn)
* PnW collects tax every turn (2 hours, on even UTC hours). The bot recognises tax from the **PnW tax id on the bank record**, not from the
  word "tax" in a note.
* After a turn's tax records have all arrived, it posts **one** message: total cash, total resources, total Current Market Value and the time.
  **No member list.** Set the channel with `/bankset setlogchannel channel:#tax-alerts kind:Tax alerts` (otherwise it uses the ECON log).
* Details: `/tax report`, `/tax profile`, `/tax turns` (totals per turn) and `/tax export` (Excel, per member and per record), all behind `bank_view_tax`.

## Linking a nation to a member (staff)
`/bank linknation member:@User nation:<id, name or link>`
* Minister or Admin can create a link when both the nation and the Discord account are free.
* If the nation already belongs to someone else, or that person already has another nation, nothing changes. An **Admin** must repeat it
  with `force:true`; the confirmation shows both people.
* The bot checks the nation is in the alliance and compares PnW's Discord field to the member's username. Every link is recorded in the audit
  log and in a permanent link history. A link moves *who may use a nation's deposit*; it never moves money.

## Depositing
See docs/12: members can deposit from Discord with their own API key, or follow the guided steps.


---

## Where tax records come from (important)
PnW keeps tax collections in their own list (`taxrecs`), separate from ordinary bank records. The bot reads that list on every scan,
about 14 days at a time, so **nothing is set up per nation**: every tax record already says which bracket it came from, the brackets and their
rates are read live from PnW (`/tax brackets`), and `/tax profile` shows each member's bracket and one line per turn with the exact cash paid.

* The first time the bot reads the tax list on a database, the older turns are stored but **not announced**; only new turns are posted.
* A tax record is never treated as a deposit, even if PnW left its tax id empty.
* If PnW refuses the tax list, deposits keep working and `/bank sync` tells you exactly what PnW said.
* To receive the per-turn summary you still need a channel: `/bankset setlogchannel` (kind: Tax alerts), and `/bankset setaccess` to give a role `bank_view_tax`.
