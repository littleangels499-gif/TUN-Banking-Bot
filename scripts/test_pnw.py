"""Tests your Politics & War key in plain English.  Run:  python scripts/test_pnw.py"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
import aiohttp  # noqa: E402

URL = os.environ.get("PNW_API_URL", "https://api.politicsandwar.com/graphql").strip()
KEY = os.environ.get("PNW_API_KEY", "")
ALLIANCE = os.environ.get("ALLIANCE_ID", "").strip()


def mask(k):
    return f"{k[:3]}...{k[-3:]} ({len(k)} characters)" if len(k) > 8 else f"({len(k)} characters)"


async def try_mode(mode, key):
    q = {"query": "{ alliances(id:[%s], first:1){ data{ id name } } }" % (ALLIANCE or "0")}
    headers, url = {"Content-Type": "application/json"}, URL
    if mode == "header":
        headers["X-Api-Key"] = key
    else:
        url = f"{URL}?api_key={key}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        async with s.post(url, json=q, headers=headers) as r:
            return r.status, (await r.text())[:300]


async def main():
    print("\nChecking your Politics & War key...\n")
    if not KEY:
        print("  FIX  PNW_API_KEY is empty in your .env file.\n")
        return
    print(f"  Key read from .env: {mask(KEY)}")
    if KEY != KEY.strip() or any(c in KEY for c in "\"' "):
        print("  FIX  The key contains a space or a quote mark. In .env it must look like:")
        print("       PNW_API_KEY=abc123...   (no quotes, no spaces)")
    if not ALLIANCE.isdigit():
        print("  FIX  ALLIANCE_ID must be a number.")
    worked = None
    for mode in ("header", "query"):
        try:
            status, body = await try_mode(mode, KEY.strip().strip("\"'"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ...  Method '{mode}': could not connect ({type(exc).__name__})")
            continue
        ok = status == 200 and '"errors"' not in body
        print(f"  {'OK  ' if ok else 'FAIL'} Method '{mode}': HTTP {status}" + ("" if ok else f" - {body[:120]}"))
        if ok and not worked:
            worked = mode
    print()
    if worked == "header":
        print("Your key works. Nothing to change. Restart the bot.\n")
    elif worked == "query":
        print("Your key works with the 'query' method. Add this line to .env (and to Railway Variables):")
        print("    PNW_AUTH_MODE=query\nThen restart the bot.\n")
    else:
        print("Neither method worked, so PnW is rejecting the key itself.")
        print("  1. Open Politics & War > Account page > API key, and copy it again (use the copy button).")
        print("  2. Paste it into .env after PNW_API_KEY= with nothing around it.")
        print("  3. Make sure the key is from a nation INSIDE this alliance.")
        print("  4. Run this test again.\n")


asyncio.run(main())
