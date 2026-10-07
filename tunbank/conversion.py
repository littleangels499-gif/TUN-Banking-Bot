"""Resource conversion INSIDE a member's TUN Bank balance. Nothing happens in-game and no PnW call is made.

What a conversion really is: an ownership swap between the member and the alliance inside the same physical bank.
  * the member's resource A becomes alliance-owned,
  * an equal MARKET VALUE of alliance-owned resource B becomes the member's.
No resource is created, no PnW transaction is faked. Because the bank still holds exactly what it held before,
the rule that keeps it honest is: the alliance must really OWN the resource the member receives (checked against the
live bank). Otherwise the ledger would promise the member something the bank does not have.

    amount received = value of what is converted / current price of what is received      (rounded DOWN)

Rounding is always in the alliance's favour by less than one hundredth of a unit, so a member can never receive
more than the exact market value they gave up.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from . import ledger as L
from . import money as M
from . import reconcile as R
from .config import cfg_bool, cfg_get
from .util import jdump, now_iso


class ConversionError(L.LedgerError):
    pass


class AllianceStockShort(ConversionError):
    """The alliance does not own enough of the resource being received. `internal` is for ECON only."""

    def __init__(self, public: str, internal: str):
        super().__init__(public)
        self.internal = internal


@dataclass
class Quote:
    from_res: str
    to_res: str
    from_units: int
    to_units: int
    value_cents: int
    from_price: Decimal
    to_price: Decimal
    snapshot_id: int | None
    as_of: str | None


def _price(snapshot, res: str) -> Decimal:
    if res == "money":
        return Decimal(1)                      # units of cash are cents
    p = snapshot.prices.get(res) if snapshot else None
    if p is None or Decimal(p) <= 0:
        raise ConversionError(f"There is no market price for {M.LABELS[res]} right now, so it can't be converted.")
    return Decimal(p)


def make_quote(snapshot, from_res: str, to_res: str, from_units: int) -> Quote:
    """Pure calculation from one price snapshot. Raises ConversionError if it can't be done safely."""
    for r in (from_res, to_res):
        if r not in M.RESOURCES:
            raise ConversionError(f"Unknown resource {r}.")
    if from_res == to_res:
        raise ConversionError("Choose two different resources.")
    if from_units <= 0:
        raise ConversionError("The amount must be more than zero.")
    if snapshot is None or snapshot.stale or snapshot.suspicious:
        raise ConversionError("Market prices can't be verified right now (missing, stale or unusual), so nothing can be "
                              "converted. Please try again shortly.")
    p_from, p_to = _price(snapshot, from_res), _price(snapshot, to_res)
    exact = Decimal(from_units) * p_from                       # value in cents
    to_units = int((exact / p_to).to_integral_value(rounding=ROUND_DOWN))
    value = int(exact.to_integral_value(rounding=ROUND_DOWN))
    if to_units < 1 or value < 1:
        raise ConversionError("That amount is too small to convert (you would receive less than 0.01 of the new resource).")
    return Quote(from_res, to_res, from_units, to_units, value, p_from, p_to, snapshot.id, snapshot.fetched_at)


def convertible(conn, nation_id: int) -> dict:
    """What a member may convert: AVAILABLE, minus pending withdrawals, positive amounts only.
    Locked funds are never convertible and a negative balance is never treated as a positive one."""
    return {r: v for r, v in L.spendable(conn, nation_id).items() if v > 0}


def parse_amount(text: str, from_res: str, available: int) -> int:
    t = (text or "").strip().lower()
    if t in ("all", "max"):
        return available
    try:
        return M.parse_amounts(f"{from_res}={t}")[from_res]
    except (M.AmountError, KeyError) as exc:
        raise ConversionError(f"I couldn't read that amount: {exc}") from exc


