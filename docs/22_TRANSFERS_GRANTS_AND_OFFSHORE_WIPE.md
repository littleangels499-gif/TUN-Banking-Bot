# 22. Update: raws & grants, offshore wipe, and the vault-card crash fix

## 1. Fixed from your Railway log: `/bank holdings` crash
`Embed size exceeds maximum size of 6000` (also hit by the **Vault** button in `/help`). Discord limits the TOTAL text of one embed to 6000
characters; the vault card holds about ten fields that each list all twelve resources, so a well-stocked bank (especially with custom
icons, which are ~32 characters each) is within a step of the limit and then fails.
Fixed centrally, not just for that card: a card that is too big is now **split across several messages** (nothing is dropped; the buttons
stay on the first one), confirmation screens show **every** part before asking you to confirm, alert channels split too, and the embed
builder can no longer produce an invalid embed at all. The test harness now enforces Discord's real limits, so this cannot slip through again.
*I could not reproduce your exact overflow with test data (my largest vault card reached 5,104 of 6,000), so I cannot say which numbers tipped yours over; the fix does not depend on that.*

## 2. The new commands
All need the **grant level** (setting "Who may give grants", default Minister). Every one shows its **funding source first** and has a mandatory `source`.

| Command | What it does |
|---|---|
| `/transfer raws nations days source [source_member] [lock_id] [no_cash] [no_daily_cash] [only_missing] [note]` | Resources each nation needs for `days` (decimals allowed), from its **actual buildings, infrastructure and military** |
| `/grant warchest nations source [ratio] [only_missing] …` | ECON's resources-per-city x city count x `ratio` (1, 1.1, 1.5, 2, 2.5 …). 12 cities, ratio 1 = 12,000 food / 1,200 steel / 1,200 aluminum; ratio 2 = double |
| `/grant city nation number_of_cities source …` | Money for the next city (or up to 10), with the nation's own city-cost discounts |
| `/grant infrastructure nations source [target_infrastructure] …` | Money to raise **each city** from its **own** infrastructure to the target (cities already there cost nothing) |
| `/grant land nations source [target_land] …` | Same for land |
| `/grantset show / warchest / infra / land / citycap / dailycash / rules / live / batch / resume` | Settings (Minister), the rules sheet, the **live switch (Admin)**, and the full record of any batch |

`nations` accepts names or IDs, comma separated, or the word `alliance` for every current member (at most 50 per command by default).
`/grant send` and `/bank withdraw` are unchanged.

