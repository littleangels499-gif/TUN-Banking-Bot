"""A $1 experiment: can TUN Bank's bot key start a deposit using a MEMBER's own API key?

Run on your PC:   python scripts/test_member_deposit.py

What it does: asks for the API key of a nation in your main alliance (typed hidden, never saved), then asks Politics & War to
deposit $1 from THAT nation into its alliance bank using your verified bot key. You then see PnW's exact answer.
Use your own main nation. Before running, switch "Whitelisted access" ON in that nation's PnW Account page.
"""
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tunbank import credentials as CR  # noqa: E402
from tunbank.config import ConfigError, load_settings  # noqa: E402
from tunbank.pnw import PnWClient, PnWRejected, PnWUncertain  # noqa: E402


async def main():
    try:
        s = load_settings()
    except ConfigError as exc:
        print(f"\nSETUP PROBLEM:\n{exc}\n")
        return
    bot = s.deposit_bot_key
    if not bot:
        print("\nNo verified bot key is configured (OFFSHORE_BOT_KEY or MAIN_BOT_KEY), so there is nothing to test.\n")
        return
    print("\nThis test will move $1 from a nation of YOUR choice into that nation's own alliance bank.")
    print("It uses your verified bot key together with that nation's API key (which you type below, hidden).")
    print("Make sure 'Whitelisted access' is switched ON in that nation's PnW Account page first.\n")
    if input("Type YES to continue: ").strip() != "YES":
        print("Cancelled. Nothing was sent.\n")
        return
    key = getpass.getpass("Paste that nation's API key (nothing will show as you type): ").strip()
    CR.register_secret(key)
    c = PnWClient(s)
    try:
        owner = await c.fetch_key_owner(key)
        print(f"  Key check: " + (f"this key belongs to nation #{owner}" if owner is not None else "PnW does not say who owns a key (that is fine)"))
    except (PnWRejected, PnWUncertain) as exc:
        print(f"  Key check failed: {CR.redact(exc)}\n  (PnW did not accept that key. Check you copied it correctly.)\n")
        await c.close()
        return
    try:
        rec = await c.bank_deposit({"money": 100}, "TUN-TEST", key, bot)
    except PnWRejected as exc:
        print("\nRESULT: PnW REFUSED the deposit. Nothing was sent. PnW said:\n   " + CR.redact(exc))
        print("\nWhat this means:")
        print("  * If it mentions whitelisted access / not authorised: switch 'Whitelisted access' on in the Account page and run this again.")
        print("  * If it says the API key must belong to the bot key's own account: PnW does NOT allow other nations' keys with this bot key.")
        print("    Then members have to deposit by hand; keep using /bank deposit for the exact steps. Nothing is lost.")
    except PnWUncertain as exc:
        print(f"\nRESULT: unclear ({CR.redact(exc)}). Check the alliance bank records before running it again.")
    else:
        print(f"\nRESULT: SUCCESS. PnW accepted the deposit (bank record #{rec.get('id')}, sender nation #{rec.get('sender_id')}).")
        print("This proves a member's own API key + the TUN bot key can start a deposit. Direct member deposits will work.")
        print("$1 is now in the alliance bank; the bot will treat it as a normal deposit for that nation if you let it scan.")
    await c.close()
    print()


asyncio.run(main())
