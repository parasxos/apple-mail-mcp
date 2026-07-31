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

# The shape `new_id` mints, as a matcher. Contract §2 says operation_id is
# "never minted *for* a failure" — so a caller-supplied argument only earns
# a place in a failure envelope if it LOOKS like an id we minted. Anything
# else (a typo, a hostile 60 KB string) is a reference to nothing.
_ID_RE = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{12}\Z")


def is_minted_id(value: str) -> bool:
    """True when `value` has the exact shape of an id this module mints."""
    return bool(_ID_RE.match(value))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"
