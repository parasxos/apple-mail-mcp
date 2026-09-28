"""Crash and I/O boundaries around durable triage selection publication."""
from __future__ import annotations

import errno
import json
import os
import select
import stat
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from email_mcp import config, plans, triage
from email_mcp.plans import Plan, PlanAction, PlanMessage


@pytest.fixture
def saved_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_TTL", "600")
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "0")
    now = plans.utcnow()
    plan = Plan(
        id=plans.new_id(now), created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(seconds=600)), status="draft",
        query={}, actions=[PlanAction("mark_read")], target=None,
        messages=[PlanMessage(
            rowid=rid, account="local", scheme="local", mailbox="Inbox",
            mailbox_rowid=1, subject="frozen", from_addr="test@example.test",
            date=plans.iso(now), unread=True, message_id_header=f"{rid}@example.test",
            global_message_id=rid, pre={"read": 0},
        ) for rid in (101, 102)], summary="two reviewed messages",
    )
    plans.save(plan)
    return plan


def _no_mail(*args, **kwargs):
    pytest.fail("Mail must not run before selection publication")


@pytest.mark.parametrize("failure", ["write", "file_fsync", "replace", "directory_fsync"])
def test_failed_selection_publication_preserves_frozen_plan(saved_plan, monkeypatch, failure):
    original = asdict(saved_plan)
    monkeypatch.setattr(triage, "_run_osascript", _no_mail)
    with monkeypatch.context() as broken:
        if failure == "write":
            def partial_write(fd, data):
                os.write(fd, data[:13])
                raise OSError(errno.ENOSPC, "fixture write failure")
            broken.setattr(plans, "_write_all", partial_write)
        elif failure == "file_fsync":
            real_sync = os.fsync

            def fail_sync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EIO, "fixture file fsync failure")
                real_sync(fd)
            broken.setattr(plans.os, "fsync", fail_sync)
        elif failure == "replace":
            def fail_replace(*args):
                raise OSError(errno.EIO, "fixture replace failure")
            broken.setattr(plans.os, "replace", fail_replace)
        else:
            real_sync_dir = plans._sync_dir
            calls = 0

            def fail_directory(directory):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "fixture directory fsync failure")
                real_sync_dir(directory)
            broken.setattr(plans, "_sync_dir", fail_directory)
        with pytest.raises(OSError, match="fixture"):
            triage.apply_plan(object(), saved_plan.id, ["102"])
    stored = plans.load(saved_plan.id)
    assert asdict(stored)["messages"] == original["messages"]
    assert stored.status == "draft"
    assert stored.excluded_ids == (["102"] if failure == "directory_fsync" else None)
    assert not list(config.plans_dir().glob(".*.tmp-*"))
    plans.gc(now=plans.utcnow() + timedelta(seconds=1201))
    terminal = plans.load(saved_plan.id)
    assert terminal.status == "failed" and terminal.messages == saved_plan.messages
    if failure == "directory_fsync":
        assert terminal.result["excluded"] == ["102"]
    else:
        assert "excluded" not in terminal.result


