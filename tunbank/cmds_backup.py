"""/bankset backup and /bankset restorestage (Administrator only)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from . import alerts as A
from . import backup as BK
from . import ledger as L
from .ui import Services, actor_label, confirm, need, reply, thinking

log = logging.getLogger("tunbank.backup")


def register(bankset: app_commands.Group, svc: Services):
    @bankset.command(name="backup", description="Admin: make a backup now and send it to you privately")
    async def backup(interaction: discord.Interaction):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = svc.settings.backup_dir / f"manual-{stamp}.db"
        try:
            await asyncio.to_thread(svc.db.backup_to, dest)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual backup failed")
            return await reply(interaction, f"Backup failed: {exc}")

        def note():
            with svc.db.tx() as conn:
                L.audit(conn, interaction.user.id, "BACKUP_CREATED", dest.name, {})
        await asyncio.to_thread(note)
        size = dest.stat().st_size
        if size > 24 * 1024 * 1024:
            return await reply(interaction, f"Backup saved on the server as `{dest.name}` but it is too big "
                                            "to send through Discord (25 MB limit). See docs/06_BACKUP_RESTORE.md.")
        await reply(interaction, f"Backup `{dest.name}` ({size // 1024} KB). **Keep this file somewhere safe** "
                                 "(it contains all balances).", file=discord.File(str(dest), filename=dest.name))

    @bankset.command(name="restorestage", description="Admin: stage a backup file to be restored at the next restart")
    @app_commands.describe(file="A TUN Bank backup (.db) made by /bankset backup")
    async def restorestage(interaction: discord.Interaction, file: discord.Attachment):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        if file.size > 200 * 1024 * 1024:
            return await reply(interaction, "That file is too large.")
        data = await file.read()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "upload.db"
            tmp.write_bytes(data)
            try:
                info = await asyncio.to_thread(BK.validate_backup, tmp)
            except BK.BackupError as exc:
                return await reply(interaction, f"This backup was NOT accepted: {exc}")
            c = A.Card("Stage a RESTORE?", "The backup passed all health checks.", A.RED)
            c.add("Contains", f"{info['ledger_entries']} ledger entries, {info['audit_entries']} audit entries", True)
            c.add("What happens", "At the next restart the CURRENT database is saved as a safety copy and replaced "
                                  "by this backup. Anything recorded after the backup was made disappears from the "
                                  "bot's books (deposits are re-detected from PnW; check `/bank review`).")
            c.add("After confirming", "Restart the service on Railway (Deployments → Restart).")
            if not await confirm(svc, interaction, c):
                return await reply(interaction, "Cancelled. Nothing changed.")
            await asyncio.to_thread(BK.stage_restore, svc.settings.data_dir, tmp)

        def note():
            with svc.db.tx() as conn:
                from . import configaudit as CA
                CA.record(conn, actor=interaction.user.id, setting="database_restore", previous="current database",
                          new=f"backup staged ({info['ledger_entries']} ledger entries); applies at next restart", target=file.filename,
                          category="RESTORE", only_if_changed=False)
                L.audit(conn, interaction.user.id, "RESTORE_STAGED", file.filename, info)
        await asyncio.to_thread(note)
        await svc.alerts.econ(A.Card("Database restore STAGED", f"By {actor_label(interaction)}. Applies at next restart.", A.RED))
        await reply(interaction, "Restore staged. Restart the service on Railway to apply it.")
