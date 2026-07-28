"""Triage plan store: one frozen JSON per plan under ~/.email-mcp/plans/.

Same idioms as spool.py (tmp-write-then-rename, atomic rename to claim),
minus the state directories: a status field plus a rename-claim suffix are
enough because plans are single-shot and short-lived (TTL ~10 min).

Lifecycle:  draft  --claim-->  (applying)  --finish-->  applied | failed
            draft  --TTL lapse at apply time-->  expired

The claim is the atomic rename <id>.json -> <id>.json.applying: whichever
process wins the rename owns the apply; the loser sees FileNotFoundError.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

STATUSES = ("draft", "applied", "failed", "expired")


@dataclass
class PlanAction:
    action: str                      # move_to|mark_read|mark_unread|flag|unflag|delete
    mailbox: str | None = None       # move_to only — decoded slash-joined path
    color: int | None = None         # flag only — 0..6


@dataclass
class PlanMessage:
    rowid: int
    account: str                     # UUID host of the mailbox URL
    scheme: str                      # "local" | "imap" | "ews" | ...
    mailbox: str                     # decoded source-mailbox name (AppleScript name)
    mailbox_rowid: int
    subject: str
    from_addr: str
    date: str                        # ISO-8601
    unread: bool
    message_id_header: str           # <>-stripped; "" when the index has none
    global_message_id: int | None    # for move outcome (b) relocation probe
    pre: dict                        # {"read": int, "flagged": int, "flag_color": int}


@dataclass
class Plan:
    id: str
    created_at: str                  # UTC ISO
    expires_at: str                  # UTC ISO
    status: str                      # draft|applied|failed|expired
    query: dict                      # original filters (audit trail)
    actions: list[PlanAction]
    target: dict | None              # move_to only: {account, mailbox, mailbox_rowid, url}
    messages: list[PlanMessage]
    summary: str
    result: dict | None = None       # filled by apply


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_id(now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _path(plan_id: str) -> Path:
    return config.plans_dir() / f"{plan_id}.json"


def _claim_path(plan_id: str) -> Path:
    return config.plans_dir() / f"{plan_id}.json.applying"


def _revive(data: dict) -> Plan:
    data = dict(data)
    data["actions"] = [PlanAction(**a) for a in data.get("actions", [])]
    data["messages"] = [PlanMessage(**m) for m in data.get("messages", [])]
    return Plan(**data)


def save(plan: Plan) -> None:
    path = _path(plan.id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(json.dumps(asdict(plan), indent=2).encode())
    tmp.rename(path)


def load(plan_id: str) -> Plan | None:
    for path in (_path(plan_id), _claim_path(plan_id)):
        try:
            return _revive(json.loads(path.read_bytes()))
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def claim(plan_id: str) -> Plan | None:
    """Atomically take ownership of a DRAFT plan. None = lost the race,
    already applied/finished, or unknown id (caller disambiguates via
    load()). A finished plan's file is renamed back untouched."""
    try:
        _path(plan_id).rename(_claim_path(plan_id))
    except FileNotFoundError:
        return None
    try:
        plan = _revive(json.loads(_claim_path(plan_id).read_bytes()))
    except (json.JSONDecodeError, TypeError):
        return None
    if plan.status != "draft":
        _claim_path(plan_id).rename(_path(plan_id))  # hand it back
        return None
    return plan


def finish(plan: Plan, status: str, result: dict | None) -> None:
    """Write the final plan JSON and release the claim."""
    plan.status = status
    plan.result = result
    save(plan)
    _claim_path(plan.id).unlink(missing_ok=True)


def expire(plan: Plan) -> None:
    finish(plan, "expired", None)


def all_plans() -> list[Plan]:
    out = []
    for path in sorted(config.plans_dir().glob("*.json")):
        try:
            out.append(_revive(json.loads(path.read_bytes())))
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
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < horizon:
            path.unlink(missing_ok=True)
            removed += 1
        elif path.name.endswith(".applying") and mtime < stale:
            try:
                plan = _revive(json.loads(path.read_bytes()))
                plan.result = {"error": "stale claim: apply crashed mid-flight"}
                plan.status = "failed"
                save(plan)
            except (json.JSONDecodeError, TypeError):
                pass
            path.unlink(missing_ok=True)
    return removed
