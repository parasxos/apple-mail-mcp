"""Plan-store housekeeping (email_mcp.plans.gc) and its scoped caller.

The promise proven here is the one repairs.py's docstring makes: doctor
--fix never deletes user data. plans.gc()'s unscoped sweep unlinks EVERY
``*.json*`` older than 7 days in the plans dir, which is only ours to do
when we own the directory — EMAIL_MCP_PLANS_DIR can point anywhere. The
stale-claim repair therefore passes the ids IT detected, and touches
nothing else in that directory.

Fake $HOME + the conftest fts/audit guards shadowed by name, the
test_repairs pattern: the env-driven resolvers must be the real ones,
pointed inside the fake HOME.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from email_mcp import audit, config, ids, plans, repairs

TTL_SECONDS = 600  # config.triage_ttl_seconds() default; 2x = stale


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    yield h
    audit.set_process("server")  # run_fixes tags "cli"; restore

@pytest.fixture(autouse=True)
def state_root_guard(home, monkeypatch):
    """Shadow conftest's root guard by name: conftest PATCHES
    config.state_root, which would override this module's fake HOME. These
    tests need the real env-driven resolution inside that HOME."""
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    return home / ".email-mcp"

@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name): the fake HOME is the
    isolation, the env pin would aim outside it."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason): plan_finish must land
    through the real env-driven resolver, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


def _make_tree(home: Path) -> Path:
    root = home / ".email-mcp"
    for d in (root, root / "spool", root / "plans", root / "audit",
              root / "graph"):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    for state in ("pending", "sending", "sent", "failed", "cancelled"):
        (root / "spool" / state).mkdir(parents=True, exist_ok=True)
    return root


def _claim(path: Path, plan_id: str, age_seconds: float) -> Path:
    """A crashed apply's .applying claim, aged past 2x TTL."""
    path.write_text(json.dumps({
        "id": plan_id, "created_at": ids.iso(ids.utcnow()),
        "expires_at": ids.iso(ids.utcnow()), "status": "draft",
        "query": {}, "actions": [], "target": None, "messages": [],
        "summary": "2 messages: mark_read", "result": None,
    }))
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def _age(path: Path, days: float, body: str = "{}") -> Path:
    if not path.exists():
        path.write_text(body)
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    return path


# --------------------------------------------------------------------- #
# gc scope                                                               #
# --------------------------------------------------------------------- #


def test_unscoped_gc_still_sweeps_the_whole_dir(home):
    """The lazy build_plan/apply_plan caller is unchanged: no plan_ids
    means the 7-day sweep covers every file, as before."""
    _make_tree(home)
    plans_dir = config.plans_dir()
    ancient = _age(plans_dir / "old.json", days=9)
    fresh = plans_dir / "new.json"
    fresh.write_text("{}")

    assert plans.gc() == 1
    assert not ancient.exists()
    assert fresh.exists()


def test_scoped_gc_touches_only_the_named_plans(home):
    """plans.gc(plan_ids=...) finalises the named claim and leaves every
    other file in the directory alone — including files old enough for
    the unscoped sweep to have eaten."""
    _make_tree(home)
    plans_dir = config.plans_dir()
    claim = _claim(plans_dir / "p1.json.applying", "p1",
                   age_seconds=2 * TTL_SECONDS + 300)
    ancient_other = _age(plans_dir / "p9.json", days=9)
    bystander = _age(plans_dir / "notes.json.backup", days=400)

    plans.gc(plan_ids=["p1"])

    assert not claim.exists()  # the named claim was finalised
    assert json.loads(
        (plans_dir / "p1.json").read_text())["status"] == "failed"
    # Out of scope: untouched, however old.
    assert ancient_other.exists()
    assert bystander.exists()


def test_scoped_gc_ignores_a_non_plan_prefix_match(home):
    """Scope matches the plan id, not a prefix: "p1x.json" is not p1."""
    _make_tree(home)
    plans_dir = config.plans_dir()
    lookalike = _age(plans_dir / "p1x.json", days=9)

    assert plans.gc(plan_ids=["p1"]) == 0
    assert lookalike.exists()


# --------------------------------------------------------------------- #
# the repair that uses it                                                #
# --------------------------------------------------------------------- #


def test_stale_claim_repair_spares_unrelated_files_in_the_plans_dir(
    home, tmp_path, monkeypatch,
):
    """The finding: a plans directory the user also keeps files in.
    run_fixes must finalise the stale claim it detected and delete NOTHING
    else — repairs never delete user data.

    v0.11 reaches that shape through the ROOT (EMAIL_MCP_PLANS_DIR is
    retired): the user's file sits in <root>/plans next to the claim.
    plans.gc()'s unscoped sweep would unlink every *.json* older than
    seven days there, which is why the repair passes the ids it detected.
    """
    _make_tree(home)
    relocated = tmp_path / "relocated-state"
    relocated.mkdir()
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(relocated))
    shared = config.plans_dir()          # <root>/plans, created 0700

    claim = _claim(shared / "p1.json.applying", "p1",
                   age_seconds=2 * TTL_SECONDS + 300)
    users_file = shared / "notes.json.backup"
    users_file.write_text("irreplaceable")
    _age(users_file, days=400)

    result = repairs.run_fixes()

    assert "stale_plan_claims" in [a["repair"] for a in result["applied"]]
    assert not claim.exists()
    assert json.loads((shared / "p1.json").read_text())["status"] == "failed"
    # The user's own ancient file survived the repair.
    assert users_file.read_text() == "irreplaceable"


def test_stale_claim_repair_reports_the_claims_it_finalised(home):
    _make_tree(home)
    _claim(config.plans_dir() / "p1.json.applying", "p1",
           age_seconds=2 * TTL_SECONDS + 300)

    result = repairs.run_fixes()

    entry = next(a for a in result["applied"]
                 if a["repair"] == "stale_plan_claims")
    assert "1 stale claim(s)" in entry["action"]
