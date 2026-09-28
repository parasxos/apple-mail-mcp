"""Triage plan store: one frozen JSON per plan under ~/.email-mcp/plans/.

Same idioms as spool.py (tmp-write-then-rename, atomic rename to claim),
minus the state directories: a status field plus a rename-claim suffix are
enough because plans are single-shot and short-lived (TTL ~10 min).

Lifecycle:  draft  --claim-->  (applying)  --finish-->  applied | failed
            draft  --TTL lapse at apply time-->  expired

Apply holds a stable lock under plans/locks through atomic manifest updates.
Housekeeping takes the same lock; process exit releases abandoned ownership.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import audit, config, state
from .domain import ids
from .domain.models import Plan, PlanAction, PlanMessage

STATUSES = ("draft", "applied", "failed", "expired")


# Single source in ids.py (shared with spool.py and the audit ledger);
# the names stay public here so call sites and monkeypatches don't churn.
utcnow = ids.utcnow
iso = ids.iso
new_id = ids.new_id


class UnknownPlanId(LookupError):
    """The id is outside the minted vocabulary, so no plan file can carry
    it — refused before it ever becomes a path (a caller's `../x` or
    `/etc/x` never reaches the filesystem)."""


def _path(plan_id: str) -> Path:
    # The one builder of plan paths: an id is minted by ids.new_id or it
    # was never minted, so this is where "inside plans_dir" is guaranteed.
    if not ids.is_minted_id(plan_id):
        raise UnknownPlanId(plan_id)
    return config.plans_dir() / f"{plan_id}.json"


def _claim_path(plan_id: str) -> Path:
    return _path(plan_id).with_suffix(".json.applying")


def _revive(data: dict, plan_id: str) -> Plan:
    """A plan from its stored JSON. The name it was read under is its
    identity: finish() renames by `plan.id`, so a file can never be
    claimed under one name and released under another."""
    data = dict(data, id=plan_id)
    data["actions"] = [PlanAction(**a) for a in data.get("actions", [])]
    data["messages"] = [PlanMessage(**m) for m in data.get("messages", [])]
    return Plan(**data)


def save(plan: Plan) -> None:
    # The one plan write seam: the store comes to exist via state adoption.
    path = state.State.resolve().adopt().plans / f"{plan.id}.json"
    _atomic_write(path, json.dumps(asdict(plan), indent=2).encode())


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)):
                raise
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError(errno.EIO, "zero-byte plan write")
        remaining = remaining[count:]


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            _write_all(stream.fileno(), payload)
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _read(plan_id: str, path: Path) -> Plan | None:
    """The plan file at `path`, or None when it is unparsable.
    FileNotFoundError passes through — absence is the caller's branch."""
    try:
        return _revive(json.loads(path.read_bytes()), plan_id)
    except (json.JSONDecodeError, TypeError):
        return None


def load(plan_id: str) -> Plan | None:
    for path in (_path(plan_id), _claim_path(plan_id)):
        try:
            return _read(plan_id, path)
        except FileNotFoundError:
            continue
    return None


def claim(plan_id: str) -> Plan | None:
    """Atomically take ownership of a DRAFT plan. None = lost the race,
    already applied/finished, or unknown id (caller disambiguates via
    load()). A finished plan's file is renamed back untouched."""
    try:
        _path(plan_id).rename(_claim_path(plan_id))
    except FileNotFoundError:
        return None
    plan = _read(plan_id, _claim_path(plan_id))
    if plan is None:
        return None
    if plan.status != "draft":
        _claim_path(plan_id).rename(_path(plan_id))  # hand it back
        return None
    return plan