def test_selection_syncs_file_and_directory_before_any_mail(saved_plan, monkeypatch):
    synced = []
    real_sync = os.fsync

    def sync(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_sync(fd)

    def run(script, timeout):
        assert synced[-2:] == ["file", "directory"]
        stored = plans.load(saved_plan.id)
        assert stored.excluded_ids == ["102"] and len(stored.messages) == 2
        return subprocess.CompletedProcess([], 0, "OK 101\n", "")

    monkeypatch.setattr(plans.os, "fsync", sync)
    monkeypatch.setattr(triage, "_run_osascript", run)
    source = SimpleNamespace(triage_snapshot=lambda ids: {101: {"read": 1}})
    assert triage.apply_plan(source, saved_plan.id, ["102"])["verified"] == 1


_CHILD = """
import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from email_mcp import plans, triage

phase = sys.argv[2]
def pause():
    print('READY', flush=True)
    sys.stdin.readline()

real_publish = plans._atomic_write
real_write = plans._write_all
def partial(fd, data):
    os.write(fd, data[:13])
    pause()
    raise RuntimeError('fixture should be killed')
def publish(path, data):
    if path.name.endswith('.applying'):
        if phase == 'before':
            pause()
        if phase == 'during':
            plans._write_all = partial
        real_publish(path, data)
        if phase == 'after':
            pause()
    else:
        real_publish(path, data)
plans._atomic_write = publish

def mail(script, timeout):
    ids = re.findall(r'«class mssg» id (\\d+)', script)
    return subprocess.CompletedProcess([], 0, ''.join('OK '+i+'\\n' for i in ids), '')
triage._run_osascript = mail
source = SimpleNamespace(triage_snapshot=lambda ids: {i: {'read': 1} for i in ids})
result = triage.apply_plan(source, sys.argv[1], ['102', '102'])
print(json.dumps(result), flush=True)
"""


@pytest.mark.parametrize("phase,crash", [
    ("before", True), ("during", True), ("after", True),
    ("before", False), ("after", False),
])
def test_process_ownership_covers_selection_commit_and_gc(saved_plan, monkeypatch, phase, crash):
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, saved_plan.id, phase],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0], "child did not reach checkpoint"
        assert child.stdout.readline().strip() == "READY"
        assert plans.gc(now=plans.utcnow() + timedelta(days=8)) == 0
        stored = plans.load(saved_plan.id)
        assert stored.status == "draft" and stored.messages == saved_plan.messages
        assert stored.excluded_ids == (["102"] if phase == "after" else None)
        monkeypatch.setattr(triage, "_run_osascript", _no_mail)
        with pytest.raises(triage.TriageError) as error:
            triage.apply_plan(object(), saved_plan.id, ["101"])
        assert error.value.code == "plan_claimed"
        if crash:
            child.kill()
            child.communicate(timeout=10)
            plans.gc(now=plans.utcnow() + timedelta(seconds=1201))
            terminal = plans.load(saved_plan.id)
            assert terminal.status == "failed"
            if phase == "after":
                assert terminal.result["excluded"] == ["102"]
                events = [json.loads(line) for p in config.audit_dir().glob("*.jsonl")
                          for line in p.read_text().splitlines()]
                assert events[-1]["detail"]["excluded"] == ["102"]
            else:
                assert "excluded" not in terminal.result
            assert not list(config.plans_dir().glob(".*.tmp-*"))
        else:
            stdout, stderr = child.communicate("continue\n", timeout=10)
            assert child.returncode == 0, stderr
            result = json.loads(stdout)
            assert result["planned"] == 2 and result["selected"] == result["verified"] == 1
            assert result["excluded"] == ["102"]
        assert plans.load(saved_plan.id).messages == saved_plan.messages
        lock = config.plans_dir() / "locks" / saved_plan.id
        assert lock.is_file()
        with plans._own(saved_plan.id) as acquired:
            assert acquired
        with pytest.raises(triage.TriageError) as error:
            triage.apply_plan(object(), saved_plan.id, ["101"])
        assert error.value.code == "plan_already_applied"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_gc_preserves_terminal_result_after_interrupted_claim_cleanup(saved_plan):
    claimed = plans.claim(saved_plan.id)
    claimed.status = "applied"
    claimed.excluded_ids = ["102"]
    claimed.result = {"planned": 2, "selected": 1, "excluded": ["102"], "verified": 1}
    plans.save(claimed)
    plans.gc(now=plans.utcnow() + timedelta(seconds=1201))
    assert plans.load(saved_plan.id).status == "applied"
    assert plans.load(saved_plan.id).result == claimed.result
    assert not (config.plans_dir() / f"{saved_plan.id}.json.applying").exists()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "public", "foreign"])
def test_unsafe_lock_files_are_refused_before_mail(saved_plan, monkeypatch, tmp_path, kind):
    lock = config.plans_dir() / "locks" / saved_plan.id
    if kind == "fifo":
        os.mkfifo(lock, 0o600)
    elif kind == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(b"preserve")
        lock.symlink_to(target)
    else:
        lock.write_bytes(b"")
        lock.chmod(0o644 if kind == "public" else 0o600)
    if kind == "foreign":
        inode = lock.stat().st_ino
        real_stat = os.fstat

        def foreign_stat(fd):
            info = real_stat(fd)
            if info.st_ino == inode:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return info
        monkeypatch.setattr(plans.os, "fstat", foreign_stat)
    monkeypatch.setattr(triage, "_run_osascript", _no_mail)
    with pytest.raises(OSError):
        triage.apply_plan(object(), saved_plan.id, ["102"])
    assert plans.load(saved_plan.id).status == "draft"
    assert plans.load(saved_plan.id).excluded_ids is None
    if kind == "symlink":
        assert target.read_bytes() == b"preserve"


def test_expired_and_unknown_plans_keep_existing_error_contract(saved_plan, monkeypatch):
    monkeypatch.setattr(triage, "_run_osascript", _no_mail)
    with pytest.raises(triage.TriageError) as error:
        triage.apply_plan(object(), "unknown-plan", ["102"])
    assert error.value.code == "plan_not_found" and error.value.operation_id is None
    future = plans.utcnow() + timedelta(seconds=601)
    monkeypatch.setattr(plans, "utcnow", lambda: future)
    with pytest.raises(triage.TriageError) as error:
        triage.apply_plan(object(), saved_plan.id, ["102"])
    assert error.value.code == "plan_expired" and error.value.operation_id == saved_plan.id
    assert plans.load(saved_plan.id).status == "expired"
    assert plans.load(saved_plan.id).excluded_ids is None
