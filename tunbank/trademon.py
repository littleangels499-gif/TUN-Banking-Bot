"""Trade monitoring: watches COMPLETED trades of our members and raises ALERTS when a configured rule is broken.

It never punishes, cancels, reverses or touches a balance. Three rules share one engine and one configuration/audit trail:

  PRICE        a trade far from the global market price (default 10x higher or 10x lower), using the SAME central
               price snapshot as the rest of the bank.
  NATIONALIST  a listed Nationalist SELLS a resource they are barred from selling, whatever the price.
  EMBARGO      one of our members trades with a member of an embargoed alliance. The trade already happened, so the
               game allowed it (the member never had the embargo applied, or opted out); TUN policy says it must not.

Only trades that trigger a rule produce an alert. The first poll just records what already exists, so history is never alerted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import alerts as A
from . import fmt
from . import ledger as L
from . import money as M
from .config import cfg_bool, cfg_get, cfg_int
from .pnw import PnWRejected, PnWUncertain
from .util import now_iso

log = logging.getLogger("tunbank.trades")
PRIORITY = ("EMBARGO", "NATIONALIST", "PRICE")
MAX_POSTS_PER_POLL = 10


def split_resources(text: str) -> set:
    t = (text or "*").strip().lower()
    return {"*"} if t in ("", "*", "all") else {x.strip() for x in t.split(",") if x.strip()}


def resources_label(text: str) -> str:
    s = split_resources(text)
    return "All resources" if "*" in s else ", ".join(M.LABELS.get(r, r.title()) for r in sorted(s))


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def sides(trade: dict) -> list[dict]:
    """Both parties of a completed trade, each from their own point of view. The offerer's direction is buy_or_sell;
    whoever accepted the offer is on the other side."""
    offer = str(trade.get("buy_or_sell") or "").lower()
    if offer not in ("buy", "sell"):
        return []
    s, r = trade.get("sender") or {}, trade.get("receiver") or {}
    sid, rid = int(trade.get("sender_id") or s.get("id") or 0), int(trade.get("receiver_id") or r.get("id") or 0)
    if not (sid and rid):
        return []
    other = {"buy": "SELL", "sell": "BUY"}
    return [dict(nation=sid, info=s, direction=offer.upper(), other=rid, other_info=r),
            dict(nation=rid, info=r, direction=other[offer], other=sid, other_info=s)]


def evaluate(trade: dict, *, roster: set, nationalists: dict, embargoes: dict, prices: dict, cfg: dict) -> dict | None:
    """Pure rule check. Returns an alert dict or None. `cfg` holds the already-read settings."""
    res = str(trade.get("offer_resource") or "").lower()
    qty, price = int(_num(trade.get("offer_amount"))), _num(trade.get("price"))
    if res not in M.RESOURCES or qty <= 0 or price <= 0:
        return None
    total = qty * price
    candidates = []
    for sd in sides(trade):
        if sd["nation"] in roster or sd["nation"] in nationalists:
            candidates.append(sd)
    if not candidates:
        return None
    best = None
    for sd in candidates:                                   # when both sides are ours the offerer is reported first
        reasons, kinds = [], []
        multiple, ref = None, None
        # ---- 1. price
        if cfg["price_on"] and ("*" in cfg["price_res"] or res in cfg["price_res"]) and total >= cfg["price_min"]:
            ref = _num(prices.get(res))
            if ref > 0:
                multiple = price / ref
                if multiple >= cfg["upper"]:
                    kinds.append("PRICE")
                    reasons.append(f"Trade price is {multiple:,.1f}× the market price (limit {cfg['upper']:g}× higher).")
                elif multiple <= 1 / cfg["lower"]:
                    kinds.append("PRICE")
                    reasons.append(f"Trade price is {multiple:.3f}× the market price, i.e. {1 / multiple:,.1f}× lower (limit {cfg['lower']:g}× lower).")
        # ---- 2. Nationalist selling a restricted resource (price is irrelevant)
        pol = nationalists.get(sd["nation"])
        if cfg["nat_on"] and pol and sd["direction"] == "SELL" and (cfg["nat_scope"] == "ALL" or str(trade.get("type")).upper() == "GLOBAL"):
            allowed = split_resources(pol["resources"])
            if "*" in allowed or res in allowed:
                kinds.append("NATIONALIST")
                reasons.append(f"Nationalist member sold {M.LABELS[res]}, which they are not allowed to sell (this applies at any price).")
        # ---- 3. embargoed alliance on the other side
        oi = sd["other_info"] or {}
        oa = oi.get("alliance_id") or (oi.get("alliance") or {}).get("id")
        emb = embargoes.get(int(oa)) if oa else None
        if cfg["emb_on"] and emb and sd["nation"] in roster:
            allowed = split_resources(emb["resources"])
            if "*" in allowed or res in allowed:
                kinds.append("EMBARGO")
                reasons.append(f"{oi.get('nation_name') or 'The counterparty'} belongs to the embargoed alliance "
                               f"{emb['alliance_name'] or ''} [#{oa}]. GAME ALLOWED · TUN POLICY VIOLATED.")
        if kinds and best is None:
            best = dict(trade_id=int(trade["id"]), trade_date=trade.get("date_accepted") or trade.get("date"),
                        trade_type=str(trade.get("type") or ""), kinds=[k for k in PRIORITY if k in kinds],
                        member_nation_id=sd["nation"], member_name=(sd["info"] or {}).get("nation_name"),
                        counterparty_id=sd["other"], counterparty_name=oi.get("nation_name"),
                        counterparty_alliance_id=int(oa) if oa else None,
                        counterparty_alliance_name=(oi.get("alliance") or {}).get("name"),
                        resource=res, quantity=qty, price=price, reference_price=ref or None, multiple=multiple,
                        total_cents=int(round(total * 100)), direction=sd["direction"], reasons=reasons,
                        both_ours=len(candidates) == 2)
    return best


def alert_card(a: dict) -> A.Card:
    kinds = a["kinds"] if isinstance(a["kinds"], list) else str(a["kinds"]).split(",")
    top = next(k for k in PRIORITY if k in kinds)
    title = {"EMBARGO": "🚨 EMBARGO VIOLATION", "NATIONALIST": "🚨 NATIONALIST RESTRICTION", "PRICE": "🚨 ABNORMAL TRADE"}[top]
    c = A.Card(title, "GAME ALLOWED · TUN POLICY VIOLATED" if top == "EMBARGO" else "Review needed. Nothing was done automatically.",
               A.RED if top in ("EMBARGO", "NATIONALIST") else A.ORANGE, kind="TRADE", nation_id=a["member_nation_id"])
    c.add("TUN member", f"{a['member_name'] or 'Nation'} [#{a['member_nation_id']}]", True)
    c.add("Counterparty", f"{a['counterparty_name'] or 'Nation'} [#{a['counterparty_id']}]", True)
    if a.get("counterparty_alliance_id"):
        c.add("Counterparty alliance", f"{a.get('counterparty_alliance_name') or 'Alliance'} [#{a['counterparty_alliance_id']}]", True)
    c.add("Resource", M.LABELS[a["resource"]], True)
    c.add("Quantity", f"{a['quantity']:,}", True)
    c.add("Direction", a["direction"], True)
    c.add("Trade price", f"${a['price']:,.2f}/unit", True)
    if a.get("reference_price"):
        c.add("Market price", f"${a['reference_price']:,.2f}/unit", True)
    if a.get("multiple"):
        c.add("Price multiple", f"{a['multiple']:,.2f}×", True)
    c.add("Total value", fmt.dollars(a["total_cents"]), True)
    c.add("Trade ID", f"#{a['trade_id']} ({(a.get('trade_type') or '').title()})", True)
    c.add("Date", str(a.get("trade_date") or "")[:19].replace("T", " "), True)
    reasons = a["reasons"] if isinstance(a.get("reasons"), list) else json.loads(a["reasons_json"])
    c.add("Reason" + ("s" if len(reasons) > 1 else ""), "\n".join(f"• {r}" for r in reasons)[:1000])
    if a.get("both_ours"):
        c.add("Note", "Both parties are TUN members.")
    c.footer = f"Alert #{a.get('id', '?')} · /trade alert {a.get('id', '')} · TUN Bank"
    return c


class TradeMonitor:
    def __init__(self, db, pnw, prices, alerts):
        self.db, self.pnw, self.prices, self.alerts = db, pnw, prices, alerts
        self._roster: set = set()
        self._roster_at = 0.0
        self._last_try = 0.0

    # ----------------------------------------------------------------- settings
    def _cfg(self, conn) -> dict:
        def f(key, default):
            try:
                return float(cfg_get(conn, key))
            except (TypeError, ValueError):
                return default
        return dict(price_on=cfg_bool(conn, "trade_price_enabled"), upper=max(f("trade_price_upper", 10), 1.0001),
                    lower=max(f("trade_price_lower", 10), 1.0001), price_res=split_resources(cfg_get(conn, "trade_price_resources")),
                    price_min=f("trade_price_min_value", 0), nat_on=cfg_bool(conn, "trade_nationalist_enabled"),
                    nat_scope=(cfg_get(conn, "trade_nationalist_scope") or "ALL").strip().upper(),
                    emb_on=cfg_bool(conn, "trade_embargo_enabled"))

    def _policies(self, conn):
        nat = {r["nation_id"]: dict(r) for r in conn.execute("SELECT * FROM trade_nationalists")}
        emb = {r["alliance_id"]: dict(r) for r in conn.execute("SELECT * FROM trade_embargoes")}
        return nat, emb

    async def roster(self) -> set:
        if self._roster and time.monotonic() - self._roster_at < 600:
            return self._roster
        self._roster = set((await self.pnw.fetch_alliance_members()).keys())
        self._roster_at = time.monotonic()
        return self._roster

    # ----------------------------------------------------------------- polling
    async def poll_if_due(self) -> dict | None:
        with self.db.read() as conn:
            if not cfg_bool(conn, "trade_monitor_enabled"):
                return None
            every = max(30, cfg_int(conn, "trade_poll_seconds"))
        if time.monotonic() - self._last_try < every:
            return None
        self._last_try = time.monotonic()
        return await self.poll()

    async def poll(self) -> dict:
        try:
            roster = await self.roster()
            trades = await self.pnw.fetch_accepted_trades(pages=3)
            snap = await self.prices.get()
        except (PnWRejected, PnWUncertain) as exc:
            await asyncio.to_thread(self._state, error=str(exc)[:300])
            log.warning("trade poll failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        prices = {r: float(v) for r, v in (snap.prices if snap else {}).items()}
        new_alerts = await asyncio.to_thread(self._process, trades, roster, prices)
        await asyncio.to_thread(self._state, error="")
        posted = 0
        for a in new_alerts[:MAX_POSTS_PER_POLL]:
            await self.alerts.trade(alert_card(a))
            posted += 1
        if len(new_alerts) > posted:
            await self.alerts.trade(A.Card("🚨 More trade alerts", f"{len(new_alerts) - posted} more alert(s) were raised. "
                                           "See them with `/trade alerts`.", A.ORANGE, kind="TRADE"))

        def mark():
            with self.db.tx() as conn:
                for a in new_alerts:
                    conn.execute("UPDATE trade_alerts SET alerted_at=? WHERE id=?", (now_iso(), a["id"]))
        if new_alerts:
            await asyncio.to_thread(mark)
        return {"ok": True, "trades": len(trades), "alerts": len(new_alerts)}

    def _state(self, error: str):
        with self.db.tx() as conn:
            L.set_state(conn, "trade_last_poll", now_iso())
            L.set_state(conn, "trade_last_error", error)

    def _process(self, trades: list, roster: set, prices: dict) -> list[dict]:
        """Record every unseen trade, and an alert for each one that breaks a rule. First run = baseline only."""
        out = []
        with self.db.tx() as conn:
            baseline = L.get_state(conn, "trade_baseline_done") != "1"
            seen = {r["trade_id"] for r in conn.execute("SELECT trade_id FROM trade_seen")}
            cfg = self._cfg(conn)
            nat, emb = self._policies(conn)
            fresh = sorted((t for t in trades if int(t["id"]) not in seen), key=lambda t: int(t["id"]))
            for t in fresh:
                conn.execute("INSERT OR IGNORE INTO trade_seen(trade_id, seen_at) VALUES(?,?)", (int(t["id"]), now_iso()))
                if baseline:
                    continue
                a = evaluate(t, roster=roster, nationalists=nat, embargoes=emb, prices=prices, cfg=cfg)
                if not a:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO trade_alerts(trade_id,created_at,trade_date,trade_type,kinds,member_nation_id,member_name,"
                    "counterparty_id,counterparty_name,counterparty_alliance_id,counterparty_alliance_name,resource,quantity,price,"
                    "reference_price,multiple,total_cents,direction,reasons_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a["trade_id"], now_iso(), a["trade_date"], a["trade_type"], ",".join(a["kinds"]), a["member_nation_id"],
                     a["member_name"], a["counterparty_id"], a["counterparty_name"], a["counterparty_alliance_id"],
                     a["counterparty_alliance_name"], a["resource"], a["quantity"], a["price"], a["reference_price"], a["multiple"],
                     a["total_cents"], a["direction"], json.dumps(a["reasons"])))
                if cur.rowcount:
                    a["id"] = cur.lastrowid
                    out.append(a)
                    L.audit(conn, "system:trades", "TRADE_ALERT", f"trade:{a['trade_id']}",
                            {"kinds": a["kinds"], "member": a["member_nation_id"], "counterparty": a["counterparty_id"]})
            if baseline:
                L.set_state(conn, "trade_baseline_done", "1")
            from datetime import timedelta
            from .util import ISO_FMT, utcnow
            conn.execute("DELETE FROM trade_seen WHERE seen_at < ?", ((utcnow() - timedelta(days=7)).strftime(ISO_FMT),))
        return out
