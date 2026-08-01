"""Action algebra suite (email_mcp.plan) — v0.11-clean S3.

Constructor fences, the pure preview, the one effectful walk, evidence-
derived honesty, and the two additive audit event types.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from email_mcp import audit, plan
from email_mcp.plan import (
    BootoutAgent, BootstrapAgent, Chmod, Kept, PrintOnly, RemoveTree,
    RenameAside, UnlinkFile, WriteFile,
)


def _fake_home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    return h


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _snapshot(root: Path) -> set[tuple]:
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            out.add((str(p), stat.S_IFMT(st.st_mode),
                     stat.S_IMODE(st.st_mode),
                     st.st_size if stat.S_ISREG(st.st_mode) else 0))
    return out


@pytest.fixture
def launchctl(monkeypatch):
    """Fake the one launchctl seam; records calls, returns rc 0."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(plan, "_launchctl", fake)
    return calls


# ---------------------------------------------------------------------- #
# constructor fences                                                      #
# ---------------------------------------------------------------------- #


def test_remove_tree_exists_only_for_the_state_root(tmp_path):
    with pytest.raises(ValueError, match="only for ~/.email-mcp"):
        RemoveTree(tmp_path / "anywhere")


def test_state_root_absent_is_kept(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    row = RemoveTree.state_root()
    assert isinstance(row, Kept) and "already absent" in row.note


def test_state_root_symlink_never_enters_a_plan(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home / ".email-mcp").symlink_to(target)
    row = RemoveTree.state_root()
    assert isinstance(row, PrintOnly) and "symlink" in row.text
    # ... and executing the plan cannot touch the target:
    plan.execute([row], verb="uninstall")
    assert target.is_dir() and (home / ".email-mcp").is_symlink()


def test_state_root_non_dir_never_enters_a_plan(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    (home / ".email-mcp").write_text("squatter")
    row = RemoveTree.state_root()
    assert isinstance(row, PrintOnly) and "not a directory" in row.text


def test_state_root_real_dir_constructs(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    (home / ".email-mcp").mkdir()
    row = RemoveTree.state_root()
    assert isinstance(row, RemoveTree)
    assert row.root == home / ".email-mcp"


# ---------------------------------------------------------------------- #
# render is pure                                                          #
# ---------------------------------------------------------------------- #


def test_render_previews_every_row_and_touches_nothing(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".email-mcp"
    root.mkdir()
    (root / "junk").write_text("x")
    rows = [
        BootoutAgent("com.email-mcp.dispatcher"),
        UnlinkFile(root / "junk"),
        RemoveTree.state_root(),
        Kept(root, "note"),
        PrintOnly("refused path row"),
    ]
    before = _snapshot(home)
    text = plan.render(rows)
    assert _snapshot(home) == before
    for row in rows:
        assert row.describe() in text
    assert "remove tree" in text and "refused path row" in text


# ---------------------------------------------------------------------- #
# execute — evidence-derived honesty                                      #
# ---------------------------------------------------------------------- #


def test_unlink_removes_with_evidence(tmp_path):
    f = tmp_path / "x"
    f.write_text("x")
    (r,) = plan.execute([UnlinkFile(f)], verb="uninstall")
    assert (r.ok, r.before, r.after) == (True, True, False)
    assert not f.exists()


def test_unlink_missing_is_the_goal_state(tmp_path):
    (r,) = plan.execute([UnlinkFile(tmp_path / "gone")], verb="uninstall")
    assert r.ok and r.before is False and r.after is False


def test_remove_tree_removes_recursively(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".email-mcp"
    (root / "audit").mkdir(parents=True)
    (root / "audit" / "2026-07.jsonl").write_text("{}\n")
    (r,) = plan.execute([RemoveTree.state_root()], verb="uninstall")
    assert r.ok and r.before is True and r.after is False
    assert not root.exists()


def test_rename_aside_moves_the_squatter(tmp_path):
    squat = tmp_path / "audit"
    squat.write_text("squatter")
    (r,) = plan.execute([RenameAside(squat)], verb="doctor_fix")
    assert r.ok and not squat.exists()
    assert (tmp_path / "audit.bak").read_text() == "squatter"


def test_rename_aside_never_clobbers(tmp_path):
    squat = tmp_path / "audit"
    squat.write_text("new")
    (tmp_path / "audit.bak").write_text("old")
    (r,) = plan.execute([RenameAside(squat)], verb="doctor_fix")
    assert r.failed and "already exists" in r.error
    assert squat.read_text() == "new"
    assert (tmp_path / "audit.bak").read_text() == "old"


def test_chmod_sets_the_mode(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    (r,) = plan.execute([Chmod(d, 0o700)], verb="doctor_fix")
    assert r.ok and _mode(d) == 0o700


def test_chmod_never_chases_a_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s")
    os.chmod(secret, 0o644)
    link = tmp_path / "link"
    link.symlink_to(secret)
    plan.execute([Chmod(link, 0o600)], verb="doctor_fix")
    assert _mode(secret) == 0o644  # the target was never touched


def test_write_file_is_atomic_and_restrictive(tmp_path):
    f = tmp_path / "meta.json"
    (r,) = plan.execute([WriteFile(f, '{"state_version": 1}\n', mode=0o600)],
                        verb="setup")
    assert r.ok and r.after is True
    assert f.read_text() == '{"state_version": 1}\n'
    assert _mode(f) == 0o600
    assert not (tmp_path / "meta.json.tmp").exists()


def test_write_file_backup_first(tmp_path):
    f = tmp_path / "identities.toml"
    f.write_text("old content")
    (r,) = plan.execute([WriteFile(f, "new content", backup=True)],
                        verb="setup")
    assert r.ok
    assert f.read_text() == "new content"
    assert (tmp_path / "identities.toml.bak").read_text() == "old content"


def test_failed_action_reaches_report_and_exit_fold(tmp_path):
    """THE partial-failure acceptance: a failed ActionResult shows up in
    the rendered report AND flips the exit-code fold — both are derived
    from the same results list."""
    d = tmp_path / "ro"
    d.mkdir()
    locked = d / "file"
    locked.write_text("x")
    os.chmod(d, 0o500)
    try:
        results = plan.execute([UnlinkFile(locked)], verb="uninstall")
    finally:
        os.chmod(d, 0o700)
    (r,) = results
    assert r.failed and r.error and r.after is True
    report = plan.render_results(results)
    assert "FAILED" in report and str(locked) in report
    assert "still present" in report
    assert "1 left behind" in report
    assert any(x.failed for x in results)  # the fold's exact input


def test_kept_and_print_only_execute_as_noops(tmp_path):
    rows = [Kept(tmp_path, "kept"), PrintOnly("cannot touch this")]
    results = plan.execute(rows, verb="uninstall")
    assert all(r.ok for r in results)
    assert plan.render_results(results).endswith("2 done")


# ---------------------------------------------------------------------- #
# launchd agents through the one seam                                     #
# ---------------------------------------------------------------------- #


def test_bootout_ok_and_not_loaded_is_goal_state(monkeypatch):
    def fake(*args):
        return subprocess.CompletedProcess(
            args, 3, stdout="", stderr="Boot-out failed: 3: No such process")

    monkeypatch.setattr(plan, "_launchctl", fake)
    (r,) = plan.execute([BootoutAgent("com.email-mcp.dispatcher")],
                        verb="uninstall")
    assert r.ok


def test_bootout_real_failure_is_honest(monkeypatch):
    def fake(*args):
        return subprocess.CompletedProcess(args, 5, stdout="",
                                           stderr="Boot-out failed: 5: perm")

    monkeypatch.setattr(plan, "_launchctl", fake)
    (r,) = plan.execute([BootoutAgent("com.email-mcp.dispatcher")],
                        verb="uninstall")
    assert r.failed and "perm" in r.error


def test_bootstrap_boots_out_then_bootstraps(tmp_path, launchctl):
    plist = tmp_path / "com.email-mcp.dispatcher.plist"
    plist.write_text("<plist/>")
    (r,) = plan.execute(
        [BootstrapAgent("com.email-mcp.dispatcher", plist)], verb="update")
    assert r.ok
    assert [c[0] for c in launchctl] == ["bootout", "bootstrap"]
    assert str(plist) in launchctl[1]


def test_bootstrap_failure_is_honest(tmp_path, monkeypatch):
    def fake(*args):
        rc = 0 if args[0] == "bootout" else 1
        return subprocess.CompletedProcess(args, rc, stdout="",
                                           stderr="Bootstrap failed: 125")

    monkeypatch.setattr(plan, "_launchctl", fake)
    plist = tmp_path / "a.plist"
    plist.write_text("<plist/>")
    (r,) = plan.execute([BootstrapAgent("a", plist)], verb="update")
    assert r.failed and "Bootstrap failed" in r.error


# ---------------------------------------------------------------------- #
# the two additive audit event types (contract §8)                        #
# ---------------------------------------------------------------------- #


def test_lifecycle_event_lands_before_destruction(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".email-mcp"
    root.mkdir()
    (root / "junk").write_text("x")
    seen = {}

    def spy(event, **kw):
        seen["event"] = event
        seen["outcome"] = kw.get("outcome")
        seen["root_existed"] = root.exists()
        return None

    monkeypatch.setattr(audit, "emit", spy)
    rows = [RemoveTree.state_root()]
    plan.execute(rows, verb="uninstall")
    assert not root.exists()
    assert seen == {"event": "lifecycle", "outcome": "uninstall",
                    "root_existed": True}


def test_lifecycle_event_carries_the_rendered_plan(state_dir_guard, tmp_path):
    f = tmp_path / "x"
    f.write_text("x")
    rows = [UnlinkFile(f), Kept(tmp_path, "rest")]
    plan.execute(rows, verb="uninstall")
    (ev,) = audit.query(event="lifecycle")["events"]
    assert ev["outcome"] == "uninstall"
    assert ev["detail"]["plan"] == [r.describe() for r in rows]


def test_actionless_plan_records_no_lifecycle_event(monkeypatch, tmp_path):
    emits = []
    monkeypatch.setattr(audit, "emit", lambda *a, **k: emits.append(a))
    plan.execute([Kept(tmp_path, "n"), PrintOnly("t")], verb="uninstall")
    assert emits == []  # no mutation, no event (contract §6)


def test_doctor_fix_emits_one_event_per_repair(state_dir_guard, tmp_path):
    ok_file = tmp_path / "a"
    ok_file.write_text("x")
    d = tmp_path / "ro"
    d.mkdir()
    locked = d / "b"
    locked.write_text("x")
    os.chmod(d, 0o500)
    try:
        plan.execute([UnlinkFile(ok_file), UnlinkFile(locked)],
                     verb="doctor_fix")
    finally:
        os.chmod(d, 0o700)
    evs = audit.query(event="doctor_fix")["events"]  # newest first
    assert [(e["outcome"]) for e in evs] == ["failed", "fixed"]
    assert str(locked) in evs[0]["detail"]["action"]
    assert evs[0]["detail"]["error"]
    assert audit.query(event="lifecycle")["events"] == []
