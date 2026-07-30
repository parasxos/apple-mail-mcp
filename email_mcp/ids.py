"""Shared id/timestamp primitives — the single source spool.py, plans.py
and audit.py delegate to.

One id vocabulary everywhere: ``<UTC stamp>-<3-byte hex>``
(e.g. ``20260730T101502Z-a1b2c3``) — sortable, greppable, collision-safe
across processes.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"
