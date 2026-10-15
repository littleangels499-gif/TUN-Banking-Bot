"""Upload the PnW resource icons to the bot as APPLICATION emojis (named pnw_<resource>).

Application emojis belong to the bot itself, so they work in every server and in DMs. Afterwards run /bankset iconpack in Discord.

    python scripts/install_pnw_icons.py              # uploads the PNGs found in the icons/ folder
    python scripts/install_pnw_icons.py --download   # first tries to fetch any missing PNG from the game's website
    python scripts/install_pnw_icons.py --dry-run    # shows what it would do, uploads nothing

Needs DISCORD_TOKEN (from your .env). It never prints the token. Safe to re-run: icons already uploaded are skipped.
"""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESOURCES = ["money", "food", "coal", "oil", "uranium", "iron", "bauxite", "lead", "gasoline", "munitions", "steel", "aluminum"]
BASE_URL = os.environ.get("PNW_ICON_BASE_URL", "https://politicsandwar.com/img/resources/")   # best effort; override if it moves
MAX_BYTES = 256 * 1024                                                                         # Discord's emoji size limit


def download_missing(folder: Path) -> None:
    for r in RESOURCES:
        target = folder / f"{r}.png"
        if target.exists():
            continue
        try:
            req = urllib.request.Request(f"{BASE_URL}{r}.png", headers={"User-Agent": "TUN-Bank-icon-installer"})
            data = urllib.request.urlopen(req, timeout=15).read()
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("not a PNG")
            target.write_bytes(data)
            print(f"downloaded {r}.png")
        except Exception as exc:  # noqa: BLE001
            print(f"could not download {r}.png ({exc}); save it into {folder} by hand")


async def main(argv: list) -> int:
    dry = "--dry-run" in argv
    folder = ROOT / "icons"
    folder.mkdir(exist_ok=True)
    if "--download" in argv:
        download_missing(folder)
    files = {r: folder / f"{r}.png" for r in RESOURCES if (folder / f"{r}.png").exists()}
    if not files:
        print(f"No PNG files found in {folder}. See icons/README.txt.")
        return 1
    for r, f in files.items():
        if f.stat().st_size > MAX_BYTES:
            print(f"{r}.png is bigger than 256 KB (Discord's limit); resize it. Skipping.")
            files.pop(r)
    if dry:
        for r in files:
            print(f"would upload pnw_{r}")
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        print("DISCORD_TOKEN is not set (.env).")
        return 1
    import discord

    client = discord.Client(intents=discord.Intents.none())
    code = 0
    try:
        await client.login(token)
        have = {e.name for e in await client.fetch_application_emojis()}
        for r, f in files.items():
            name = f"pnw_{r}"
            if name in have:
                print(f"{name}: already uploaded")
                continue
            try:
                await client.create_application_emoji(name=name, image=f.read_bytes())
                print(f"{name}: uploaded")
            except discord.HTTPException as exc:
                print(f"{name}: FAILED ({exc.status})")
                code = 1
    finally:
        await client.close()
    print("Done. Now run /bankset iconpack in Discord." if not code else "Finished with errors (see above).")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
