# 16 · The TUN Bank panel and trade monitoring

> **Several resources at once.** Every amounts box takes any number of resources in one request, e.g. `money=2000000 steel=3000 aluminium=1000 food=50000` (`food=all` also works for withdrawals and sends). Everything is checked first, you confirm once, and if one item is invalid or short the **whole** request is refused with nothing partly done. The conversion panel works the same way (several resources into one).

## A. The banking panel
Post it once (Admin): `/bankset panel` (optionally `channel:#bank`). It is remembered, so running it again **re-posts** the panel and removes the old one.
The buttons never expire and keep working after the bot restarts.

| Button | What it does | Uses the same service as |
|---|---|---|
| 💸 **Withdraw to Me** | pick a resource → amount → quote with current market value → confirm → real PnW transfer → ledger | `/bank withdrawself` |
| 📤 **Send Funds** | pick a resource → recipient, amount, note → confirm → real PnW transfer to that nation → ledger | the same withdrawal code (own available balance, limits, net-worth rule, hold, PnW confirmation) |
| 📥 **Deposit Funds** | pick a resource → amount → confirm → the bot starts the deposit **from your nation with your own API key** → credited only when PnW's real record appears | `/bank deposit` |
| ♻️ **Deposit Excess** | reads what your nation holds (with your key), compares it with ECON's limits, shows **EXCESS FUNDS FOUND**, deposits it after you confirm | the same deposit service |
| 🏝️ **Offshore Funds** | main bank → offshore bank. Funds stay alliance-owned; **no member balance changes** | `/bank offshore`, following `offshore_access` (members / staff / admin) |
| 🏦 **My Account** | your balance, locked funds, activity | `/bank dashboard` |

* Locked funds can never be withdrawn or sent. Frozen accounts, emergency lock, pending reconciliation and every limit apply exactly as for the commands.
* Nothing is credited because a request was sent: deposits are credited only after PnW's own bank record is seen; a refused transfer changes nothing and the member is told why.
* If a member's API access isn't ready the panel says: *"Your PnW API access is not configured. Please link your nation and enable the required API access…"* with a button to save the key. Keys are stored encrypted and never shown or logged.
* **Send Funds** can be switched off with `member_send_enabled` (`/bankset config`). It can send to any valid PnW nation; keep the member transfer limit (`/bankset settransferlimit`) set to what you are comfortable with. Every send is also posted to the ECON log.

### Excess limits (Deposit Excess)
`/bankset excess limits:"money=50m food=250k coal=10k"` sets the most a nation should keep. Anything above, for the resources you list, is "excess".
`/bankset excess` with no limits shows the current setting; `limits:none` clears it. (There is no separate warchest feature yet; when there is, these limits can feed it.)

## B. Trade monitoring
It watches **completed** trades of our members and posts an **alert only when a rule is broken**. It never cancels, reverses, punishes or touches a balance.
Set the alert channel with `/bankset setlogchannel kind:Trade alerts` (blank = the ECON log). The first check only records existing trades, so history is never alerted.

| Rule | Alerts when |
|---|---|
| 💹 **Price** | a trade price is ≥ **10×** higher or ≥ **10×** lower than the market price (both configurable), using the bank's normal market-price snapshot |
| 🛡️ **Nationalist** | a listed Nationalist **sells** a resource they are barred from selling, **at any price** |
| ⛔ **Embargo** | one of our members trades (buy or sell) with a member of an embargoed alliance. Labelled **GAME ALLOWED · TUN POLICY VIOLATED**: the trade completed, so the game permitted it (no embargo applied, or the member opted out). The API does not say which. |

Each alert shows the member, the other nation, the other alliance, resource, quantity, trade price, market price, multiple, total value, direction, trade id and date, and the reason(s).

### Commands (`/trade …`)
| Command | Who | What |
|---|---|---|
| `alerts` · `alert id` | Auditor+ | recent alerts (open / reviewed / all) · one alert in full |
| `review id note` | Banker+ | mark an alert as looked at |
| `status` | Auditor+ | is it running, last check, last problem, what is on |
| `config` | Minister+ | switch rules on/off; `upper`, `lower`, `price_resources`, `min_value`, `nationalist_scope` (every sale / global only), `poll_seconds` |
| `nationalist list/add/remove` | Minister+ (list: Auditor+) | e.g. `add nation:Foo resources:"food coal oil"` or `resources:all` |
| `embargo list/add/remove` | Minister+ (list: Auditor+) | by alliance id or exact name; optional resource restriction |

Every change (rules, thresholds, Nationalists, embargoes) is written to the configuration audit log with who, when, before and after.

### Good to know
* It checks PnW every 2 minutes by default (`poll_seconds`, minimum 30). That uses about 1–3 PnW API requests per check.
* A Nationalist or member who has left the alliance still counts if they are on the Nationalist list.
* If both sides of a trade are TUN members, one alert is raised and says so.
