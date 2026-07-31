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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, ids

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
    identity: str = "default"    # sending identity; pre-0.7.0 manifests omit it
    # Which executor fires this entry: "launchd" (the local spool path) or
    # "graph" (Exchange-side deferred send). Pre-v0.8 manifests omit both
    # fields — the defaults keep them on the local path unchanged.
    executor: str = "launchd"
    graph_draft_id: str | None = None  # Exchange draft id once armed


# Single source in ids.py (shared with plans.py and the audit ledger);
# the names stay public here so call sites and monkeypatches don't churn.
utcnow = ids.utcnow
iso = ids.iso
new_id = ids.new_id


def _paths(state: str, id: str, *, create: bool = False) -> tuple[Path, Path]:
    """Resolve one entry's (.eml, .json). create=False by DEFAULT: reading a
    manifest must not materialise the spool tree, and load()/read_eml() are
    reads. Only save() — which is about to write — passes create=True."""
    d = config.spool_dir(create=create) / state
    return d / f"{id}.eml", d / f"{id}.json"


def save(raw: bytes, entry: Entry) -> None:
    """Write .eml + .json into pending/ (tmp-then-rename, so a crashed
    writer never leaves a half-visible message)."""
    eml, manifest = _paths("pending", entry.id, create=True)
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
    # create=False: a pure scan must not materialise the spool tree. doctor
    # calls this, and creating here let a read-only diagnostic build the
    # spool wherever the state root pointed. A missing dir simply globs to
    # nothing, which is the honest answer for "what is queued".
    d = config.spool_dir(create=False) / state
    out: list[Entry] = []
    unreadable: list[str] = []
    try:
        manifests = sorted(d.glob("*.json"))
    except OSError:
        # Unreadable or refused root: a scan that cannot see the spool
        # reports nothing rather than raising into a read tool.
        unreadable_manifests[state] = []
        return out
    for manifest in manifests:
        try:
            out.append(Entry(**json.loads(manifest.read_bytes())))
        except (json.JSONDecodeError, TypeError, OSError):
            # Never crash the scan — but never lose the fact either. A
            # manifest that does not parse was silently dropped from every
            # count, so `pending 0` was indistinguishable between "nothing
            # queued" and "a message we cannot read". doctor surfaces this.
            unreadable.append(manifest.name)
            continue
    unreadable_manifests[state] = unreadable
    return out


# Names of manifests the last entries() scan could not parse, per state.
# Read by doctor; deliberately not an exception — a foreign file in the
# spool must not break a read, only be reported.
unreadable_manifests: dict[str, list[str]] = {}


def unreadable(state: str) -> list[str]:
    """Manifests in `state` that entries() could not parse, newest scan."""
    return list(unreadable_manifests.get(state, []))


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
