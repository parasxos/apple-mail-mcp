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

The manifest is the commit record. Every create/rewrite is written to a
unique file in the same directory, fsynced, atomically replaced, then the
directory is fsynced. A failed rewrite therefore leaves the last valid
manifest in place. Read-side scans count raw records as well as readable
records and report broken pairs, corrupt JSON and interrupted temp files;
one bad record never disappears as a false-empty queue and never prevents
healthy siblings from being dispatched.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, ids, state

STATES = state.SPOOL_STATES


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


@dataclass(frozen=True)
class IntegrityIssue:
    """One record the spool cannot account for safely."""

    code: str
    state: str
    id: str | None
    path: str
    detail: str


@dataclass
class Scan:
    """A state-directory inventory: raw files, readable records, issues."""

    state: str
    entries: list[Entry] = field(default_factory=list)
    manifest_files: int = 0
    eml_files: int = 0
    issues: list[IntegrityIssue] = field(default_factory=list)
    artifact_ids: tuple[str, ...] = ()

    @property
    def readable_manifests(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        return not self.issues


# Single source in ids.py (shared with plans.py and the audit ledger);
# the names stay public here so call sites and monkeypatches don't churn.
utcnow = ids.utcnow
iso = ids.iso
new_id = ids.new_id


def _paths(state: str, id: str) -> tuple[Path, Path]:
    d = config.spool_dir() / state
    return d / f"{id}.eml", d / f"{id}.json"


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes, handling legal short writes. Test injection seam."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "zero-byte write")
        view = view[written:]


def _sync_dir(directory: Path) -> None:
    """Persist a rename/unlink in its parent directory where supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        try:
            os.fsync(fd)
        except OSError as e:
            # Some non-POSIX test/remote filesystems reject directory fsync
            # even though rename is atomic. APFS and the supported local
            # path accept it; only the explicit "unsupported" verdicts are
            # soft, never real I/O failures such as ENOSPC/EIO.
            unsupported = {errno.EINVAL}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if e.errno not in unsupported:
                raise
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """Durably replace ``path`` without ever truncating the live file."""
    tmp = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    replaced = False
    try:
        fd = os.open(tmp, flags, 0o600)
        os.fchmod(fd, 0o600)  # deterministic even under an unusual umask
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        replaced = True
        _sync_dir(path.parent)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if not replaced:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # the integrity scan will surface a leftover temp file
        raise


def save(raw: bytes, entry: Entry) -> None:
    """Durably freeze .eml then publish its manifest as the commit record.

    The one spool write seam goes through state adoption; every other
    operation stays inside the tree it guarantees. A live-process failure
    before manifest publication removes the uncommitted .eml best-effort.
    A hard crash can still leave an orphan, which :func:`scan` reports.
    """
    d = state.State.resolve().adopt().spool / "pending"
    eml, manifest = d / f"{entry.id}.eml", d / f"{entry.id}.json"
    _atomic_write(eml, raw)
    try:
        _atomic_write(manifest, _dumps(entry))
    except BaseException:
        if not manifest.exists():
            try:
                eml.unlink(missing_ok=True)
                _sync_dir(d)
            except OSError:
                pass  # visible to scan() as manifest_missing
        raise


def _dumps(entry: Entry) -> bytes:
    return json.dumps(asdict(entry), indent=2).encode()


def _read_manifest(path: Path) -> Entry:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise TypeError("manifest root is not an object")
    return Entry(**data)


def load(state: str, id: str) -> Entry | None:
    _, manifest = _paths(state, id)
    try:
        entry = _read_manifest(manifest)
    except FileNotFoundError:
        return None
    if entry.id != id:
        raise ValueError(
            f"manifest filename id {id!r} does not match payload id "
            f"{entry.id!r}"
        )
    return entry


def read_eml(state: str, id: str) -> bytes:
    eml, _ = _paths(state, id)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(eml, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "frozen message is not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _issue(code: str, spool_state: str, path: Path, detail: str,
           *, id: str | None = None) -> IntegrityIssue:
    return IntegrityIssue(code, spool_state, id, str(path), detail)


def _temp_id(name: str) -> str | None:
    clean = name[1:] if name.startswith(".") else name
    for marker in (".json.tmp-", ".eml.tmp-", ".json.tmp", ".eml.tmp"):
        if marker in clean:
            return clean.split(marker, 1)[0] or None
    return None


def scan(spool_state: str) -> Scan:
    """Inventory one spool state without hiding anything that still exists.

    Absent spool storage is a clean fresh install. Once ``spool/`` exists,
    a missing/unreadable state directory is an integrity issue. Enumeration
    uses ``os.listdir`` because ``Path.glob`` may swallow PermissionError.
    """
    if spool_state not in STATES:
        raise ValueError(f"unknown spool state {spool_state!r}")
    root = config.spool_dir()
    d = root / spool_state
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return Scan(spool_state)
    except OSError as e:
        return Scan(spool_state, issues=[_issue(
            "spool_unreadable", spool_state, root,
            f"cannot inspect spool root: {e.strerror or e}")])
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return Scan(spool_state, issues=[_issue(
            "spool_unreadable", spool_state, root,
            "spool root is not a real directory")])
    try:
        d_stat = os.lstat(d)
    except FileNotFoundError:
        return Scan(spool_state, issues=[_issue(
            "state_missing", spool_state, d,
            "spool state directory is missing")])
    except OSError as e:
        return Scan(spool_state, issues=[_issue(
            "state_unreadable", spool_state, d,
            f"cannot inspect state directory: {e.strerror or e}")])
    if stat.S_ISLNK(d_stat.st_mode) or not stat.S_ISDIR(d_stat.st_mode):
        return Scan(spool_state, issues=[_issue(
            "state_unreadable", spool_state, d,
            "spool state path is not a real directory")])
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        return Scan(spool_state, issues=[_issue(
            "state_unreadable", spool_state, d,
            f"cannot list state directory: {e.strerror or e}")])

    manifest_names: list[str] = []
    eml_names: list[str] = []
    temp_ids: set[str] = set()
    issues: list[IntegrityIssue] = []
    for name in names:
        path = d / name
        if ".tmp-" in name or name.endswith(".tmp"):
            temp_id = _temp_id(name)
            if temp_id:
                temp_ids.add(temp_id)
            issues.append(_issue(
                "temporary_file", spool_state, path,
                "interrupted atomic write left a temporary file",
                id=temp_id))
        elif name.endswith(".json"):
            manifest_names.append(name)
        elif name.endswith(".eml"):
            eml_names.append(name)
        else:
            issues.append(_issue(
                "unexpected_file", spool_state, path,
                "unexpected file in spool state directory"))

    entries_out: list[Entry] = []
    manifest_ids = {Path(name).stem for name in manifest_names}
    eml_ids = {Path(name).stem for name in eml_names}
    for name in manifest_names:
        path = d / name
        file_id = path.stem
        try:
            lst = os.lstat(path)
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                raise OSError("manifest is not a real file")
            entry = _read_manifest(path)
        except (OSError, ValueError, TypeError) as e:
            issues.append(_issue(
                "manifest_invalid", spool_state, path,
                f"manifest cannot be read: {type(e).__name__}: {e}",
                id=file_id))
            continue
        if entry.id != file_id:
            issues.append(_issue(
                "manifest_id_mismatch", spool_state, path,
                f"filename id does not match payload id {entry.id!r}",
                id=file_id))
            continue
        entries_out.append(entry)

    for name in eml_names:
        path = d / name
        file_id = path.stem
        fd: int | None = None
        try:
            lst = os.lstat(path)
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                raise OSError("frozen message is not a real file")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
        except OSError as e:
            issues.append(_issue(
                "message_invalid", spool_state, path,
                f"frozen message cannot be read: {type(e).__name__}: {e}",
                id=file_id))
        finally:
            if fd is not None:
                os.close(fd)

    for file_id in sorted(manifest_ids - eml_ids):
        issues.append(_issue(
            "message_missing", spool_state, d / f"{file_id}.json",
            "manifest has no matching frozen .eml", id=file_id))
    for file_id in sorted(eml_ids - manifest_ids):
        issues.append(_issue(
            "manifest_missing", spool_state, d / f"{file_id}.eml",
            "frozen .eml has no matching manifest", id=file_id))

    return Scan(
        state=spool_state,
        entries=entries_out,
        manifest_files=len(manifest_names),
        eml_files=len(eml_names),
        issues=issues,
        artifact_ids=tuple(sorted(manifest_ids | eml_ids | temp_ids)),
    )


def scan_all(states: tuple[str, ...] | list[str] | None = None) -> list[Scan]:
    return [scan(s) for s in (states or STATES)]


def integrity(scans: list[Scan]) -> dict:
    """JSON-ready integrity report shared by every queue surface."""
    issues = [asdict(issue) for result in scans for issue in result.issues]
    return {
        "ok": not issues,
        "counts": {result.state: result.manifest_files for result in scans},
        "readable_counts": {
            result.state: result.readable_manifests for result in scans
        },
        "message_files": {result.state: result.eml_files for result in scans},
        "issues": issues,
    }


def entries(state: str) -> list[Entry]:
    """Readable entries only; reporting surfaces must also use scan/integrity."""
    return scan(state).entries


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
        os.replace(src_manifest, dst_manifest)
    except FileNotFoundError:
        return False
    try:
        os.replace(src_eml, dst_eml)
    except FileNotFoundError:
        pass  # .eml missing is handled by the dispatcher (parks to failed)
    _sync_dir(src_manifest.parent)
    _sync_dir(dst_manifest.parent)
    return True


def update(state: str, entry: Entry) -> None:
    _, manifest = _paths(state, entry.id)
    _atomic_write(manifest, _dumps(entry))


def move(id: str, src: str, dst: str, entry: Entry | None = None) -> None:
    """Move both files src → dst; optionally rewrite the manifest after."""
    src_eml, src_manifest = _paths(src, id)
    dst_eml, dst_manifest = _paths(dst, id)
    if src_manifest.exists():
        os.replace(src_manifest, dst_manifest)
    if src_eml.exists():
        os.replace(src_eml, dst_eml)
    _sync_dir(src_manifest.parent)
    _sync_dir(dst_manifest.parent)
    if entry is not None:
        entry.status = dst
        update(dst, entry)
