"""Tests every Politics & War connection the bot uses, in plain English.
Run:  python scripts/check_pnw.py"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tunbank import bankrec  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank.config import ConfigError, load_settings  # noqa: E402
from tunbank.pnw import PnWClient, PnWError  # noqa: E402


async def step(name, coro, show):
    try:
        result = await coro
    except PnWError as exc:
        print(f"  FAIL  {name}\n        PnW said: {exc}\n")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}\n")
        return None
    print(f"  OK    {name}: {show(result)}")
    return result


async def main():
    try:
        s = load_settings()
    except ConfigError as exc:
        print(f"\nSETUP PROBLEM:\n{exc}\n")
        return
    print("\nChecking every Politics & War connection the bank uses...\n")
    c = PnWClient(s)
    prices = await step("Market prices", c.fetch_prices(),
                        lambda r: f"{len(r)} of 11 resources priced" + (
                            "" if len(r) == 11 else " (missing: " + ", ".join(x for x in M.NON_CASH if x not in r) + ")"))
    if prices:
        print("        " + ", ".join(f"{k} ${float(v):,.0f}" for k, v in prices.items()) + "\n")
    await step("Alliance members", c.fetch_alliance_members(), lambda r: f"{len(r)} members found")
    recs = None
    for bank in s.banks:
        await step(f"{bank.name.title()} bank contents (alliance {bank.alliance_id})", c.fetch_bank_holdings(bank),
                   lambda r: f"cash ${M.units_to_float(r.get('money', 0)):,.0f} in the bank")
        got = await step(f"{bank.name.title()} bank records", c.fetch_bankrecs(bank), lambda r: f"{len(r)} records in the last ~14 days")
        recs = recs or got
    print("  Sending money:  main bank " + ("CAN" if s.main.bot_key else "cannot") + " be sent from by the bot (needs a bot key from a main-alliance nation); "
          + (f"offshore CAN be." if s.offshore and s.offshore.bot_key else "no offshore configured.") + "\n")
    if recs:
        bad = 0
        for r in recs:
            try:
                bankrec.normalize(r)
            except Exception:  # noqa: BLE001
                bad += 1
        print(f"        {len(recs) - bad} readable, {bad} unreadable\n")
    await step("Tax brackets", c.fetch_tax_brackets(), lambda r: f"{len(r)} brackets")
    await c.close()
    print("If anything says FAIL, copy that block and send it to me.\n")


asyncio.run(main())
