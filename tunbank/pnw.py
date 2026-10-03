"""Politics & War API client (GraphQL v3).

Key safety idea: errors are split into two kinds.
  * PnWRejected  - PnW clearly refused (bad request, no permission, not enough
                   funds...). The transfer did NOT happen.
  * PnWUncertain - timeout / network / server error / unreadable answer. We do
                   NOT know whether the transfer happened, so we never assume
                   either way; the bot checks the bank records instead.
Outgoing transfers are never retried automatically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from . import money as M

log = logging.getLogger("tunbank.pnw")

BANKREC_FIELDS = (
    "id date sender_id sender_type receiver_id receiver_type banker_id note tax_id "
    + " ".join(M.RESOURCES)
)


class PnWError(Exception):
    pass


class PnWRejected(PnWError):
    """PnW definitively refused the request."""


class PnWUncertain(PnWError):
    """We cannot tell whether the request took effect."""


class PnWClient:
    def __init__(self, settings, min_interval: float = 0.6, timeout: float = 25.0):
        self.s = settings
        self.min_interval = min_interval
        self.timeout = timeout
        self._session = None
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0

    # ---------------------------------------------------------------- HTTP
    async def _session_get(self):
        if self._session is None or self._session.closed:
            import aiohttp  # imported here so tests can run without it

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, query: str, variables: dict | None, *, mutation: bool, bank=None) -> dict:
        async with self._rate_lock:  # simple rate limit: one call at a time, spaced out
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._post_inner(query, variables, mutation=mutation, bank=bank)
            finally:
                self._last_call = time.monotonic()

    async def _post_inner(self, query, variables, *, mutation, bank=None):
        import aiohttp

        bank = bank or self.s.main
        if mutation and not (bank.bot_key and bank.api_key):
            # Definitely nothing was sent: we refuse before any network call.
            raise PnWRejected(f"No verified bot key is configured for the {bank.name} bank, so the bot cannot send money from it.")

        headers = {"Content-Type": "application/json"}
        url = self.s.pnw_url
        # Politics & War accepts the key in the web address (?api_key=...). Sending it only as a
        # header was rejected by PnW in real tests, so "query" is the default.
        key = bank.api_key if mutation else bank.read_key
        if self.s.pnw_auth_mode == "header":
            headers["X-Api-Key"] = key
        else:
            url = f"{url}?api_key={key}"
        if mutation:
            headers["X-Bot-Key"] = bank.bot_key
            headers["X-Api-Key"] = key
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            session = await self._session_get()
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                text = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise PnWUncertain(f"Network problem talking to PnW: {type(exc).__name__}") from exc
        if status >= 500:
            raise PnWUncertain(f"PnW server error (HTTP {status})")
        if status in (401, 403):
            raise PnWRejected(f"PnW refused the API/bot key (HTTP {status}). Check your keys.")
        if status == 429:
            raise PnWRejected("PnW rate limit hit (HTTP 429). Nothing was sent; try again later.")
        if status >= 400:
            raise PnWRejected(f"PnW rejected the request (HTTP {status}).")
        import json

        try:
            body = json.loads(text)
        except ValueError as exc:
            raise PnWUncertain("PnW returned an unreadable answer") from exc
        if not isinstance(body, dict):
            raise PnWUncertain("PnW returned an unexpected answer")
        if body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in body["errors"])[:400]
            raise PnWRejected(msg)
        if "data" not in body or body["data"] is None:
            raise PnWUncertain("PnW answer had no data")
        return body["data"]

    async def query(self, query: str, variables: dict | None = None, retries: int = 3, bank=None) -> dict:
        """Read-only queries are safe to retry."""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return await self._post(query, variables, mutation=False, bank=bank)
            except PnWUncertain as exc:
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------- reading
    async def fetch_bankrecs(self, bank=None) -> list[dict]:
        """All bank records PnW still shows for an alliance bank (about 14 days)."""
        bank = bank or self.s.main
        q = ("query($id:[Int]){ alliances(id:$id, first:1){ data{ id bankrecs{ "
             + BANKREC_FIELDS + " } } } }")
        data = await self.query(q, {"id": [bank.alliance_id]}, bank=bank)
        rows = data.get("alliances", {}).get("data") or []
        if not rows:
            raise PnWRejected("Alliance not found - check ALLIANCE_ID.")
        recs = rows[0].get("bankrecs")
        if recs is None:
            raise PnWRejected(
                "PnW did not return bank records. The API key's nation must be in this "
                "alliance with bank-view permission.")
        return recs

    async def fetch_bank_holdings(self, bank=None) -> dict:
        """The REAL bank contents of one alliance, as exact units."""
        bank = bank or self.s.main
        q = ("query($id:[Int]){ alliances(id:$id, first:1){ data{ id "
             + " ".join(M.RESOURCES) + " } } }")
        data = await self.query(q, {"id": [bank.alliance_id]}, bank=bank)
        rows = data.get("alliances", {}).get("data") or []
        if not rows:
            raise PnWRejected("Alliance not found - check ALLIANCE_ID.")
        row = rows[0]
        if all(row.get(r) is None for r in M.RESOURCES):
            raise PnWRejected("PnW did not return the bank contents (missing bank-view permission?).")
        return M.clean({r: M.to_units(row.get(r)) for r in M.RESOURCES})

    async def fetch_prices(self) -> dict:
        """Latest PnW trade prices {resource: price per unit}.

        Current PnW schema (confirmed from PnW's own error message): `tradeprices(first: Int, page: Int)` returns a
        paginator `{ paginatorInfo { lastPage } data { id date <resources> } }`. We read page 1; if the list turns out to
        be oldest-first we read the LAST page, and always take the newest row. If PnW says a field doesn't exist, that
        one field is dropped and the request retried (so one rename can't blank every price)."""
        import re

        fields = ["id", "date"] + list(M.NON_CASH)
        info = True

        async def page(n: int):
            nonlocal info
            for _ in range(len(fields) + 3):
                q = ("{ tradeprices(first: 25, page: %d){ %s data{ %s } } }"
                     % (n, "paginatorInfo{ lastPage }" if info else "", " ".join(fields)))
                try:
                    return (await self.query(q)).get("tradeprices") or {}
                except PnWRejected as exc:
                    msg = str(exc)
                    m = re.search(r'Cannot query field "(\w+)" on type "Tradeprice"', msg)
                    if m and m.group(1) in fields and m.group(1) != "id":
                        fields.remove(m.group(1))
                        continue
                    if info and 'on type "PaginatorInfo"' in msg:
                        info = False
                        continue
                    raise
            raise PnWRejected("PnW did not accept the price request.")

        def key(r):
            try:
                ident = int(r.get("id") or 0)
            except (TypeError, ValueError):
                ident = 0
            return (str(r.get("date") or ""), ident)

        first = await page(1)
        rows = list(first.get("data") or [])
        if not rows:
            raise PnWUncertain("PnW returned no trade prices")
        last_page = int(((first.get("paginatorInfo") or {}).get("lastPage")) or 1)
        if last_page > 1 and key(rows[0]) <= key(rows[-1]):          # oldest-first: the newest rows are on the last page
            rows += list((await page(last_page)).get("data") or [])
        row = sorted(rows, key=key, reverse=True)[0]
        return {r: Decimal(str(row[r])) for r in M.NON_CASH if row.get(r) is not None}

    async def fetch_alliance_members(self) -> dict:
        """{nation_id: nation_name} for real (non-applicant) alliance members."""
        q = ("query($id:[Int]){ alliances(id:$id, first:1){ data{ id nations{ id nation_name "
             "alliance_position } } } }")
        data = await self.query(q, {"id": [self.s.alliance_id]})
        rows = data.get("alliances", {}).get("data") or []
        if not rows:
            raise PnWRejected("Alliance not found - check ALLIANCE_ID.")
        out = {}
        for n in rows[0].get("nations") or []:
            if str(n.get("alliance_position", "")).upper() == "APPLICANT":
                continue
            out[int(n["id"])] = n.get("nation_name") or ""
        return out

    async def fetch_tax_brackets(self) -> list[dict]:
        """The alliance's tax brackets exactly as PnW reports them.

        I could not verify the field list offline, so this asks for the likely fields and, if PnW
        says one doesn't exist, drops it and retries. Nothing is ever invented or guessed."""
        import re

        fields = ["id", "bracket_name", "tax_rate", "resource_tax_rate", "date_modified"]
        for _ in range(len(fields) + 1):
            q = ("query($id:[Int]){ alliances(id:$id, first:1){ data{ id tax_brackets{ "
                 + " ".join(fields) + " } } } }")
            try:
                data = await self.query(q, {"id": [self.s.alliance_id]})
            except PnWRejected as exc:
                m = re.search(r'Cannot query field "(\w+)" on type "TaxBracket"', str(exc))
                if m and m.group(1) in fields and m.group(1) != "id":
                    fields.remove(m.group(1))
                    continue
                raise
            rows = data.get("alliances", {}).get("data") or []
            if not rows:
                raise PnWRejected("Alliance not found - check ALLIANCE_ID.")
            return rows[0].get("tax_brackets") or []
        raise PnWRejected("PnW did not accept the tax bracket request.")

    async def fetch_nation(self, nation_id: int) -> dict | None:
        q = ("query($id:[Int]){ nations(id:$id, first:1){ data{ id nation_name alliance_id "
             "alliance_position discord } } }")
        data = await self.query(q, {"id": [int(nation_id)]})
        rows = data.get("nations", {}).get("data") or []
        return rows[0] if rows else None

    # ------------------------------------------------------------- writing
    async def bank_withdraw(self, receiver_id: int, amounts: dict, note: str, receiver_type: int = 1, bank=None) -> dict:
        """Send resources OUT of `bank` (default: main). NEVER auto-retried.

        The PnW mutation has no "sender" argument: money leaves the alliance bank of the nation that owns
        the API key. So `bank` must be the one whose credentials are the right nation's.
        Returns the PnW bank record. Raises PnWRejected (did not happen) or PnWUncertain (unknown)."""
        var_defs = ["$receiver:ID!", "$note:String"]
        args = [f"receiver_type:{int(receiver_type)}", "receiver:$receiver", "note:$note"]
        variables: dict = {"receiver": str(int(receiver_id)), "note": note}
        for res, units in amounts.items():
            var_defs.append(f"${res}:Float")
            args.append(f"{res}:${res}")
            variables[res] = M.units_to_float(units)
        q = (f"mutation({', '.join(var_defs)}){{ bankWithdraw({', '.join(args)}){{ "
             + BANKREC_FIELDS + " } }")
        data = await self._post(q, variables, mutation=True, bank=bank)
        rec = data.get("bankWithdraw")
        if not rec or rec.get("id") in (None, ""):
            raise PnWUncertain("PnW answered but gave no bank record id")
        return rec
