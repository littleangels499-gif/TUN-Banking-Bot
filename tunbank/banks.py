"""Reading the real bank contents of every alliance bank the bot manages (main, and offshore if configured)."""
from __future__ import annotations

from . import money as M


async def live_holdings(pnw, settings, db=None) -> tuple[dict, dict]:
    """(holdings that belong to THIS alliance, {bank name: holdings}). Raises PnWRejected/PnWUncertain if ANY bank can't be
    read, so money is never spent against a bank balance we could not actually see.

    Single-offshore mode (the original): everything in the offshore is this alliance's, so it is simply main + offshore.
    SHARED-offshore mode (db given and the registry on): the offshore physically holds several alliances' funds, so only
    this alliance's SHARE of it counts. The whole physical offshore is still reported in the per-bank detail.
    """
    per: dict = {}
    for bank in settings.banks:
        per[bank.name] = await pnw.fetch_bank_holdings(bank)
    if db is not None and settings.offshore is not None and "offshore" in per:
        from . import offshore_ledger as OL

        with db.read() as conn:
            if OL.shared_enabled(conn):
                share = OL.host_share(conn, per["offshore"])
                combined = M.add(per.get("main", {}), share)
                per = dict(per, offshore_share=share)
                return combined, per
    combined: dict = {}
    for h in per.values():
        combined = M.add(combined, h)
    return combined, per
