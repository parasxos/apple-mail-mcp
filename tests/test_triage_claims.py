"""Apply ownership survives concurrent housekeeping and releases on process exit."""
from __future__ import annotations

import os
import select
import subprocess
import sys
from datetime import timedelta

import pytest

from email_mcp import config, plans
from email_mcp.plans import Plan, PlanAction, PlanMessage


_APPLY = """
import subprocess
import sys
from types import SimpleNamespace
from email_mcp import triage

def run(script, timeout):
    if 'accountIds' not in script:
        print('READY', flush=True)
        sys.stdin.readline()
    return subprocess.CompletedProcess([], 0, 'OK 101\\n', '')

triage._run_osascript = run
source = SimpleNamespace(triage_snapshot=lambda ids: {
    101: {'read': 1, 'flagged': 0, 'flag_color': -1, 'mailbox_rowid': 1, 'deleted': 0}
})
result = triage.apply_plan(source, sys.argv[1])
print(result['status'], result['verified'], flush=True)
"""


@pytest.mark.parametrize("crash", [False, True])
def test_apply_owner_excludes_gc_until_completion_or_crash(tmp_path, monkeypatch, crash):
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_TTL", "600")
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "0")
    now = plans.utcnow()
    plan = Plan(
        id=plans.new_id(now), created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(seconds=600)), status="draft",
        query={}, actions=[PlanAction("mark_read")], target=None,
        messages=[PlanMessage(
            rowid=101, account="local", scheme="local", mailbox="Inbox",
            mailbox_rowid=1, subject="fixture", from_addr="test@example.test",
            date=plans.iso(now), unread=True, message_id_header="fixture@example.test",
            global_message_id=101, pre={"read": 0},
        )], summary="fixture",
    )
    plans.save(plan)
    claim = config.plans_dir() / f"{plan.id}.json.applying"
    child = subprocess.Popen(
        [sys.executable, "-c", _APPLY, plan.id], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy(),
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0], "apply did not start"
        assert child.stdout.readline().strip() == "READY"
        assert claim.exists()
        assert plans.gc(now=now + timedelta(days=8)) == 0
        assert claim.exists() and plans.load(plan.id).status == "draft"
        with plans.claim_owned(plan.id) as other:
            assert other is None
        if crash:
            child.kill()
            child.communicate(timeout=10)
            plans.gc(now=now + timedelta(seconds=1201))
            assert plans.load(plan.id).status == "failed"
            assert plans.load(plan.id).result == {
                "error": "stale claim: apply crashed mid-flight",
                "planned": 1, "selected": 1, "excluded": [],
            }
        else:
            stdout, stderr = child.communicate("finish\n", timeout=10)
            assert child.returncode == 0, stderr
            assert stdout.strip() == "applied 1"
            assert plans.load(plan.id).status == "applied"
        assert not claim.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