@contextmanager
def _own(plan_id: str):
    _path(plan_id)
    path = state.State.resolve().adopt().plans / "locks" / plan_id
    flags = (os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError("Plan lock must be a private regular file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextmanager
def claim_owned(plan_id: str, prepare=None):
    """Validate under ownership, then publish the complete accepted selection."""
    path = _path(plan_id)
    if not path.exists():
        yield None
        return
    with _own(plan_id) as owned:
        if not owned:
            yield None
            return
        try:
            plan = _read(plan_id, path)
        except FileNotFoundError:
            plan = None
        if plan is None or plan.status != "draft":
            yield None
            return
        if prepare is not None:
            prepare(plan)
        claimed = _claim_path(plan_id)
        path.rename(claimed)
        _sync_dir(path.parent)
        if prepare is not None:
            _atomic_write(claimed, json.dumps(asdict(plan), indent=2).encode())
        yield plan


def selection_detail(plan: Plan) -> dict:
    if plan.excluded_ids is None:
        return {}
    return {"planned": len(plan.messages),
            "selected": len(plan.messages) - len(plan.excluded_ids),
            "excluded": plan.excluded_ids}


def _finish_detail(result: dict | None) -> dict | None:
    """Compact, GC-surviving extract of a finish result: counts, per-message
    failure codes and pending ids — never the full dicts, never bodies."""
    if not result:
        return None
    detail: dict = {k: result[k] for k in ("planned", "selected", "excluded", "acted", "verified")
                    if k in result}
    if "failures" in result:
        detail["failures"] = [{"id": f.get("id"), "code": f.get("code")}
                              for f in result["failures"]]
    if "pending" in result:
        detail["pending"] = [p.get("id") for p in result["pending"]]
    if "error" in result:
        detail["error"] = result["error"]
    return detail or None


def finish(plan: Plan, status: str, result: dict | None) -> None:
    """Write the final plan JSON, release the claim, and record the ONE
    `plan_finish` ledger event — this seam covers apply success, every
    apply failure site, expiry, and gc's stale-claim finalisation. The
    event carries the plan summary and compact outcomes, so the story
    outlives the plan file's 7-day GC."""
    plan.status = status
    selection = selection_detail(plan)
    plan.result = {**(result or {}), **selection} if selection else result
    save(plan)
    _claim_path(plan.id).unlink(missing_ok=True)
    _sync_dir(_path(plan.id).parent)
    try:
        detail = _finish_detail(plan.result)
    except Exception:  # noqa: BLE001 — detail is best-effort; a shaped-data
        # surprise must never turn a finished apply into an error or lose
        # the plan_finish event (audit finding F5: this expression used to
        # sit OUTSIDE emit()'s log-and-continue fence).
        detail = {"detail_error": "unrenderable result"}
    audit.emit("plan_finish", outcome=status, operation_id=plan.id,
               plan_id=plan.id, summary=plan.summary, detail=detail)


def expire(plan: Plan) -> None:
    finish(plan, "expired", None)


def all_plans() -> list[Plan]:
    out = []
    for path in sorted(config.plans_dir().glob("*.json")):
        try:
            out.append(_revive(json.loads(path.read_bytes()), path.stem))
        except (json.JSONDecodeError, TypeError, OSError):
            continue
    return out


def gc(now: datetime | None = None) -> int:
    """Housekeeping, called lazily from build_plan/apply_plan: drop plan
    files older than 7 days; finalise a stale .applying (crashed apply)
    as failed after 2x TTL so its plan id stops reading as in-flight."""
    now = now or utcnow()
    removed = 0
    horizon = now - timedelta(days=7)
    stale = now - timedelta(seconds=2 * config.triage_ttl_seconds())
    for path in config.plans_dir().glob("*.json*"):
        plan_id = path.name.lstrip(".").partition(".")[0]
        if not ids.is_minted_id(plan_id):
            continue
        with _own(plan_id) as owned:
            if not owned:
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except FileNotFoundError:
                continue
            if ".tmp-" in path.name:
                if mtime < stale:
                    path.unlink(missing_ok=True)
                    removed += 1
            elif mtime < horizon:
                path.unlink(missing_ok=True)
                removed += 1
            elif path.name.endswith(".applying") and mtime < stale:
                terminal = load(plan_id)
                if terminal is not None and terminal.status != "draft":
                    path.unlink(missing_ok=True)
                    _sync_dir(path.parent)
                    continue
                try:
                    plan = _read(plan_id, path)
                except (json.JSONDecodeError, TypeError):
                    plan = None
                if plan is not None:
                    finish(plan, "failed",
                           {"error": "stale claim: apply crashed mid-flight"})
                else:
                    path.unlink(missing_ok=True)
    return removed
