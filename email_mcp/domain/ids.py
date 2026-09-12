"""Shared id/timestamp primitives — the single source spool.py, plans.py,
the audit ledger and the use cases delegate to: ids are minted here,
stamps are written (``iso``) and read back (``parse_timestamp``,
``bound_interval``) here, below every caller.

One id vocabulary everywhere: ``<UTC stamp>-<6-byte hex>``
(e.g. ``20260730T101502Z-a1b2c3d4e5f6``) — sortable, greppable,
collision-safe across processes. The stamp has one-second resolution, so
the random suffix alone carries uniqueness inside a burst: 24 bits
(3 bytes) collide at ~1-in-3000 for two same-second mints and near-
certainly for thousands (birthday bound) — 48 bits keep the red team's
10k-mint burst collision-free by nine orders of magnitude.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")  # ASCII: strftime mints nothing else
_CALENDAR_PREFIX = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(stamp: str | None) -> datetime | None:
    """Parse stored timestamps defensively; naive legacy values mean UTC."""
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(
        tzinfo=timezone.utc,
    )


def bound_interval(value: str) -> tuple[datetime, datetime]:
    """The closed UTC interval an audit since/until bound denotes.

    A calendar prefix (2026, 2026-07, 2026-07-29) spans its whole period;
    a full timestamp names one instant (offset or Z honoured, naive means
    UTC), so equivalent spellings denote the same interval. ValueError
    for anything else. The one place the bound grammar lives: the use
    case, the ledger reader and the CLI all ask here.
    """
    match = _CALENDAR_PREFIX.fullmatch(value)
    if match is None:
        instant = parse_timestamp(value)
        if instant is None:
            raise ValueError(
                f"invalid ISO-8601 bound {value!r} "
                "(prefixes allowed, e.g. 2026-07 or 2026-07-29)"
            )
        return instant, instant
    year, month, day = (None if g is None else int(g) for g in match.groups())
    # datetime() is the calendar validator: 2026-00 and 2026-09-31 raise
    # here as ValueError like any other malformed bound.
    start = datetime(year, 1 if month is None else month,
                     1 if day is None else day, tzinfo=timezone.utc)
    if day is not None:
        end = start + timedelta(days=1)
    elif month is not None:
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return start, end - timedelta(microseconds=1)


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def is_minted_id(value: object) -> bool:
    """True iff ``value`` is a string in the minted vocabulary above —
    the proof the envelope boundary's operation_id gate demands
    (contract §2: an id is minted here or it was never minted)."""
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None
