"""Reading the real bank contents of every alliance bank the bot manages (main, and offshore if configured)."""
from __future__ import annotations

from . import money as M


async def live_holdings(pnw, settings) -> tuple[dict, dict]:
    """(combined holdings, {bank name: holdings}). Raises PnWRejected/PnWUncertain if ANY bank can't be read,
    so money is never spent against a bank balance we could not actually see."""
    per: dict = {}
    for bank in settings.banks:
        per[bank.name] = await pnw.fetch_bank_holdings(bank)
    combined: dict = {}
    for h in per.values():
        combined = M.add(combined, h)
    return combined, per
