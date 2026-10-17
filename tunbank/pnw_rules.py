"""Politics & War rules used to calculate transfers and grants. PURE FUNCTIONS: no database, no network, no Discord.

!!  RULES_VERSION below is "unverified".  These numbers are the bot author's understanding of the game's rules; they could
!!  not be checked against the live game when this was written. Compare a few previews with the game / Locutus before
!!  enabling sending (/grantset live), and edit the constants here if the game differs or changes. Every preview prints the
!!  rules version and every assumption, and a nation whose data is incomplete is flagged for review - never guessed.

What is deliberately NOT modelled (the preview says so): a nation's own production (mines, farms, refineries output) is not
subtracted from what it needs; domestic-policy and war-time effects are not applied; seasonal/radiation effects on food are not
applied. `only_missing` (deducting the verified stockpile) is the only netting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from . import money as M

RULES_VERSION = "pnw-rules-2026-10-UNVERIFIED-1"
D = Decimal

# ----------------------------------------------------------------------------------------------- power
# building -> (resource used, infrastructure one plant can power, resource used per infrastructure point per day)
POWER_PLANTS = {
    "wind_power": (None, 250, D(0)),
    "nuclear_power": ("uranium", 2000, D("2.4") / 1000),
    "coal_power": ("coal", 500, D("1.2") / 100),
    "oil_power": ("oil", 500, D("1.2") / 100),
}
POWER_ORDER = ("wind_power", "nuclear_power", "coal_power", "oil_power")      # assumption when plant types are mixed in one city

# ----------------------------------------------------------------------------------------------- factories (inputs per building per day)
MANUFACTURING_INPUTS = {
    "oil_refinery": {"oil": D(3)},
    "steel_mill": {"coal": D(3), "iron": D(3)},
    "aluminum_refinery": {"bauxite": D(3)},
    "munitions_factory": {"lead": D(6)},
}

# ----------------------------------------------------------------------------------------------- cash upkeep per building per day
BUILDING_CASH_UPKEEP = {
    "coal_power": D(1200), "oil_power": D(1800), "nuclear_power": D(10500), "wind_power": D(500),
    "coal_mine": D(400), "oil_well": D(600), "uranium_mine": D(5000), "iron_mine": D(1600), "bauxite_mine": D(1600),
    "lead_mine": D(1500), "farm": D(300),
    "police_station": D(750), "hospital": D(1000), "recycling_center": D(2500), "subway": D(3250),
    "supermarket": D(600), "bank": D(1800), "shopping_mall": D(5400), "stadium": D(12150),
    "oil_refinery": D(4000), "aluminum_refinery": D(2500), "steel_mill": D(4000), "munitions_factory": D(3500),
    "barracks": D(1500), "factory": D(3000), "hangar": D(4000), "drydock": D(5000),
}
ALL_BUILDINGS = tuple(BUILDING_CASH_UPKEEP)

# ----------------------------------------------------------------------------------------------- military (cash upkeep per unit per day)
MILITARY_CASH_UPKEEP = {"soldiers": D("1.25"), "tanks": D(50), "aircraft": D(500), "ships": D(3375),
                        "missiles": D(21000), "nukes": D(31500), "spies": D(2400)}

# ----------------------------------------------------------------------------------------------- food
FOOD_PER_PERSON_PER_DAY = D(1) / 1000
FOOD_PER_SOLDIER_PER_DAY = D(1) / 500

# ----------------------------------------------------------------------------------------------- purchase prices
CITY_DISCOUNT_PROJECTS = {"urban_planning": D("0.05"), "advanced_urban_planning": D("0.05"), "metropolitan_planning": D("0.05")}
INFRA_DISCOUNT_PROJECTS = {"center_for_civil_engineering": D("0.05"), "advanced_engineering_corps": D("0.05")}
LAND_DISCOUNT_PROJECTS = {"center_for_civil_engineering": D("0.05"), "advanced_engineering_corps": D("0.05")}


def city_price(current_cities: int) -> D:
    """Price of the NEXT city when the nation owns `current_cities` (before discounts)."""
    c = D(current_cities)
    return D(50000) * (c - 1) ** 3 + D(150000) * c + D(75000)


def infra_unit_price(level) -> D:
    """Price of one infrastructure point bought while at `level` (before discounts)."""
    return D(str(abs(float(level) - 10) ** 2.2 / 710 + 300))


def land_unit_price(level) -> D:
    """Price of one acre bought while at `level` (before discounts)."""
    return D(str(0.002 * (float(level) - 20) ** 2 + 50))


def sheet() -> str:
    """The rules in plain text for ECON to compare with the game (/grantset rules)."""
    lines = [f"Rules version: {RULES_VERSION}", "", "POWER (per city, per day)"]
    for b, (res, cap, rate) in POWER_PLANTS.items():
        lines.append(f"  {b}: powers {cap} infra" + (f", uses {rate * 100:.3f} {res} per 100 infra" if res else ", uses nothing"))
    lines.append(f"  mixed plant types are filled in this order: {', '.join(POWER_ORDER)} (assumption)")
    lines += ["", "FACTORY INPUTS (per building per day)"]
    for b, inp in MANUFACTURING_INPUTS.items():
        lines.append(f"  {b}: " + ", ".join(f"{v} {k}" for k, v in inp.items()))
    lines += ["", "CASH UPKEEP (per building per day)"]
    lines.append("  " + ", ".join(f"{b} ${v:,}" for b, v in BUILDING_CASH_UPKEEP.items()))
    lines += ["", "MILITARY CASH UPKEEP (per unit per day)", "  " + ", ".join(f"{u} ${v}" for u, v in MILITARY_CASH_UPKEEP.items())]
    lines += ["", f"FOOD: {FOOD_PER_PERSON_PER_DAY} per person + {FOOD_PER_SOLDIER_PER_DAY} per soldier, per day (nation's own farms NOT subtracted)",
              "", "CITY: 50,000*(c-1)^3 + 150,000*c + 75,000 for the next city (c = cities owned now); discounts "
              + ", ".join(f"{k} {int(v * 100)}%" for k, v in CITY_DISCOUNT_PROJECTS.items()) + " (added together)",
              "INFRASTRUCTURE: per point 300 + |level-10|^2.2/710, point by point; discounts "
              + ", ".join(f"{k} {int(v * 100)}%" for k, v in INFRA_DISCOUNT_PROJECTS.items()),
              "LAND: per acre 0.002*(level-20)^2 + 50, acre by acre; discounts "
              + ", ".join(f"{k} {int(v * 100)}%" for k, v in LAND_DISCOUNT_PROJECTS.items()),
              "", "Not modelled: the nation's own production, domestic policies, wartime effects, seasons/radiation."]
    return "\n".join(lines)


# ============================================================================================== results
@dataclass
class Calc:
    """The outcome for ONE nation. `review` non-empty = cannot be trusted -> must not be sent, ECON decides."""
    nation_id: int
    amounts: dict = field(default_factory=dict)          # what to send, in units (hundredths)
    requirement: dict = field(default_factory=dict)      # what the rules say is needed, in units
    stock: dict | None = None                            # verified current stockpile in units, None = not available
    lines: list = field(default_factory=list)            # human-readable calculation, one line each
    assumptions: list = field(default_factory=list)
    review: list = field(default_factory=list)
    estimate: bool = False                               # True = an input was assumed; not presented as exact
    inputs: dict = field(default_factory=dict)           # raw numbers used, stored with the transaction

    @property
    def sendable(self) -> bool:
        return not self.review


def clean(d) -> str:
    """A Decimal as plain text without trailing zeros: 1.0 -> "1", 10 -> "10", 1.50 -> "1.5"."""
    return format(D(str(d)).normalize(), "f")


def _units(x: Decimal, *, up: bool = True) -> int:
    """Decimal PnW amount -> units (hundredths); resources are rounded UP to a whole in-game unit so a nation is never short."""
    if up:
        return int((x.to_integral_value(rounding="ROUND_CEILING")) * M.SCALE)
    return int((x * M.SCALE).quantize(D(1), rounding=ROUND_HALF_UP))


def _stock_units(nation: dict) -> dict | None:
    s = nation.get("stock")
    if not s or any(s.get(r) is None for r in M.RESOURCES):
        return None
    return {r: M.to_units(s[r]) for r in M.RESOURCES}


def _cities_ok(nation: dict, calc: Calc) -> list | None:
    cities = nation.get("cities")
    n = nation.get("num_cities")
    if not n:
        calc.review.append("the number of cities is unknown")
        return None
    if not cities or len(cities) != n:
        calc.review.append(f"city data is incomplete ({len(cities or [])} of {n} cities returned by PnW)")
        return None
    return cities


# ============================================================================================== raws
def compute_raws(nation: dict, days, *, include_cash: bool = True, only_missing: bool = False) -> Calc:
    """What a nation needs to keep operating for `days` days, from its ACTUAL buildings, infrastructure and military."""
    days = D(str(days))
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Gross consumption: the nation's own production is NOT subtracted",
                      "Domestic policy, wartime and radiation/season effects are not applied"]
    cities = _cities_ok(nation, c)
    per_day = {r: D(0) for r in M.RESOURCES}
    if cities is not None:
        for city in cities:
            infra, b = city.get("infrastructure"), city.get("buildings")
            if infra is None or b is None or any(k not in b for k in ALL_BUILDINGS):
                c.review.append(f"city #{city.get('id')} has incomplete building data")
                continue
            remaining = D(str(infra))
            used_here = []
            capacity = sum(POWER_PLANTS[p][1] * b[p] for p in POWER_PLANTS)
            powered = capacity >= infra
            for plant in POWER_ORDER:
                res, cap, rate = POWER_PLANTS[plant]
                carried = min(remaining, D(cap * b[plant]))
                if carried > 0:
                    remaining -= carried
                    if res:
                        per_day[res] += carried * rate
                        used_here.append(f"{plant} x{b[plant]} powers {carried} infra -> {carried * rate:.2f} {res}/day")
            if len([p for p in POWER_PLANTS if b[p]]) > 1:
                c.assumptions.append(f"city #{city.get('id')}: mixed power plants, filled in the order {', '.join(POWER_ORDER)}")
            if powered:
                for fac, inputs in MANUFACTURING_INPUTS.items():
                    for res, amt in inputs.items():
                        if b[fac]:
                            per_day[res] += amt * b[fac]
                            used_here.append(f"{fac} x{b[fac]} uses {amt * b[fac]} {res}/day")
            else:
                c.assumptions.append(f"city #{city.get('id')} is not fully powered (infra {infra} > capacity {capacity}); "
                                     "its factories are idle and use no inputs")
            c.lines += [f"City #{city.get('id')} (infra {infra}): " + ("; ".join(used_here) if used_here else "no fuel or inputs needed")]
            if include_cash:
                per_day["money"] += sum(BUILDING_CASH_UPKEEP[k] * b[k] for k in ALL_BUILDINGS)
    # food
    pop, mil = nation.get("population"), nation.get("military")
    if pop is None:
        c.review.append("the population is unknown, so food cannot be calculated")
    elif mil is None or mil.get("soldiers") is None:
        c.review.append("the military numbers are unknown, so food and cash upkeep cannot be calculated")
    else:
        food = D(str(pop)) * FOOD_PER_PERSON_PER_DAY + D(str(mil["soldiers"])) * FOOD_PER_SOLDIER_PER_DAY
        per_day["food"] += food
        c.lines.append(f"Food: {pop:,} people + {mil['soldiers']:,} soldiers = {food:.2f}/day")
    if include_cash:
        if mil is None or any(mil.get(u) is None for u in MILITARY_CASH_UPKEEP):
            if not any("military numbers" in r for r in c.review):
                c.review.append("the military numbers are unknown, so military cash upkeep cannot be calculated")
        else:
            m_cash = sum(MILITARY_CASH_UPKEEP[u] * D(str(mil[u])) for u in MILITARY_CASH_UPKEEP)
            per_day["money"] += m_cash
            c.lines.append(f"Military cash upkeep: ${m_cash:,.2f}/day")
    c.requirement = {r: _units(per_day[r] * days) for r in M.RESOURCES if per_day[r] * days > 0}
    c.inputs = {"days": clean(days), "per_day": {r: str(v) for r, v in per_day.items() if v}, "rules": RULES_VERSION,
                "cities": nation.get("num_cities"), "include_cash": include_cash}
    c.stock = _stock_units(nation)
    c.amounts = dict(c.requirement)
    if only_missing:
        if c.stock is None:
            c.review.append("the current stockpile is not available, so only the missing amount cannot be verified")
        else:
            c.amounts = {r: max(0, a - c.stock.get(r, 0)) for r, a in c.requirement.items()}
            c.amounts = {r: a for r, a in c.amounts.items() if a > 0}
    if not c.review and not c.requirement:
        c.lines.append("Nothing is needed.")
    return c


def apply_daily_cash_rule(calc: Calc, nation: dict, threshold_dollars: int) -> None:
    """`no_daily_cash`: never send cash beyond what keeps the nation at or below the daily-bonus threshold. Only ever REDUCES cash.

    Interpretation (documented, please confirm): the daily login bonus is kept by not lifting the nation's cash above the
    threshold, so cash is sent only up to (threshold - current cash), and none if the nation is already at or above it.
    """
    if "money" not in calc.amounts:
        return
    if threshold_dollars <= 0:
        calc.review.append("no_daily_cash was requested but the daily-bonus threshold is not configured (/grantset dailycash)")
        return
    cash = (nation.get("stock") or {}).get("money")
    if cash is None:
        calc.review.append("the nation's current cash is not available, so the daily-bonus rule cannot be applied")
        return
    room = max(0, M.to_units(threshold_dollars) - M.to_units(cash))
    before = calc.amounts["money"]
    allowed = min(before, room)
    if allowed <= 0:
        calc.amounts.pop("money")
    else:
        calc.amounts["money"] = allowed
    calc.lines.append(f"Daily-bonus rule: cash capped at ${M.units_to_float(allowed):,.0f} "
                      f"(threshold ${threshold_dollars:,}, nation has ${float(cash):,.0f}); was ${M.units_to_float(before):,.0f}")


# ============================================================================================== warchest
MAX_RATIO = D(10)


def compute_warchest(nation: dict, per_city: dict, ratio, *, only_missing: bool = False) -> Calc:
    """per_city: {resource: units per city} from the ECON-configured setting. Example: food 1000, steel 100 at ratio 2, 12 cities
    -> food 24,000 and steel 2,400."""
    c = Calc(nation_id=nation["id"])
    ratio = D(str(ratio))
    if ratio <= 0 or ratio > MAX_RATIO:
        c.review.append(f"the ratio must be above 0 and at most {MAX_RATIO}")
        return c
    n = nation.get("num_cities")
    if not n:
        c.review.append("the number of cities is unknown")
        return c
    if not per_city:
        c.review.append("no warchest amounts are configured (/grantset warchest)")
        return c
    for r, u in per_city.items():
        c.requirement[r] = int((D(u) * n * ratio).quantize(D(1), rounding=ROUND_HALF_UP))
        c.lines.append(f"{M.LABELS[r]}: {M.fmt_units(r, u)} per city x {n} cities x ratio {ratio.normalize():f} = {M.fmt_units(r, c.requirement[r])}")
    c.stock = _stock_units(nation)
    c.amounts = dict(c.requirement)
    if only_missing:
        if c.stock is None:
            c.review.append("the current stockpile is not available, so only the missing amount cannot be verified")
        else:
            c.amounts = {r: max(0, a - c.stock.get(r, 0)) for r, a in c.requirement.items()}
            c.amounts = {r: a for r, a in c.amounts.items() if a > 0}
            c.lines.append("Deducted the verified stockpile: " + ", ".join(
                f"{M.LABELS[r]} {M.fmt_units(r, c.stock.get(r, 0))}" for r in c.requirement))
    c.inputs = {"cities": n, "ratio": clean(ratio), "per_city": per_city, "only_missing": only_missing, "rules": "config"}
    return c


# ============================================================================================== city / infrastructure / land
def _discount(nation: dict, table: dict, calc: Calc) -> D:
    projects = nation.get("projects") or {}
    total, unknown = D(0), []
    for p, pct in table.items():
        v = projects.get(p)
        if v is None:
            unknown.append(p)
        elif v:
            total += pct
            calc.lines.append(f"Discount: {p} -{int(pct * 100)}%")
    if unknown:
        calc.estimate = True
        calc.assumptions.append("ESTIMATE: PnW did not report " + ", ".join(unknown) + "; priced as if the nation does NOT have it")
    return total


def compute_city(nation: dict, how_many: int) -> Calc:
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Domestic-policy discounts are not applied (verify in game)",
                      "Discounts are added together, then taken off the price"]
    n = nation.get("num_cities")
    if not n:
        c.review.append("the number of cities is unknown")
        return c
    if how_many < 1 or how_many > 10:
        c.review.append("the number of cities must be between 1 and 10")
        return c
    disc = _discount(nation, CITY_DISCOUNT_PROJECTS, c)
    total = D(0)
    for k in range(how_many):
        base = city_price(n + k)
        price = base * (1 - disc)
        total += price
        c.lines.append(f"City {n + k + 1}: base ${base:,.0f}" + (f" - {int(disc * 100)}% = ${price:,.0f}" if disc else f" = ${price:,.0f}"))
    c.requirement = c.amounts = {"money": _units(total)}
    c.inputs = {"cities_now": n, "buying": how_many, "discount": str(disc), "rules": RULES_VERSION}
    return c


def _price_range(unit_price, start: int, end: int) -> D:
    return sum((unit_price(x) for x in range(int(start), int(end))), D(0))


def compute_infra(nation: dict, target: int) -> Calc:
    """Money to raise every city's infrastructure to `target` (cities already at or above it cost nothing)."""
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Priced point by point from each city's own infrastructure level"]
    cities = _cities_ok(nation, c)
    if cities is None:
        return c
    disc = _discount(nation, INFRA_DISCOUNT_PROJECTS, c)
    total = D(0)
    for city in cities:
        cur = city.get("infrastructure")
        if cur is None:
            c.review.append(f"city #{city.get('id')} has no infrastructure level")
            continue
        if cur >= target:
            c.lines.append(f"City #{city.get('id')}: {cur} (already at or above {target})")
            continue
        base = _price_range(infra_unit_price, cur, target)
        cost = base * (1 - disc)
        total += cost
        c.lines.append(f"City #{city.get('id')}: {cur} -> {target} = ${cost:,.0f}")
    c.requirement = c.amounts = {"money": _units(total)} if total > 0 else {}
    c.inputs = {"target": target, "cities": len(cities), "discount": str(disc), "rules": RULES_VERSION}
    if not c.review and total <= 0:
        c.lines.append("Every city is already at or above the target: nothing to grant.")
    return c


def compute_land(nation: dict, target: int) -> Calc:
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Priced acre by acre from each city's own land"]
    cities = _cities_ok(nation, c)
    if cities is None:
        return c
    disc = _discount(nation, LAND_DISCOUNT_PROJECTS, c)
    total = D(0)
    for city in cities:
        cur = city.get("land")
        if cur is None:
            c.review.append(f"city #{city.get('id')} has no land amount")
            continue
        if cur >= target:
            c.lines.append(f"City #{city.get('id')}: {cur} (already at or above {target})")
            continue
        base = _price_range(land_unit_price, math.floor(cur), math.ceil(target))
        cost = base * (1 - disc)
        total += cost
        c.lines.append(f"City #{city.get('id')}: {cur} -> {target} acres = ${cost:,.0f}")
    c.requirement = c.amounts = {"money": _units(total)} if total > 0 else {}
    c.inputs = {"target": target, "cities": len(cities), "discount": str(disc), "rules": RULES_VERSION}
    if not c.review and total <= 0:
        c.lines.append("Every city is already at or above the target: nothing to grant.")
    return c
