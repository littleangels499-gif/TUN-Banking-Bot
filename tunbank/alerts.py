"""Alerts. Every financial alert shows quantities AND Current Market Value.

Cards are plain data (Card) so they can be tested without Discord; `to_embed`
converts them for sending.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import fmt
from . import icons
from . import money as M
from .config import cfg_bool, cfg_get
from .util import now_iso

log = logging.getLogger("tunbank.alerts")

RED, ORANGE, GREEN, BLUE, GREY = 0xE74C3C, 0xE67E22, 0x2ECC71, 0x3498DB, 0x95A5A6


@dataclass
class Card:
    title: str
    description: str = ""
    color: int = BLUE
    fields: list = field(default_factory=list)   # (name, value, inline)
    kind: str = "INFO"
    nation_id: int | None = None
    footer: str = ""
    image: str = ""

    def add(self, name, value, inline=False):
        value = str(value) or "-"
        self.fields.append((name[:250], value[:1000], inline))
        return self

    def as_text(self) -> str:
        parts = [f"**{self.title}**", self.description]
        parts += [f"{n}\n{v}" for n, v, _ in self.fields]
        return "\n".join(p for p in parts if p)


def to_embed(card: Card):
    import discord

    import datetime as dt

    e = discord.Embed(title=card.title[:250], description=card.description[:3500], color=card.color)
    for n, v, i in card.fields[:24]:
        e.add_field(name=n, value=v, inline=i)
    if card.image:
        e.set_image(url=card.image)
    foot = card.footer or "TUN Bank"
    try:
        e.set_footer(text=foot[:200])
        e.timestamp = dt.datetime.now(dt.timezone.utc)
    except AttributeError:  # test doubles
        pass
    return e


def _who(nation_id, name=None):
    return f"{name or 'Nation'} [#{nation_id}]" if nation_id else "-"


def _money_block(amounts, val):
    return fmt.amounts_with_value(amounts, val)


# ----------------------------------------------------------------- builders
def deposit_card(o, name: str | None = None) -> Card:
    c = Card(f"{icons.status('deposit')} Deposit received", color=GREEN, kind="DEPOSIT", nation_id=o.nation_id)
    c.add(f"{icons.status('member')} From", _who(o.nation_id, name), True)
    c.add("Classification", o.classification.replace("_", " ").title(), True)
    c.add("PnW record", f"#{o.record_id}", True)
    c.add(f"{icons.status('money')} Received", _money_block(o.amounts, getattr(o, "valuation", None)))
    if o.before is not None:
        c.add("Balance before", fmt.amount_lines(o.before["available"]), True)
        c.add("Balance after", fmt.amount_lines(o.after["available"]), True)
    if o.warnings:
        c.add(f"{icons.status('warn')} Warnings", "\n".join(o.warnings))
        c.color = ORANGE
    return c


def member_deposit_dm(o) -> Card:
    c = Card(f"{icons.status('ok')} Your deposit was received", "Your TUN Bank account has been credited."
             + (f"\nThis is the deposit you planned (#{o.intent})." if getattr(o, "intent", None) else ""), GREEN, kind="DEPOSIT")
    c.add("Deposited", _money_block(o.amounts, getattr(o, "valuation", None)))
    c.add("New available balance", fmt.amount_lines(o.after["available"]))
    c.add("PnW record", f"#{o.record_id}", True)
    return c


def classified_card(o, name=None) -> Card:
    titles = {"TAX": f"{icons.status('tax')} Tax collection recorded", "DONATION": f"{icons.status('alliance')} Alliance donation (#ignore)",
              "LOAN": f"{icons.status('bank')} Loan repayment detected", "REVIEW": f"{icons.status('warn')} Record needs ECON review",
              "EXTERNAL_OUTFLOW": f"{icons.status('warn')} Money left the bank outside TUN Bank",
              "OUTGOING_LINKED": f"{icons.status('withdraw')} Outgoing transfer confirmed by PnW",
              "OFFSHORE": "🏝️ Funds moved between our banks"}
    colors = {"TAX": BLUE, "DONATION": BLUE, "LOAN": BLUE, "REVIEW": ORANGE, "OUTGOING_LINKED": GREEN, "OFFSHORE": BLUE}
    c = Card(titles.get(o.kind, "Bank record"), color=colors.get(o.kind, ORANGE), kind=o.kind,
             nation_id=o.nation_id)
    c.add("Nation", _who(o.nation_id, name), True)
    c.add("PnW record", f"#{o.record_id}", True)
    c.add("Contents", _money_block(o.amounts, getattr(o, "valuation", None)))
    if o.note:
        c.add("Reason", o.note)
    if o.kind == "REVIEW":
        c.add("What to do", f"Use `/bank review record_id:{o.record_id}` to decide.")
    return c


def withdrawal_card(r, *, actor_label: str, dest_nation_id: int, source_label: str, amounts: dict,
                    note: str) -> Card:
    ok = r.status == "COMPLETED"
    c = Card(f"{icons.status('ok') if ok else icons.status('bad' if r.status in ('FAILED', 'BLOCKED') else 'wait')} Withdrawal " + ("completed" if ok else r.status.lower().replace("_", " ")),
             r.message, GREEN if ok else (RED if r.status in ("FAILED", "BLOCKED") else ORANGE),
             kind="WITHDRAWAL")
    c.add("Requested by", actor_label, True)
    c.add("Destination", _who(dest_nation_id), True)
    c.add(f"{icons.status('bank')} Funding source", source_label, True)
    c.add(f"{icons.status('withdraw')} Sent", _money_block(amounts, r.valuation))
    if note:
        c.add("Note", note)
    if r.tx_id:
        c.add("Transaction", f"#{r.tx_id}", True)
    if r.pnw_record_id:
        c.add("PnW record", f"#{r.pnw_record_id}", True)
    if ok and r.before and r.after and "available" in r.before:
        c.add("Balance before", fmt.amount_lines(r.before["available"]), True)
        c.add("Balance after", fmt.amount_lines(r.after["available"]), True)
    return c


def lock_card(kind: str, nation_id: int, amounts: dict, val, reason: str, lock_id: int, res: dict,
              actor_label: str) -> Card:
    c = Card(f"{icons.status('lock' if kind == 'LOCK' else 'unlock')} Funds " + ("reserved" if kind == "LOCK" else "released"), color=BLUE, kind=kind,
             nation_id=nation_id)
    c.add("Nation", _who(nation_id), True)
    c.add("Lock ID", f"#{lock_id}", True)
    c.add("By", actor_label, True)
    c.add("Amount", _money_block(amounts, val))
    c.add("Reason", reason)
    c.add("Before", fmt.account_lines(res["before"]), True)
    c.add("After", fmt.account_lines(res["after"]), True)
    return c


def adjustment_card(nation_id, deltas: dict, val, reason, evidence, res, actor_label, adj_id) -> Card:
    c = Card(f"{icons.status('audit')} Accounting adjustment", "A documented correction was applied.", ORANGE,
             kind="ADJUSTMENT", nation_id=nation_id)
    c.add("Nation", _who(nation_id), True)
    c.add("Adjustment ID", f"#{adj_id}", True)
    c.add("By", actor_label, True)
    c.add("Change", fmt.signed_lines(deltas) + "\n" + fmt.value_line(val))
    c.add("Reason", reason)
    c.add("Evidence", evidence)
    c.add("Before", fmt.account_lines(res["before"]), True)
    c.add("After", fmt.account_lines(res["after"]), True)
    return c


def integrity_card(f: dict) -> Card:
    sev = f["severity"]
    c = Card(f"{icons.status('bad' if sev == 'CRITICAL' else 'warn')} Financial integrity: {f['kind'].replace('_', ' ').title()}", f["message"],
             RED if sev == "CRITICAL" else ORANGE, kind="INTEGRITY")
    c.add("Severity", sev, True)
    c.add("Event ID", f"#{f.get('event_id', '?')}", True)
    hide = {"message", "bank", "shortfall", "member_total", "banks"}      # treasury figures stay out of shared channels
    det = {k: v for k, v in f.get("details", {}).items() if k not in hide}
    if det:
        c.add("Evidence", "```json\n" + json.dumps(det, default=str)[:900] + "\n```")
    c.add("What to do", "Review with `/ledger dashboard`. Nothing was changed automatically; "
                        "balances are NOT rewritten.")
    return c


def lock_state_card(on: bool, reason: str, actor: str) -> Card:
    return Card((icons.status("lock") + " EMERGENCY LOCK ENGAGED") if on else (icons.status("unlock") + " Emergency lock lifted"),
                (f"Reason: {reason}" if on else "Financial changes may resume."),
                RED if on else GREEN, kind="LOCK").add("By", actor, True)


# ---------------------------------------------------------------- delivery
class AlertService:
    """Sends cards to the ECON log channel and DMs; logs every attempt."""

    def __init__(self, db):
        self.db = db
        self.bot = None
        self._lock_announced = None

    def _log(self, card: Card, channel: str, ok: bool, err: str | None = None):
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO alert_log(ts,kind,nation_id,channel,payload_json,delivered,error) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (now_iso(), card.kind, card.nation_id, channel,
                     json.dumps({"title": card.title, "text": card.as_text()[:3000]}), int(ok), err))
        except Exception:  # noqa: BLE001
            log.exception("could not write alert_log")

    async def tax(self, card: Card):
        """The tax-turn summary goes to the tax alert channel (or the ECON log when none is set)."""
        with self.db.read() as conn:
            chan = cfg_get(conn, "tax_alert_channel_id")
        await self.econ(card, channel_id=chan if chan.isdigit() else None)

    async def flush_tax_turns(self, prices):
        """One summary per finished turn: totals only, never a member list."""
        import datetime as dt

        from . import ledger as L
        from .config import cfg_int
        from .util import ISO_FMT, utcnow
        from .valuation import value_amounts

        with self.db.read() as conn:
            settle = cfg_int(conn, "tax_alert_settle_seconds")
            cutoff = (utcnow() - dt.timedelta(seconds=settle)).strftime(ISO_FMT)
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM tax_turns WHERE alerted_at IS NULL AND last_seen_at<=? ORDER BY turn_key", (cutoff,))]
        if not rows:
            return
        snap = await prices.get()
        for r in rows:
            totals = json.loads(r["totals_json"])
            val = value_amounts(totals, snap)
            others = {k: v for k, v in totals.items() if k != "money"}
            when = dt.datetime.strptime(r["turn_key"], "%Y-%m-%d %H")
            c = Card("💰 Tax Collection — Turn Complete", f"{when:%d %b %Y} — {when:%H:%M} UTC", GREEN, kind="TAX_TURN")
            c.add(f"{icons.resource('money')} Cash", fmt.dollars(totals.get("money", 0)), True)
            c.add("Resources", fmt.amount_lines(others), True)
            c.add("\u200b", fmt.value_line(val), False)
            c.footer = "Totals only · member details: /tax report · TUN Bank"
            await self.tax(c)
            with self.db.tx() as conn:
                conn.execute("UPDATE tax_turns SET alerted_at=?, value_cents=?, price_snapshot_id=? WHERE turn_key=?",
                             (now_iso(), val.total_cents, snap.id if snap else None, r["turn_key"]))
                L.audit(conn, "system", "TAX_TURN_ALERTED", f"turn:{r['turn_key']}", {"records": r["records"]})

    async def config_audit(self, card: Card) -> bool:
        """Post to the private configuration-audit channel. Returns True when it was delivered."""
        with self.db.read() as conn:
            chan = cfg_get(conn, "config_audit_channel_id")
        if not (self.bot and chan.isdigit()):
            return False
        try:
            ch = self.bot.get_channel(int(chan)) or await self.bot.fetch_channel(int(chan))
            await ch.send(embed=to_embed(card))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("config audit post failed: %s", exc)
            return False

    async def flush_config_audit(self):
        """Every recorded change reaches the audit channel. Anything that could not be posted stays queued and is retried."""
        from . import configaudit as CA

        with self.db.read() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM config_audit WHERE posted_at IS NULL ORDER BY id LIMIT 40")]
            channel_set = cfg_get(conn, "config_audit_channel_id").isdigit()
        if not rows:
            return
        if not channel_set:
            if not getattr(self, "_warned_no_audit_channel", False):
                self._warned_no_audit_channel = True
                await self.econ(Card("⚠️ Configuration audit channel not set",
                                     "Changes are being recorded permanently in the database, but there is no private channel for them yet. "
                                     "An Admin should run `/bankset setlogchannel kind:Configuration audit`. Past entries will be posted then.", ORANGE))
            return
        for r in rows:
            ok = await self.config_audit(CA.card_for(r))
            with self.db.tx() as conn:
                conn.execute("UPDATE config_audit SET posted_at=?, post_error=? WHERE id=?",
                             (now_iso() if ok else None, None if ok else "could not post; will retry", r["id"]))
            if not ok:
                break

    async def econ(self, card: Card, channel_id=None):
        with self.db.read() as conn:
            chan_id = channel_id or cfg_get(conn, "econ_log_channel_id")
        if not (self.bot and str(chan_id).isdigit()):
            self._log(card, "econ", False, "no channel set (/bankset setlogchannel)")
            log.warning("ECON alert not delivered (no channel): %s", card.title)
            return
        try:
            ch = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            await ch.send(embed=to_embed(card))
            self._log(card, f"econ:{chan_id}", True)
        except Exception as exc:  # noqa: BLE001
            self._log(card, f"econ:{chan_id}", False, str(exc)[:200])
            log.warning("ECON alert failed: %s", exc)

    async def dm(self, discord_id, card: Card):
        with self.db.read() as conn:
            if not cfg_bool(conn, "dm_members"):
                return
        if not (self.bot and discord_id):
            return
        try:
            user = self.bot.get_user(int(discord_id)) or await self.bot.fetch_user(int(discord_id))
            await user.send(embed=to_embed(card))
            self._log(card, f"dm:{discord_id}", True)
        except Exception as exc:  # noqa: BLE001
            self._log(card, f"dm:{discord_id}", False, str(exc)[:200])

    def discord_id_for(self, nation_id):
        with self.db.read() as conn:
            r = conn.execute("SELECT discord_id FROM members WHERE nation_id=?", (nation_id,)).fetchone()
        return r["discord_id"] if r else None

    def member_name(self, nation_id):
        with self.db.read() as conn:
            r = conn.execute("SELECT nation_name FROM members WHERE nation_id=?", (nation_id,)).fetchone()
        return r["nation_name"] if r else None

    async def flush_events(self):
        """Alert ECON about every integrity event exactly once (tracked by event id)."""
        from . import ledger as L
        await self.flush_config_audit()

        def load():
            with self.db.read() as conn:
                last = int(L.get_state(conn, "last_event_alerted", "0") or 0)
                rows = conn.execute("SELECT * FROM integrity_events WHERE id>? ORDER BY id LIMIT 30",
                                    (last,)).fetchall()
            return [dict(r) for r in rows]

        rows = load()
        for r in rows:
            details = json.loads(r["details_json"] or "{}")
            f = {"severity": r["severity"], "kind": r["kind"], "event_id": r["id"],
                 "message": details.get("message") or details.get("reason") or r["kind"],
                 "details": details}
            await self.econ(integrity_card(f))
            with self.db.tx() as conn:
                L.set_state(conn, "last_event_alerted", str(r["id"]))
        with self.db.read() as conn:
            st = L.integrity_state(conn)
        if rows and st["emergency_lock"] and self._lock_announced != st["emergency_reason"]:
            self._lock_announced = st["emergency_reason"]
            await self.econ(lock_state_card(True, st["emergency_reason"], "system"))
