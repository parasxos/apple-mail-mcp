"""Shared id/timestamp primitives — the single source spool.py, plans.py
and audit.py delegate to.

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
from datetime import datetime, timezone

_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")  # ASCII: strftime mints nothing else


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def is_minted_id(value: object) -> bool:
    """True iff ``value`` is a string in the minted vocabulary above —
    the proof the envelope boundary's operation_id gate demands
    (contract §2: an id is minted here or it was never minted)."""
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None