def execute(conn, *, nation_id: int, quote: Quote, actor: str, idempotency_key: str, holdings: dict | None) -> dict:
    """Post the conversion. Must run inside db.tx(); any failure leaves everything unchanged. Idempotent."""
    prior = conn.execute("SELECT * FROM conversions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if prior:
        return {"conversion_id": prior["id"], "replay": True, "from_units": prior["from_units"],
                "to_units": prior["to_units"], "value_cents": prior["value_cents"]}
    if not cfg_bool(conn, "conversion_enabled"):
        raise ConversionError("Resource conversion is switched off by ECON.")
    L.assert_can_mutate(conn, nation_id, "convert")
    m = conn.execute("SELECT frozen, frozen_reason FROM members WHERE nation_id=?", (nation_id,)).fetchone()
    if not m:
        raise ConversionError("This nation has no TUN Bank account.")
    if m["frozen"]:
        raise ConversionError("Your account is frozen by ECON. Please contact ECON staff."
                              + (f" Reason: {m['frozen_reason']}" if m["frozen_reason"] else ""))
    cap = Decimal(cfg_get(conn, "conversion_max_value") or "0")
    if cap > 0 and quote.value_cents > int(cap * 100):
        raise ConversionError(f"A single conversion is limited to ${cap:,.2f} of market value.")

    free = convertible(conn, nation_id)
    if free.get(quote.from_res, 0) < quote.from_units:
        have = free.get(quote.from_res, 0)
        locked = L.get_balances(conn, nation_id, "LOCKED").get(quote.from_res, 0)
        raise L.InsufficientFunds(
            f"You only have {M.fmt_units(quote.from_res, have)} {M.LABELS[quote.from_res]} available to convert."
            + (" Locked (reserved) funds can't be converted." if locked else ""))

    before = L.snapshot_accounts(conn, nation_id)
    bal_to = L.get_balances(conn, nation_id, "AVAILABLE").get(quote.to_res, 0)
    # Only the part that becomes a NEW positive claim on the bank must be backed by alliance-owned stock.
    # (Receiving a resource you owe just pays the debt down.)
    claim_increase = max(0, bal_to + quote.to_units) - max(0, bal_to)
    if claim_increase > 0:
        if holdings is None:
            raise ConversionError("The live PnW bank could not be read, so nothing was converted. Try again shortly.")
        pos = R.bank_position(conn, holdings)
        owned = (pos["alliance_owned"] or {}).get(quote.to_res, 0) - L.holds(conn, None, "ALLIANCE").get(quote.to_res, 0)
        if owned < claim_increase:
            raise AllianceStockShort(
                "This conversion can't be processed right now. ECON has been notified; your balance is unchanged.",
                f"Conversion refused: {M.fmt_units(quote.to_res, claim_increase)} {M.LABELS[quote.to_res]} requested but the "
                f"alliance only owns {M.fmt_units(quote.to_res, max(owned, 0))} (nation {nation_id}).")

    L.ensure_member(conn, nation_id)
    cur = conn.execute(
        "INSERT INTO conversions(created_at,nation_id,from_resource,from_units,to_resource,to_units,value_cents,"
        "from_price,to_price,price_snapshot_id,price_as_of,actor,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), nation_id, quote.from_res, quote.from_units, quote.to_res, quote.to_units, quote.value_cents,
         str(quote.from_price), str(quote.to_price), quote.snapshot_id, quote.as_of, str(actor), idempotency_key))
    cid = cur.lastrowid
    g = L.new_group("CONV")
    note = f"Conversion #{cid}: {quote.from_res} -> {quote.to_res} at market prices"
    L.post_entries(conn, [
        dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=quote.from_res, delta=-quote.from_units,
             entry_type="CONVERSION", conversion_id=cid, actor=actor, note=note, price_snapshot_id=quote.snapshot_id),
        dict(group_id=g, nation_id=nation_id, bucket="AVAILABLE", resource=quote.to_res, delta=quote.to_units,
             entry_type="CONVERSION", conversion_id=cid, actor=actor, note=note, price_snapshot_id=quote.snapshot_id),
    ])
    after = L.snapshot_accounts(conn, nation_id)
    L.audit(conn, actor, "CONVERSION", f"conversion:{cid}", {
        "nation": nation_id, "from": quote.from_res, "from_units": quote.from_units, "to": quote.to_res,
        "to_units": quote.to_units, "value_cents": quote.value_cents, "price_snapshot_id": quote.snapshot_id,
        "from_price": str(quote.from_price), "to_price": str(quote.to_price)})
    return {"conversion_id": cid, "replay": False, "from_units": quote.from_units, "to_units": quote.to_units,
            "value_cents": quote.value_cents, "before": before, "after": after}
