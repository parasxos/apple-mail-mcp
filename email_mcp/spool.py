"""Scheduled-mail spool: frozen RFC-822 files + JSON manifests.

Layout:  <spool>/{pending,sending,sent,failed,cancelled}/<id>.eml + <id>.json

A scheduled message is composed IN FULL at schedule time (recipients,
attachments, Bcc-to-self, Message-ID) and frozen as bytes — nothing is
re-read at fire time, so editing or deleting a source file after
scheduling cannot change what goes out.

Ownership hand-off is the atomic rename of the .json manifest between
state directories (rename is atomic on APFS): whichever dispatcher run
wins the rename owns the message; the loser sees FileNotFoundError and
moves on. That makes overlapping dispatcher runs double-send-safe.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config

STATES = ("pending", "sending", "sent", "failed", "cancelled")


@dataclass
class Entry:
    """Manifest for one scheduled message (mirrors <id>.json)."""

    id: str
    send_at: str                 # UTC ISO-8601
    created_at: str              # UTC ISO-8601
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    attachments: list[str]       # filenames embedded in the frozen .eml
    message_id: str
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    delivered_at: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _paths(state: str, id: str) -> tuple[Path, Path]:
    d = config.spool_dir() / state
    return d / f"{id}.eml", d / f"{id}.json"


def save(raw: bytes, entry: Entry) -> None:
    """Write .eml + .json into pending/ (tmp-then-rename, so a crashed
    writer never leaves a half-visible message)."""
    eml, manifest = _paths("pending", entry.id)
    for path, data in ((eml, raw), (manifest, _dumps(entry))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)


def _dumps(entry: Entry) -> bytes:
    return json.dumps(asdict(entry), indent=2).encode()


def load(state: str, id: str) -> Entry | None:
    _, manifest = _paths(state, id)
    try:
        return Entry(**json.loads(manifest.read_bytes()))
    except FileNotFoundError:
        return None


def read_eml(state: str, id: str) -> bytes:
    eml, _ = _paths(state, id)
    return eml.read_bytes()


def entries(state: str) -> list[Entry]:
    d = config.spool_dir() / state
    out = []
    for manifest in sorted(d.glob("*.json")):
        try:
            out.append(Entry(**json.loads(manifest.read_bytes())))
        except (json.JSONDecodeError, TypeError, OSError):
            continue  # half-written or foreign file; never crash the scan
    return out


def find(id: str) -> tuple[str, Entry] | None:
    """Locate a message id across all states."""
    for state in STATES:
        e = load(state, id)
        if e is not None:
            return state, e
    return None


def claim(id: str, src: str = "pending", dst: str = "sending") -> bool:
    """Atomically take ownership by renaming the manifest src → dst.
    Returns False if another run (or a cancel) got there first."""
    src_eml, src_manifest = _paths(src, id)
    dst_eml, dst_manifest = _paths(dst, id)
    try:
        src_manifest.rename(dst_manifest)
    except FileNotFoundError:
        return False
    try:
        src_eml.rename(dst_eml)
    except FileNotFoundError:
        pass  # .eml missing is handled by the dispatcher (parks to failed)
    return True


def update(state: str, entry: Entry) -> None:
    _, manifest = _paths(state, entry.id)
    manifest.write_bytes(_dumps(entry))


def move(id: str, src: str, dst: str, entry: Entry | None = None) -> None:
    """Move both files src → dst; optionally rewrite the manifest after."""
    src_eml, src_manifest = _paths(src, id)
    dst_eml, dst_manifest = _paths(dst, id)
    if src_manifest.exists():
        src_manifest.rename(dst_manifest)
    if src_eml.exists():
        src_eml.rename(dst_eml)
    if entry is not None:
        entry.status = dst
        update(dst, entry)
