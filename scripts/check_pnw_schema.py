"""Check, against the LIVE Politics & War API, that every field the transfer/grant calculations ask for really exists,
and whether your key can see nation stockpiles.

    python scripts/check_pnw_schema.py                 # field names only
    python scripts/check_pnw_schema.py --nation 12345  # also fetch ONE real nation and show what comes back (numbers, no keys)

Uses PNW_API_KEY from your .env (read-only queries; never sends anything; never prints the key). Run this BEFORE the first
live test of /transfer raws or /grant ...: a missing field means that command cannot calculate and will flag nations for review.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tunbank import pnw as P  # noqa: E402


async def gql(session, url, key, query, variables=None):
    async with session.post(f"{url}?api_key={key}", json={"query": query, "variables": variables or {}}) as r:
        return r.status, json.loads(await r.text())


async def main(argv) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("PNW_API_KEY", "").strip()
    if not key:
        print("PNW_API_KEY is not set (.env).")
        return 1
    import aiohttp
    url = os.environ.get("PNW_API_URL", "https://api.politicsandwar.com/graphql")
    bad = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        for type_name, needed in (("Nation", P.NATION_SCALAR_FIELDS + P.NATION_PROJECT_FIELDS), ("City", P.CITY_FIELDS)):
            status, body = await gql(s, url, key, '{ __type(name:"%s"){ fields{ name } } }' % type_name)
            if status != 200 or not body.get("data", {}).get("__type"):
                print(f"{type_name}: could not read the schema (HTTP {status}). Introspection may be disabled; use --nation instead.")
                bad += 1
                continue
            have = {f["name"] for f in body["data"]["__type"]["fields"]}
            missing = [f for f in needed if f not in have]
            print(f"{type_name}: {len(needed) - len(missing)}/{len(needed)} fields exist" + (f"  MISSING: {', '.join(missing)}" if missing else "  OK"))
            bad += bool(missing)
        if "--nation" in argv:
            nid = int(argv[argv.index("--nation") + 1])
            client = P.PnWClient.__new__(P.PnWClient)
            q = ("query($id:[Int]){ nations(id:$id, first:1){ data{ " + " ".join(P.NATION_SCALAR_FIELDS + P.NATION_PROJECT_FIELDS)
                 + " cities{ " + " ".join(P.CITY_FIELDS) + " } } } }")
            status, body = await gql(s, url, key, q, {"id": [nid]})
            if body.get("errors"):
                print("PnW rejected the real query:", "; ".join(str(e.get("message")) for e in body["errors"])[:400])
                bad += 1
            else:
                rows = body["data"]["nations"]["data"]
                if not rows:
                    print("That nation was not returned.")
                else:
                    n = rows[0]
                    res = {r: n.get(r) for r in P.RESOURCE_FIELDS}
                    hidden = [r for r, v in res.items() if v is None]
                    print(f"nation {nid}: {n.get('num_cities')} cities, population {n.get('population')}, {len(n.get('cities') or [])} cities returned")
                    print("stockpile visible:", "YES" if not hidden else "NO - these came back empty: " + ", ".join(hidden))
                    nulls = [k for k in P.NATION_PROJECT_FIELDS if n.get(k) is None]
                    print("projects reported:", "all" if not nulls else "MISSING " + ", ".join(nulls))
                    c0 = (n.get("cities") or [{}])[0]
                    print("first city building fields empty:", [k for k in P.CITY_FIELDS if c0.get(k) is None] or "none")
    print("\nRESULT:", "OK - the calculations have every field they need." if not bad else
          "PROBLEMS FOUND - tell me the exact lines above and I will adjust the field list.")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