## 3. Funding sources: always stated, never mixed
| Source | Offered? | Notes |
|---|---|---|
| **Alliance funds** | Yes | Only the **alliance-owned** part (the bank minus what members own, minus other alliances' shares). Says which bank pays and, in shared-offshore mode, that it is this alliance's ownership share. Needs only the grant level. |
| **A member's available balance** | Yes | Each recipient's **own** balance, or one named member's (`source_member`). Needs the separate permission **`disburse_member_available`** (Admins have it; give it to a role with `/bankset setaccess`). Ordinary ECON does **not** have it. |
| **A member's locked funds** | Yes | One recipient, one named lock (`lock_id`), the lock must belong to that member. Needs **`disburse_member_locked`** and the setting `econ_locked_withdraw_enabled` on. |
| Member tax account | **No** | The bank does not track a verifiable balance for tax-bracket accounts, so it is not offered. |
| Another alliance's ownership share | **No** | Those owners withdraw their own shares; it is not safe to spend them from here. |

Member deposits, locked reserves and alliance-owned funds are never mixed: the check for each source looks only at that source.
The PnW note of **alliance-funded** transfers carries the donation note (`#ignore`, setting "Mark alliance-funded transfers as donations") so
they are never treated as a member's deposit or withdrawal; **member-funded** transfers never carry it. The structured classification
(`RAWS`, `WARCHEST_GRANT`, `CITY_GRANT`, `INFRA_GRANT`, `LAND_GRANT`) is stored separately, not in the note.

## 4. How a transfer is made safe
1. Recipients are resolved; live nation, city and stockpile data is fetched from PnW.
2. Each nation is calculated. A nation PnW did not return, that is **not in this alliance**, or whose data is **incomplete** is **flagged for review and left out**; nothing is guessed.
3. The funding source is validated **for the whole operation, all-or-nothing**: if it cannot cover everyone, **nothing is sent**.
4. Preview (funding source first, totals, per-nation amounts, assumptions, anything left out) + the **full calculation attached** (requirement, stockpile, amount, source per nation).
5. If sending is off for that kind: it stays a preview. If the value is over the approval threshold (setting "Second approver above", default **0 = off**) a second Minister must approve.
6. **Explicit confirmation**, then everything is re-checked and the numbers **re-fetched**; if anything changed since the preview, **nothing is sent**.
7. Each recipient is paid through the **existing withdrawal pipeline** (hold, one PnW call, verify, book): the same guarantees as `/bank withdraw`, including limits, the emergency lock and the reconciliation checks.
8. The batch and every recipient's outcome are stored (inputs, calculation, source, transaction and PnW record ids). A batch with anything failed, uncertain or left out is **Partial**, never "Completed". An uncertain transfer stops the batch; the existing safeguard then blocks further sending until ECON resolves it (`/bank resolvetx`), and `/grantset resume` continues without ever re-sending a sent or uncertain recipient.
9. Doing the identical thing again within 10 minutes is refused.

## 5. READ THIS before the first live test: the game formulas are unverified
I have no access to the live game, so **the game rules in `tunbank/pnw_rules.py` could not be checked** and the version is labelled `UNVERIFIED`.
That is why **every kind starts as preview-only** (`/grantset live`, Admin, with a warning). What to do:
1. `python scripts/check_pnw_schema.py` (and `--nation <id>`): confirms every field the calculations request really exists in PnW's API and whether your key can see stockpiles. A missing field means that command flags nations for review instead of calculating.
2. `/grantset rules` sends the rules as a text file. Compare with the game.
3. Run **previews** for a few real nations and compare with Locutus / in game. Warchest is plain arithmetic on your configuration and city count, so start there. **Raws, city, infrastructure and land depend on the formulas**; one number in my preview (infrastructure at 1,000 infra, about $5,785 per point) already looks high to me.
4. Tell me what differs, or edit the constants in `pnw_rules.py`. Then enable one kind at a time.

**Not modelled** (the preview says so): a nation's own production is **not** subtracted (only `only_missing` nets the stockpile); domestic policy, wartime and radiation/season effects; military has **cash** upkeep only (no daily resource upkeep). Missing project data makes city/infra/land an **ESTIMATE**, marked as such, priced without that discount.
**Needs your decision:** `no_daily_cash`. I implemented it as "never lift a nation's cash above the daily-bonus threshold": cash is sent only up to *threshold − current cash* (none if already above). It only ever **reduces** cash. Your wording ("would leave the nation below the threshold") could mean something else; tell me the exact rule you want.

## 6. `/offshore wipe` (Admin): start the assignments over
Clears **every** alliance's recorded share of the shared offshore so you can assign from scratch. Scope: *every alliance including this one*, or *every other alliance (keep this alliance's own share)*.
* Needs the typed phrase `WIPE OFFSHORE ASSIGNMENTS`, a reason, and confirmation. A **safety backup** is taken first.
* **Nothing is deleted or rewritten**: each share is brought to zero with reversing entries in one group (identifiable, audited, with the previous amounts in the audit log and attached as a text file).
* The funds stay in the offshore (they simply become *unassigned*); **member balances and PnW are untouched**; nothing is sent. Reconciliation treats unassigned funds as a surplus, not a problem.
* Refuses while any withdrawal or offshore transfer is in flight, during the emergency lock, and if the result would not be exactly zero (all-or-nothing).
* Afterwards: `/offshore assign`, `/offshore adjustownership` or `/offshore reassign`. New real PnW deposits keep being credited to the depositing alliance as before.

## 7. New settings, permissions, migration
* Settings (page "Grants & transfers" in `/bankset config`): warchest per city, infrastructure/land targets and per-nation caps, city cap, daily-bonus threshold, which kinds may send, donation note on/off, most nations per command.
* New permission flags: `disburse_member_available`, `disburse_member_locked` (assign with `/bankset setaccess`).
* Migration **`016_disbursements.sql`**, additive: `disbursement_batches` and `disbursement_items` (what was calculated can never be edited or deleted; only outcomes update). Nothing existing is touched.
* No new environment variables.

## 8. Applying it
Same procedure as guide 21: rehearse on a `/bankset backup` with `scripts/rehearse_upgrade.py`, replace the code (keep `.env`, `data/`, `.venv/`), `pip install -r requirements.txt`, run `python scripts/check_startup.py`, push, watch the log for `Applied migration 016…` and `Logged in as`.

## 9. Limitations
Formulas unverified (section 5) · locked funds pay one recipient at a time · no tax-account or other-alliance source · `/bankset` is full (25 of 25); the new settings live under `/grantset` · real PnW and Discord behaviour (the live GraphQL field names, stockpile visibility, real DMs) is for you to confirm; everything here was tested with simulated PnW and Discord.
