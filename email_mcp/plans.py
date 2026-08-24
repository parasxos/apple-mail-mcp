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
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import audit, config, ids, state
from .domain.models import Plan, PlanAction, PlanMessage

STATUSES = ("draft", "applied", "failed", "expired")


# Single source in ids.py (shared with spool.py and the audit ledger);
# the names stay public here so call sites and monkeypatches don't churn.
utcnow = ids.utcnow
iso = ids.iso
new_id = ids.new_id


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
    # The one plan write seam: the store comes to exist via state adoption.
    path = state.State.resolve().adopt().plans / f"{plan.id}.json"
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


def _finish_detail(result: dict | None) -> dict | None:
    """Compact, GC-surviving extract of a finish result: counts, per-message
    failure codes and pending ids — never the full dicts, never bodies."""
    if not result:
        return None
    detail: dict = {k: result[k] for k in ("planned", "acted", "verified")
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
    plan.result = result
    save(plan)
    _claim_path(plan.id).unlink(missing_ok=True)
    try:
        detail = _finish_detail(result)
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
            except (json.JSONDecodeError, TypeError):
                plan = None
            if plan is not None:
                # Through finish(): the stale claim gets the same terminal
                # write + plan_finish event as every other ending.
                finish(plan, "failed",
                       {"error": "stale claim: apply crashed mid-flight"})
            path.unlink(missing_ok=True)  # id/filename mismatch safety net
    return removed
