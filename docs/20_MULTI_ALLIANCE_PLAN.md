# 20. Multi-alliance support: assessment and plan (Part A)

## Verdict
**TUN Bank is a single-alliance bot today and must not be described as supporting other alliances.** The *shared offshore* feature
(guide 17) lets several alliances own shares of **one physical offshore that TUN runs**. That is accounting inside one bank, not
independent banks for other alliances. A second alliance on its own Discord server cannot use this bot today without seeing, or
touching, TUN's data.

## What the code looks like today (measured)
| Fact | Count / detail | Why it matters |
|---|---|---|
| Tables with no alliance/tenant column | 61 of 61 (only the shared-offshore tables know about alliances) | every member, balance, ledger entry, loan, transaction is in one namespace |
| Hash chains (ledger, audit, offshore ledger) | one global chain per table | a tenant's chain would be interleaved with every other tenant's |
| Settings read through `cfg_get/int/bool/set` | 96 call sites, one global `bank_config` table | roles, channels, limits, rules are global |
| Global key/value state (`get_state/set_state`) | 55 call sites: emergency lock, pause, baselines, scan timers | one alliance's emergency lock would freeze all |
| Permission checks | 104; roles are Discord role IDs from one server | no notion of "admin of alliance X" |
| PnW credentials | one `ALLIANCE_ID` + keys in environment variables | one bank per process |
| Services (scanner, withdrawals, prices, trade monitor, alerts) | one instance each, built once at start-up | all are bound to that single alliance and its keys |
| Background loops | 6, all single-alliance | would need per-alliance scheduling and a shared PnW rate-limit budget |
| Name/identity cache | process-wide (`identity.init`) | autocomplete could leak another alliance's nation names |
| Persistent buttons / panel | custom IDs carry no alliance | a button must never act on the wrong alliance's data |

## Already done in this release (Phase 0)
* **Enforced guild lock:** with `GUILD_ID` set, a command from any other server is refused (before, `GUILD_ID` only chose where commands were *registered*).
* Tests can no longer read a real `.env`.
* `scripts/verify_database.py`, `scripts/check_startup.py`, per-database fingerprints and the migration guide (18) are the base that per-alliance backup/migration will reuse.

## Two ways to build it. Please decide.
**Option B (recommended): one bot, one Railway deployment, one *database file per alliance*.**
A small "tenant registry" maps each Discord server → an alliance → its own `tunbank.db` (`/data/tenants/<guild_id>/tunbank.db`), its own settings and its own encrypted PnW keys.
The bot picks the right set of services from `interaction.guild_id`. All existing single-tenant code (ledger rules, triggers, hash chains, reconciliation) keeps working unchanged *inside* each alliance's database.
* Isolation is physical: a query can't cross alliances because it can't even see the other file.
* Backups, restores, migrations, deletion and fingerprint checks are per alliance, using the tools that already exist.
* Cost: "one database" becomes "one database per alliance, one deployment".

**Option A: literally one database.** Add `tenant_id` to all 61 tables, rebuild the guarded tables (as migration 007 rebuilt the ledger), make every hash chain and trigger tenant-aware, and touch all 96 + 55 + 104 call sites above.
* Larger, riskier, and isolation depends on *every* query being right forever. A single missed `WHERE tenant_id=?` leaks money data between alliances.
* I do not recommend it for a financial bot unless you have a specific reason to need one file.

## Plan (Option B; Option A adds a large schema-rebuild phase before Phase 1)
| Phase | Work | Done when | Size |
|---|---|---|---|
| 1. Tenant registry + routing | `tenants` table (guild, alliance, status); per-tenant `Settings` (alliance id, bank keys stored **encrypted**, not in env); `Services` built per tenant; every command handler resolves services from the interaction instead of capturing one global `svc`; identity cache per tenant | two alliances can be loaded in one process and a command from each hits only its own database | **L** (wide but mechanical: every handler) |
| 2. Per-tenant background work | scan/reconcile/loan/trade/notification loops run per tenant, staggered; one shared PnW rate-limit budget | one tenant's PnW outage or emergency lock never affects another | M |
| 3. Onboarding and roles | `/setup` by the server owner: verifies the alliance via PnW, stores keys, creates the database; platform operators (you) vs per-alliance admins; per-alliance role mapping; offboarding/deletion; per-alliance backup & restore commands | an alliance can be added and removed without touching code or Railway | M |
| 4. Isolation test suite | two tenants in one process; automated checks that no command, export, autocomplete, button, modal or approval id can read or act on the other tenant; persistent-view custom IDs carry the tenant | the suite fails if any cross-tenant access exists | M |
| 5. Pilot | a second alliance in a staging server; real PnW; monitor | no cross-tenant incident, backups/restore verified per alliance | S |

## Non-negotiables for the build
No shared hash chain · no global emergency-lock/pause state · no tenant data in logs · tenant keys never in the repo or env · deleting an
alliance removes only its file · every cross-tenant path covered by a test · the existing accounting and security rules (guards, confirmations,
audit, additive migrations) apply unchanged inside each alliance.

## Interim, safe way to serve a second alliance **today**
Run a **separate deployment** for them: a separate Railway service and Volume (so a separate database), their own Variables, and their **own Discord application/token**
(two deployments must never share one token). Their data is then physically separate from TUN's. This is not the one-deployment goal, but it is safe and needs no code change.

## Decisions I need from you
1. Option B (database file per alliance) or Option A (one database)?
2. One Discord application for all alliances, or one per alliance? (Option B supports one for all.)
3. Who approves a new alliance: you only, or self-service by the server owner after PnW verification?
4. Does each alliance supply its own PnW API/bot keys? (It has to; the bot cannot use TUN's.)
5. How should the shared offshore work across alliances that are tenants of the same bot?
