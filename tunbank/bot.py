"""The Discord bot: wires everything together and runs the background jobs."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import cmds_admin, cmds_audit, cmds_backup, cmds_bulk, cmds_chart, cmds_convert, cmds_deposit, cmds_grant, cmds_loan, cmds_panel, cmds_shared_offshore, cmds_trade, cmds_offshore, cmds_econ, cmds_help, cmds_market, cmds_member
from . import ledger as L
from .alerts import AlertService
from .credentials import Crypto
from .memberdeposit import MemberDepositService
from .offshore import OffshoreService
from .config import cfg_int
from .db import Database, prune_backups
from .pnw import PnWClient
from .scanner import Scanner
from .ui import Services, post_outcomes
from .util import now_iso, seconds_since
from .valuation import PriceService
from .withdrawals import WithdrawalService

log = logging.getLogger("tunbank.bot")


class TunBankBot(commands.Bot):
    def __init__(self, settings, db: Database):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.settings = settings
        self.db = db
        pnw = PnWClient(settings)
        prices = PriceService(db, pnw)
        self.svc = Services(settings=settings, db=db, pnw=pnw, prices=prices,
                            scanner=Scanner(db, pnw, prices, settings),
                            wd=WithdrawalService(db, pnw, prices, settings),
                            alerts=AlertService(db))
        self.svc.alerts.bot = self
        self.svc.offshore = OffshoreService(db, pnw, prices, settings)
        self.svc.crypto = Crypto(settings.credential_key)
        self.svc.deposits = MemberDepositService(db, pnw, prices, settings, self.svc.crypto, self.svc.scanner)
        from .trademon import TradeMonitor
        self.svc.trade_monitor = TradeMonitor(db, pnw, prices, self.svc.alerts)
        self._ready_once = False

    async def setup_hook(self):
        from . import icons
        with self.db.read() as conn:
            icons.load_from_db(conn)
        bank = app_commands.Group(name="bank", description="TUN Bank")
        bankset = app_commands.Group(name="bankset", description="TUN Bank settings, limits, roles and backups (Admin)")
        nation = app_commands.Group(name="nation", description="Your Politics & War nation")
        tax = app_commands.Group(name="tax", description="Alliance tax records (ECON)")
        audit = app_commands.Group(name="audit", description="Audit tools (ECON)")
        ledger = app_commands.Group(name="ledger", description="Ledger integrity (ECON)")
        chart = app_commands.Group(name="chart", description="Charts from your real bank data")
        bulk = app_commands.Group(name="bulk", description="Pay many nations at once from alliance funds (ECON)")
        grant = app_commands.Group(name="grant", description="Alliance-approved grants (ECON)")
        deposit = app_commands.Group(name="deposit", description="Deposit reset and restoration (Admin)")
        loan = app_commands.Group(name="loan", description="Loans: money members owe the alliance")
        trade = app_commands.Group(name="trade", description="Trade monitoring: alerts and rules (ECON)")
        offshore_grp = app_commands.Group(name="offshore", description="Shared offshore: who owns what in it")
        cmds_member.register(bank, nation, self.svc)
        cmds_econ.register(bank, self.svc)            # must come first: defines svc.do_reconcile
        cmds_admin.register(bank, bankset, ledger, self.svc)
        cmds_backup.register(bankset, self.svc)
        cmds_chart.register(chart, self.svc)
        cmds_bulk.register(bulk, self.svc)
        cmds_grant.register(grant, self.svc)
        cmds_offshore.register(bank, self.svc)
        cmds_help.register(self.tree, self.svc)
        cmds_market.register(self.tree, self.svc)
        cmds_audit.register_tax(tax, self.svc)
        cmds_audit.register_audit(audit, ledger, self.svc)
        cmds_deposit.register(deposit, self.svc)
        cmds_convert.register(bankset, self.svc)
        cmds_loan.register(loan, self.svc)
        cmds_panel.register(bankset, self.svc)
        cmds_trade.register(trade, self.svc)
        cmds_shared_offshore.register(offshore_grp, self.svc)
        self.add_view(self.svc.panel_view())                           # the banking panel keeps working after restarts
        self.add_view(cmds_convert.ConversionPanelView(self.svc))     # the panel button keeps working after restarts
        self.tree.on_error = self.on_tree_error
        for g in (bank, bankset, nation, tax, audit, ledger, chart, bulk, grant, deposit, loan, trade, offshore_grp):
            self.tree.add_command(g)
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("command error", exc_info=error)
        msg = "Something went wrong and nothing was changed unless a confirmation said otherwise. ECON can check `/ledger dashboard`."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True
        log.info("Logged in as %s", self.user)
        try:
            notes = await self.svc.wd.recover()
            if self.svc.offshore.enabled:
                notes += await self.svc.offshore.recover()
            for n in notes:
                log.warning("startup recovery: %s", n)
            await self.svc.alerts.flush_events()
            with self.db.read() as conn:
                stuck = [r["id"] for r in conn.execute("SELECT id FROM bulk_batches WHERE status='RUNNING'")]
            if stuck:
                from .alerts import Card, ORANGE
                await self.svc.alerts.econ(Card("⏸️ Bulk transfer interrupted", "The bot restarted while bulk transfer(s) "
                    + ", ".join(f"#{i}" for i in stuck) + " were running. Check `/bulk status`, then `/bulk resume`. "
                    "Rows already sent are never repeated.", ORANGE))
        except Exception:  # noqa: BLE001
            log.exception("startup recovery failed")
        try:
            from . import configaudit as CA
            with self.db.tx() as conn:
                changed = CA.check_env_changes(conn, self.settings)
            if changed:
                log.warning("%s startup setting(s) changed since the last start; recorded in the audit log", changed)
            await self.svc.alerts.flush_config_audit()
        except Exception:  # noqa: BLE001
            log.exception("startup configuration check failed")
        try:
            from .records import backfill_tax_turns
            with self.db.tx() as conn:
                n = backfill_tax_turns(conn)
            if n:
                log.info("Grouped existing tax records into %s turns (already announced)", n)
        except Exception:  # noqa: BLE001
            log.exception("tax-turn backfill failed")
        self.scan_loop.start()
        self.recon_loop.start()
        self.backup_loop.start()
        self.audit_loop.start()
        self.trade_loop.start()

    async def close(self):
        for t in (self.scan_loop, self.recon_loop, self.backup_loop, self.audit_loop, self.trade_loop):
            t.cancel()
        await self.svc.pnw.close()
        self.db.close()
        await super().close()

    # ------------------------------------------------------------- loops
    @tasks.loop(seconds=30)
    async def scan_loop(self):
        """Runs every 30s but only scans when the configured interval has passed."""
        try:
            with self.db.read() as conn:
                interval = cfg_int(conn, "scan_interval_seconds")
                last = L.get_state(conn, "last_scan_attempt")
            if last and (seconds_since(last) or 9999) < interval:
                return
            with self.db.tx() as conn:
                L.set_state(conn, "last_scan_attempt", now_iso())
            res = await self.svc.scanner.scan()
            if res.ok:
                await post_outcomes(self.svc, res.outcomes)
            else:
                log.warning("scan failed: %s", res.error)
            await self.svc.alerts.flush_events()
        except Exception:  # noqa: BLE001
            log.exception("scan loop error")

    @tasks.loop(seconds=30)
    async def trade_loop(self):
        """Watches completed trades; only does anything when the configured interval has passed."""
        try:
            await self.svc.trade_monitor.poll_if_due()
        except Exception:  # noqa: BLE001
            log.exception("trade monitor error")

    @tasks.loop(seconds=60)
    async def recon_loop(self):
        try:
            with self.db.read() as conn:
                interval = cfg_int(conn, "reconcile_interval_seconds")
                last = L.get_state(conn, "last_recon_attempt")
            if last and (seconds_since(last) or 9999) < interval:
                return
            with self.db.tx() as conn:
                L.set_state(conn, "last_recon_attempt", now_iso())
            await self.svc.do_reconcile("system:scheduled")
        except Exception:  # noqa: BLE001
            log.exception("reconcile loop error")
        try:
            await self.svc.announce_overdue_loans()
        except Exception:  # noqa: BLE001
            log.exception("overdue loan check failed")

    @tasks.loop(seconds=20)
    async def audit_loop(self):
        """Nothing stays unannounced: any recorded change not yet in the audit channel is posted here."""
        try:
            await self.svc.alerts.flush_config_audit()
        except Exception:  # noqa: BLE001
            log.exception("config audit flush failed")

    @tasks.loop(minutes=30)
    async def backup_loop(self):
        """One consistent backup per UTC day, kept inside the persistent volume."""
        try:
            today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            target = self.settings.backup_dir / f"daily-{today}.db"
            if target.exists():
                return
            await asyncio.to_thread(self.db.backup_to, target)
            with self.db.read() as conn:
                keep = cfg_int(conn, "backup_keep_daily")
            prune_backups(self.settings.backup_dir, keep_daily=max(7, keep))
            log.info("Daily backup written: %s", target)
        except Exception:  # noqa: BLE001
            log.exception("backup failed")

    @scan_loop.before_loop
    @recon_loop.before_loop
    @backup_loop.before_loop
    @trade_loop.before_loop
    @audit_loop.before_loop
    async def _wait(self):
        await self.wait_until_ready()
