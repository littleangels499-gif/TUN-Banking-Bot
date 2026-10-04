"""Members' own PnW API keys: encrypted at rest, usable ONLY for the member's own nation, never shown or logged.

Why the keys exist: a deposit has to be made BY the member's nation. With a key the member chose to share (and with
'Whitelisted access' switched on in their PnW account), the bot can start that deposit for them, after they confirm.
The bot never credits anything because of this: balances change only when the real PnW bank record is seen.
"""
from __future__ import annotations

import logging
import re

from .util import now_iso

KEY_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")
_SECRETS: set = set()


class CredentialError(Exception):
    """Message is safe to show to the member."""


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text) -> str:
    out = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(s, "[hidden key]")
    return re.sub(r"(api_key=)[A-Za-z0-9]+", r"\1[hidden key]", out)


class RedactingFilter(logging.Filter):
    """Installed on every log handler: no registered secret can reach the console, a log file or Railway's logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
        except Exception:  # noqa: BLE001
            pass
        return True


def install_log_redaction(settings) -> None:
    for bank in settings.banks:
        for v in (bank.read_key, bank.api_key, bank.bot_key):
            register_secret(v)
    for v in (settings.discord_token, settings.pnw_api_key, settings.pnw_bot_key, settings.pnw_bot_key_api_key,
              settings.main_bot_key, settings.main_bot_api_key, settings.credential_key):
        register_secret(v)
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(RedactingFilter())
    logging.getLogger().addFilter(RedactingFilter())


class Crypto:
    """Fernet (AES-128-CBC + HMAC). The key comes from CREDENTIAL_ENCRYPTION_KEY, never from the database."""

    def __init__(self, key: str | None):
        self.fernet = None
        if key:
            try:
                from cryptography.fernet import Fernet
                self.fernet = Fernet(key.encode())
            except Exception:  # noqa: BLE001 - invalid key or library missing
                self.fernet = None

    @property
    def enabled(self) -> bool:
        return self.fernet is not None

    def encrypt(self, plain: str) -> bytes:
        if not self.fernet:
            raise CredentialError("Encrypted key storage is not set up (CREDENTIAL_ENCRYPTION_KEY).")
        return self.fernet.encrypt(plain.encode())

    def decrypt(self, blob: bytes) -> str:
        if not self.fernet:
            raise CredentialError("Encrypted key storage is not set up (CREDENTIAL_ENCRYPTION_KEY).")
        try:
            return self.fernet.decrypt(bytes(blob)).decode()
        except Exception as exc:  # noqa: BLE001
            raise CredentialError("The saved key can't be read (the encryption key changed). Please set your key again.") from exc


def hint(api_key: str) -> str:
    return "…" + api_key[-4:]


def get_row(conn, nation_id: int):
    return conn.execute("SELECT * FROM member_credentials WHERE nation_id=?", (nation_id,)).fetchone()


def save(conn, crypto: Crypto, *, nation_id: int, discord_id: str, api_key: str, verified: bool) -> str:
    """Store (or replace) the member's key. Returns the previous hint ('' if none). Records a CREDENTIAL audit entry (no key)."""
    from . import configaudit as CA

    if not KEY_RE.match(api_key or ""):
        raise CredentialError("That doesn't look like a PnW API key (letters and numbers only).")
    register_secret(api_key)
    old = get_row(conn, nation_id)
    conn.execute(
        "INSERT INTO member_credentials(nation_id,discord_id,key_enc,key_hint,created_at,verified,disabled) VALUES(?,?,?,?,?,?,0) "
        "ON CONFLICT(nation_id) DO UPDATE SET discord_id=excluded.discord_id, key_enc=excluded.key_enc, key_hint=excluded.key_hint, "
        "created_at=excluded.created_at, verified=excluded.verified, disabled=0, disabled_reason=NULL, last_used_at=NULL",
        (nation_id, str(discord_id), crypto.encrypt(api_key), hint(api_key), now_iso(), int(verified)))
    CA.record(conn, actor=discord_id, setting="member_api_key", previous=f"set ({old['key_hint']})" if old else "not set",
              new=f"set ({hint(api_key)})", target=f"nation [#{nation_id}] · key never stored in plain text", category="CREDENTIAL", only_if_changed=False)
    return old["key_hint"] if old else ""


def remove(conn, nation_id: int, actor, why: str) -> bool:
    from . import configaudit as CA

    old = get_row(conn, nation_id)
    if not old:
        return False
    conn.execute("DELETE FROM member_credentials WHERE nation_id=?", (nation_id,))
    CA.record(conn, actor=actor, setting="member_api_key", previous=f"set ({old['key_hint']})", new="removed",
              target=f"nation [#{nation_id}] · {why}", category="CREDENTIAL", only_if_changed=False)
    return True


def disable(conn, nation_id: int, actor, why: str) -> None:
    from . import configaudit as CA

    old = get_row(conn, nation_id)
    if old and not old["disabled"]:
        conn.execute("UPDATE member_credentials SET disabled=1, disabled_reason=? WHERE nation_id=?", (why[:200], nation_id))
        CA.record(conn, actor=actor, setting="member_api_key", previous="active", new="disabled", target=f"nation [#{nation_id}] · {why}",
                  category="CREDENTIAL", only_if_changed=False)


def load_for_member(conn, crypto: Crypto, *, nation_id: int, discord_id) -> str | None:
    """The decrypted key, but ONLY if it belongs to this very member on this very nation and is still enabled."""
    row = get_row(conn, nation_id)
    member = conn.execute("SELECT discord_id FROM members WHERE nation_id=?", (nation_id,)).fetchone()
    if (not row or row["disabled"] or not member or str(member["discord_id"]) != str(discord_id)
            or str(row["discord_id"]) != str(discord_id)):
        return None
    key = crypto.decrypt(row["key_enc"])
    register_secret(key)
    return key
