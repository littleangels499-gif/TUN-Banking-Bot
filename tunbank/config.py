"""Configuration.

Two kinds of settings:
  1. SECRETS / deployment settings come from the .env file (locally) or from
     Railway's "Variables" tab (in production). They never go in the code.
  2. BANK settings (log channel, limits, thresholds...) live inside the
     database and are changed with the /bankset commands, so upgrades never
     touch them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .util import now_iso


class ConfigError(Exception):
    """Problem with the setup, with a message a beginner can act on."""


@dataclass(frozen=True)
class BankAccess:
    """One alliance bank the bot can read from and (maybe) send from.

    PnW rule (from the API documentation): the nation that performs a withdrawal is the nation that owns
    the API key used, and a verified bot key is linked to ONE account. So sending money OUT of a bank needs
    the credentials of a nation that belongs to that bank's alliance and may withdraw from it."""
    name: str                   # "main" or "offshore"
    alliance_id: int
    read_key: str               # API key that can view this alliance's bank
    api_key: str | None         # API key of the nation that owns bot_key (None = cannot send from this bank)
    bot_key: str | None


@dataclass
class Settings:
    discord_token: str
    pnw_api_key: str
    pnw_bot_key: str
    pnw_bot_key_api_key: str
    alliance_id: int
    data_dir: Path
    owner_ids: set = field(default_factory=set)
    guild_id: int | None = None
    pnw_url: str = "https://api.politicsandwar.com/graphql"
    pnw_auth_mode: str = "query"
    offshore: BankAccess | None = None          # set when OFFSHORE_ALLIANCE_ID is configured
    main_bot_key: str = ""                      # optional: credentials of a MAIN-alliance nation (for main -> offshore)
    main_bot_api_key: str = ""
    offshore_nation_id: int | None = None
    alliance_receiver_type: int = 2             # PnW receiver_type for "an alliance" (1 = nation)

    @property
    def main(self) -> BankAccess:
        if self.offshore is not None:           # offshore mode: PNW_BOT_KEY is not assumed to belong to the main alliance
            return BankAccess("main", self.alliance_id, self.pnw_api_key,
                              self.main_bot_api_key or None, self.main_bot_key or None)
        return BankAccess("main", self.alliance_id, self.pnw_api_key, self.pnw_bot_key_api_key, self.pnw_bot_key)

    @property
    def banks(self) -> list:
        return [self.main] + ([self.offshore] if self.offshore else [])

    @property
    def bank_ids(self) -> set:
        return {b.alliance_id for b in self.banks}

    @property
    def payout(self) -> BankAccess:
        """The bank that member withdrawals and payments are sent from (offshore when there is one)."""
        return self.offshore or self.main

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tunbank.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def _need(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val or val.lower().startswith("paste"):
        raise ConfigError(
            f"The setting {name} is missing. Add it to your .env file "
            "(or to the Variables tab on Railway). See docs/02_SETUP_ENV.md."
        )
    return val


def resolve_data_dir() -> Path:
    """Where the database lives. Must be a PERSISTENT place on Railway."""
    explicit = os.environ.get("DATA_DIR", "").strip()
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    on_railway = bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
    )
    if on_railway and os.environ.get("ALLOW_EPHEMERAL_DB") != "1":
        if not volume:
            raise ConfigError(
                "SAFETY STOP: this bot is running on Railway but no Volume is "
                "attached. Without a Volume your database would be ERASED on every "
                "deploy. Add a Volume mounted at /data (docs/04_DEPLOY_RAILWAY.md), "
                "then redeploy."
            )
        path = Path(explicit or volume).resolve()
        vol = Path(volume).resolve()
        if path != vol and vol not in path.parents:
            raise ConfigError(
                f"SAFETY STOP: DATA_DIR ({path}) is not inside your Railway Volume "
                f"({vol}). Set DATA_DIR to {vol} or remove the DATA_DIR variable."
            )
        return path
    return Path(explicit or "data").resolve()


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)  # real environment variables (Railway) win over .env
    except ImportError:  # pragma: no cover
        pass

    api_key = _need("PNW_API_KEY")
    try:
        alliance_id = int(_need("ALLIANCE_ID"))
    except ValueError as exc:
        raise ConfigError("ALLIANCE_ID must be a number, e.g. 1234") from exc
    owners = set()
    for part in os.environ.get("OWNER_DISCORD_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise ConfigError("OWNER_DISCORD_IDS must be numbers separated by commas")
            owners.add(int(part))
    if not owners:
        raise ConfigError(
            "OWNER_DISCORD_IDS is missing. Put your own Discord user ID there so "
            "you can configure the bot (docs/02_SETUP_ENV.md)."
        )
    guild = os.environ.get("GUILD_ID", "").strip()

    def opt(name: str) -> str:
        v = os.environ.get(name, "").strip()
        return "" if v.lower().startswith("paste") else v

    offshore = None
    off_id = opt("OFFSHORE_ALLIANCE_ID")
    if off_id:
        if not off_id.isdigit():
            raise ConfigError("OFFSHORE_ALLIANCE_ID must be a number")
        if int(off_id) == alliance_id:
            raise ConfigError("OFFSHORE_ALLIANCE_ID must be DIFFERENT from ALLIANCE_ID (main alliance and offshore are separate)")
        for needed in ("OFFSHORE_API_KEY", "OFFSHORE_BOT_KEY"):
            if not opt(needed):
                raise ConfigError(f"{needed} is missing. With an offshore, these are the credentials of the nation that "
                                  "operates the offshore (docs/10_OFFSHORE.md).")
        offshore = BankAccess("offshore", int(off_id), opt("OFFSHORE_API_KEY"), opt("OFFSHORE_API_KEY"), opt("OFFSHORE_BOT_KEY"))
        bot_key = opt("PNW_BOT_KEY")                      # optional in offshore mode
    else:
        bot_key = _need("PNW_BOT_KEY")
    nation = opt("OFFSHORE_NATION_ID")
    return Settings(
        discord_token=_need("DISCORD_TOKEN"),
        pnw_api_key=api_key,
        pnw_bot_key=bot_key,
        pnw_bot_key_api_key=os.environ.get("PNW_BOT_KEY_API_KEY", "").strip() or api_key,
        alliance_id=alliance_id,
        data_dir=resolve_data_dir(),
        owner_ids=owners,
        guild_id=int(guild) if guild.isdigit() else None,
        pnw_url=os.environ.get("PNW_API_URL", "https://api.politicsandwar.com/graphql").strip(),
        pnw_auth_mode=os.environ.get("PNW_AUTH_MODE", "query").strip().lower(),
        offshore=offshore,
        main_bot_key=opt("MAIN_BOT_KEY"),
        main_bot_api_key=opt("MAIN_BOT_API_KEY"),
        offshore_nation_id=int(nation) if nation.isdigit() else None,
        alliance_receiver_type=int(opt("ALLIANCE_RECEIVER_TYPE") or 2),
    )


# ------------------------------------------------------------------ bank config
# key -> (default, description)
DEFAULTS: dict[str, tuple[str, str]] = {
    "econ_log_channel_id": ("", "Discord channel ID for the private ECON log"),
    "dm_members": ("1", "1 = DM members when their account changes"),
    "price_ttl_seconds": ("300", "How long market prices are cached"),
    "price_stale_seconds": ("1800", "Prices older than this are flagged STALE"),
    "price_change_warn_pct": ("40", "Warn when a price moves more than this % between snapshots"),
    "scan_interval_seconds": ("120", "How often PnW bank records are scanned"),
    "reconcile_interval_seconds": ("900", "How often automatic reconciliation runs"),
    "confirm_timeout_seconds": ("90", "How long a confirmation screen stays valid"),
    "stale_sync_minutes": ("30", "Warn if no successful PnW scan for this long"),
    "large_credit_value": ("2000000000", "Deposits worth more than this ($) raise a WARNING"),
    "mass_change_nations": ("25", "Warn if this many accounts get adjustments/openings within 1 hour"),
    "stuck_tx_minutes": ("10", "In-flight transactions older than this need review"),
    "auto_emergency_lock": ("1", "1 = CRITICAL integrity findings trigger the EMERGENCY LOCK"),
    "self_withdraw_enabled": ("1", "1 = members may use /bank withdrawself"),
    "econ_locked_withdraw_enabled": ("0", "1 = staff may withdraw from a member's LOCKED funds"),
    "require_alliance_member_deposit": ("1", "1 = only credit deposits from current alliance members"),
    "approval_threshold_value": ("0", "ECON withdrawals worth more than this ($) need a 2nd approver (0 = off)"),
    "approval_expiry_minutes": ("60", "How long an approval request stays open"),
    "tag_ignore": ("#ignore", "Deposit note that marks an alliance donation"),
    "tag_loan": ("#loan", "Deposit note that marks a loan repayment"),
    "backup_keep_daily": ("30", "How many daily backups to keep"),
    "opening_import_allowed": ("1", "1 = spreadsheet opening-balance import is allowed"),
    "offshore_access": ("STAFF", "Who may run /bank offshore: STAFF (Bankers+, default), ADMIN, or MEMBERS"),
    "offshore_keep_in_main": ("", "Amounts that must stay in the MAIN bank, e.g. money=100m (blank = none)"),
    "grant_min_level": ("MINISTER", "Lowest staff level that may give a grant: BANKER, MINISTER or ADMIN"),
    "icon_money": ("", "Icon for Cash (blank = default; use /bankset seticon)"),
    "icon_food": ("", "Icon for Food"),
    "icon_coal": ("", "Icon for Coal"),
    "icon_oil": ("", "Icon for Oil"),
    "icon_uranium": ("", "Icon for Uranium"),
    "icon_iron": ("", "Icon for Iron"),
    "icon_bauxite": ("", "Icon for Bauxite"),
    "icon_lead": ("", "Icon for Lead"),
    "icon_gasoline": ("", "Icon for Gasoline"),
    "icon_munitions": ("", "Icon for Munitions"),
    "icon_steel": ("", "Icon for Steel"),
    "icon_aluminum": ("", "Icon for Aluminum"),
}


def cfg_get(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM bank_config WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    if key not in DEFAULTS:
        raise KeyError(key)
    return DEFAULTS[key][0]


def cfg_int(conn, key: str) -> int:
    try:
        return int(float(cfg_get(conn, key) or 0))
    except ValueError:
        return int(float(DEFAULTS[key][0] or 0))


def cfg_bool(conn, key: str) -> bool:
    return cfg_get(conn, key).strip() in ("1", "true", "True", "yes")


def cfg_set(conn, key: str, value: str, actor: str):
    if key not in DEFAULTS:
        raise KeyError(key)
    conn.execute(
        "INSERT INTO bank_config(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, str(value), now_iso(), actor),
    )
