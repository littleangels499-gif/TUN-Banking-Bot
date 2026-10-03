"""Exact money handling.

Every amount inside TUN Bank is stored as a whole number of HUNDREDTHS
("units"): $1,000.50 is stored as 100050. This avoids the tiny rounding errors
that decimal/float numbers cause, which matters for a ledger that must add up
to the cent.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

RESOURCES = (
    "money", "food", "coal", "oil", "uranium", "iron",
    "bauxite", "lead", "gasoline", "munitions", "steel", "aluminum",
)
NON_CASH = tuple(r for r in RESOURCES if r != "money")

LABELS = {
    "money": "Cash", "food": "Food", "coal": "Coal", "oil": "Oil",
    "uranium": "Uranium", "iron": "Iron", "bauxite": "Bauxite", "lead": "Lead",
    "gasoline": "Gasoline", "munitions": "Munitions", "steel": "Steel",
    "aluminum": "Aluminum",
}

ALIASES = {
    "cash": "money", "$": "money", "dollars": "money", "dollar": "money",
    "aluminium": "aluminum", "alu": "aluminum", "al": "aluminum",
    "gas": "gasoline", "muni": "munitions", "munis": "munitions",
    "ura": "uranium", "bx": "bauxite",
}

SCALE = 100
MAX_UNITS = 10**18  # far above anything possible in the game; blocks typos like 1e30

Amounts = dict  # {resource: units(int)}


class AmountError(ValueError):
    """Raised with a human-friendly message when an amount can't be understood."""


def to_units(value) -> int:
    """Convert a PnW number (float/str/None) to hundredths. Rounds half-up."""
    if value is None or value == "":
        return 0
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise AmountError(f"'{value}' is not a number") from exc
    if not d.is_finite():
        raise AmountError(f"'{value}' is not a finite number")
    return int((d * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def units_to_decimal(units: int) -> Decimal:
    return Decimal(units) / SCALE


def units_to_float(units: int) -> float:
    """Only for sending to the PnW API / spreadsheets, never for accounting."""
    return float(units_to_decimal(units))


def clean(amounts: dict) -> dict:
    """Drop zero entries and validate resource names."""
    out = {}
    for res, amt in amounts.items():
        if res not in RESOURCES:
            raise AmountError(f"Unknown resource '{res}'")
        if amt:
            out[res] = int(amt)
    return out


def add(a: dict, b: dict) -> dict:
    out = dict(a)
    for res, amt in b.items():
        out[res] = out.get(res, 0) + amt
    return {r: v for r, v in out.items() if v}


def sub(a: dict, b: dict) -> dict:
    out = dict(a)
    for res, amt in b.items():
        out[res] = out.get(res, 0) - amt
    return {r: v for r, v in out.items() if v}


def is_positive(amounts: dict) -> bool:
    return bool(amounts) and all(v > 0 for v in amounts.values())


_SUFFIX = {"k": Decimal(10) ** 3, "m": Decimal(10) ** 6, "b": Decimal(10) ** 9}


def parse_one(text: str) -> int:
    """Parse '1,000,000', '$2.5m', '750k' into units. Rejects >2 decimal places."""
    raw = text.strip().lower().replace(",", "").replace("$", "").replace("_", "")
    if not raw:
        raise AmountError("empty amount")
    mult = Decimal(1)
    if raw[-1] in _SUFFIX:
        mult = _SUFFIX[raw[-1]]
        raw = raw[:-1]
    try:
        d = Decimal(raw)
    except InvalidOperation as exc:
        raise AmountError(f"'{text}' is not a valid amount") from exc
    if not d.is_finite():
        raise AmountError(f"'{text}' is not a valid amount")
    d = d * mult
    if d <= 0:
        raise AmountError(f"'{text}' must be greater than zero")
    scaled = d * SCALE
    if scaled != scaled.to_integral_value():
        raise AmountError(f"'{text}' has more than 2 decimal places")
    units = int(scaled)
    if units > MAX_UNITS:
        raise AmountError(f"'{text}' is unrealistically large")
    return units


def resolve_resource(name: str) -> str:
    n = name.strip().lower()
    n = ALIASES.get(n, n)
    if n not in RESOURCES:
        raise AmountError(
            f"Unknown resource '{name}'. Use: " + ", ".join(RESOURCES)
        )
    return n


_TOKEN = re.compile(r"([A-Za-z$]+)\s*[=:]\s*([^\s;]+)")


def parse_amounts(text: str) -> dict:
    """Parse 'money=1m coal=1000 aluminum=500' into {resource: units}.

    Separators between items: spaces or semicolons.
    """
    if not text or not text.strip():
        raise AmountError("No amounts given. Example: money=1m coal=1000")
    leftover = _TOKEN.sub("", text).replace(";", "").strip()
    if leftover:
        raise AmountError(
            f"I couldn't understand '{leftover}'. Use the format "
            "resource=amount, e.g. money=1m coal=1000"
        )
    out: dict = {}
    for name, val in _TOKEN.findall(text):
        res = resolve_resource(name)
        if res in out:
            raise AmountError(f"'{res}' was given twice")
        out[res] = parse_one(val)
    if not out:
        raise AmountError("No amounts found. Example: money=1m coal=1000")
    return out


def fmt_units(res: str, units: int) -> str:
    d = units_to_decimal(units)
    if res == "money":
        return f"${d:,.2f}"
    s = f"{d:,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return s
