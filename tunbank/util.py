"""Small shared helpers (time, hashing, JSON)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return utcnow().strftime(ISO_FMT)


def parse_iso(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, ISO_FMT).replace(tzinfo=dt.timezone.utc)


def seconds_since(text: str | None) -> float | None:
    """Seconds since an ISO timestamp written by now_iso(); None if missing/unparseable."""
    if not text:
        return None
    try:
        return (utcnow() - parse_iso(text)).total_seconds()
    except ValueError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jdump(obj) -> str:
    """Canonical JSON: same input always gives the same text (needed for hashes)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
