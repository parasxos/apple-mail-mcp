"""Checks registry suite (email_mcp.checks) — v0.11-clean S3.

One registry behind doctor, --fix, setup and update: read-only probes,
findings mapped to plan.py actions, the StateWriter gate for repairs,
and the meta.json state_version stamp.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from email_mcp import checks, plan, state
from email_mcp.checks import META, STATE_VERSION, Finding


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake $HOME (LaunchAgents included) so the launchd probes never
    see the real machine; the state root stays under the conftest pin."""
    h = tmp_path / "home"
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def writer(home, state_dir_guard) -> state.StateWriter:
    """An adopted state tree with every leaf materialized."""
    w = state.State.resolve().adopt()
    _ = (w.spool, w.plans, w.graph, w.fts, w.audit)
    return w


@pytest.fixture
def launchctl(monkeypatch):
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(plan, "_launchctl", fake)
    return calls


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


def _finding(check_id: str) -> Finding | None:
    hits = [f for f in checks.findings() if f.check == check_id]
    return hits[0] if hits else None


def _fix(only: str | None = None) -> list:
    rows = checks.plan_fix(
        only=frozenset({only}) if only else None)
    return plan.execute(rows, verb="doctor_fix")


# ---------------------------------------------------------------------- #
# the runner gates                                                        #
# ---------------------------------------------------------------------- #


def test_refused_root_is_one_finding_with_the_reason(home, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "old"))
    r = state.State.resolve()
    (f,) = checks.findings()
    assert f.check == checks.STATE_ROOT and f.detail == r.reason


def test_no_writer_can_exist_for_a_refused_root(home, monkeypatch, tmp_path):
    """The whole gate: repairs demand a StateWriter, which a refused root
    cannot produce — plan_fix answers with a report, and mutates nothing."""
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "old"))
    (row,) = checks.plan_fix()
    assert isinstance(row, plan.PrintOnly) and "cannot fix" in row.text
    assert not (tmp_path / "old").exists()


def test_fresh_home_is_clean_and_stays_untouched(home, monkeypatch):
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    assert checks.findings() == []
    assert checks.plan_fix() == []
    assert not (home / ".email-mcp").exists()  # nothing probed it into being


def test_probes_are_read_only_over_a_populated_tree(home, writer):
    before = _snapshot(writer.root)
    checks.findings()
    checks.findings()
    assert _snapshot(writer.root) == before


def test_healthy_tree_has_no_findings_beyond_meta(home, writer):
    assert [f.check for f in checks.findings()] == [checks.META_VERSION]


# ---------------------------------------------------------------------- #
# managed-tree modes                                                      #
# ---------------------------------------------------------------------- #


def test_tree_modes_found_and_repaired(home, writer):
    os.chmod(writer.plans, 0o755)
    os.chmod(writer.spool / "pending", 0o775)
    f = _finding(checks.TREE_MODES)
    assert set(f.paths) == {writer.plans, writer.spool / "pending"}
    results = _fix(checks.TREE_MODES)
    assert all(r.ok for r in results)
    assert stat.S_IMODE(os.lstat(writer.plans).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(writer.spool / "pending").st_mode) == 0o700
    assert _finding(checks.TREE_MODES) is None


def test_secret_modes_found_and_repaired(home, writer):
    token = writer.graph / "main.token.json"
    token.write_text("{}")
    os.chmod(token, 0o644)
    month = writer.audit / "2026-07.jsonl"
    month.write_text("{}\n")
    os.chmod(month, 0o664)
    os.chmod(writer.root / state.MARKER, 0o644)
    f = _finding(checks.SECRET_MODES)
    assert set(f.paths) == {token, month, writer.root / state.MARKER}
    results = _fix(checks.SECRET_MODES)
    assert all(r.ok for r in results)
    for p in (token, month, writer.root / state.MARKER):
        assert stat.S_IMODE(os.lstat(p).st_mode) == 0o600
    assert _finding(checks.SECRET_MODES) is None


def test_symlinked_leaf_is_not_a_mode_finding(home, writer, tmp_path):
    """Symlinks are the state policy's business, never chmod'ed at."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    os.chmod(elsewhere, 0o755)
    os.rmdir(writer.root / "fts")
    (writer.root / "fts").symlink_to(elsewhere)
    assert _finding(checks.TREE_MODES) is None


# ---------------------------------------------------------------------- #
# audit-path squat → rename-aside                                         #
# ---------------------------------------------------------------------- #


def test_audit_squat_found_and_renamed_aside(home, state_dir_guard):
    writer = state.State.resolve().adopt()
    squat = writer.root / "audit"
    squat.write_text("squatter")
    f = _finding(checks.AUDIT_SQUAT)
    assert f.paths == (squat,) and "dropped" in f.detail
    results = _fix(checks.AUDIT_SQUAT)
    assert all(r.ok for r in results)
    assert (writer.root / "audit.bak").read_text() == "squatter"
    # The fix's own doctor_fix event already re-adopted the leaf: the
    # squatting file is gone and a real 0700 directory took its place.
    assert not squat.is_file()
    assert writer.audit.is_dir() and not writer.audit.is_symlink()


def test_symlinked_audit_leaf_is_a_squat_finding(home, writer, tmp_path):
    target = tmp_path / "elsewhere-audit"
    target.mkdir()
    os.rmdir(writer.root / "audit")
    (writer.root / "audit").symlink_to(target)
    f = _finding(checks.AUDIT_SQUAT)
    assert f is not None and f.paths == (writer.root / "audit",)


# ---------------------------------------------------------------------- #
# meta.json — the state_version stamp                                     #
# ---------------------------------------------------------------------- #


def test_unstamped_root_is_found_and_stamped(home, writer):
    f = _finding(checks.META_VERSION)
    assert f is not None and "unstamped" in f.detail
    results = _fix(checks.META_VERSION)
    assert all(r.ok for r in results)
    meta = writer.root / META
    assert json.loads(meta.read_text()) == {"state_version": STATE_VERSION}
    assert stat.S_IMODE(os.lstat(meta).st_mode) == 0o600
    assert _finding(checks.META_VERSION) is None
    assert checks.read_state_version(
        state.State.resolve().reader()) == STATE_VERSION


def test_stale_state_version_is_a_migration_finding(home, writer):
    (writer.root / META).write_text('{"state_version": 0}')
    f = _finding(checks.META_VERSION)
    assert "migration pending" in f.detail
    _fix(checks.META_VERSION)
    assert checks.read_state_version(
        state.State.resolve().reader()) == STATE_VERSION


@pytest.mark.parametrize("content", ["not json", '"scalar"',
                                     '{"state_version": "1"}', "{}"])
def test_read_state_version_is_total(home, writer, content):
    (writer.root / META).write_text(content)
    assert checks.read_state_version(
        state.State.resolve().reader()) is None
    assert _finding(checks.META_VERSION) is not None


def test_absent_root_has_nothing_to_version(home, monkeypatch):
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    assert _finding(checks.META_VERSION) is None


# ---------------------------------------------------------------------- #
# launchd: legacy bootout + stale plist re-render                          #
# ---------------------------------------------------------------------- #


def test_legacy_plist_found_booted_out_and_removed(home, writer, launchctl):
    from email_mcp import dispatcher

    label = dispatcher.LEGACY_LABELS[0]
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.write_text("<plist/>")
    f = _finding(checks.LEGACY_LAUNCHD)
    assert f.paths == (plist,)
    results = _fix(checks.LEGACY_LAUNCHD)
    assert all(r.ok for r in results)
    assert not plist.exists()
    assert ("bootout", f"gui/{os.getuid()}/{label}") in launchctl


def test_stale_plist_found_rerendered_and_bootstrapped(home, writer,
                                                       launchctl):
    from email_mcp import dispatcher

    plist = dispatcher._plist_path()
    assert plist.is_relative_to(home)  # the probe sees the FAKE home only
    plist.write_text("<plist>stale</plist>")
    f = _finding(checks.PLIST_DRIFT)
    assert f.paths == (plist,)
    results = _fix(checks.PLIST_DRIFT)
    assert all(r.ok for r in results)
    assert plist.read_text() == dispatcher._plist_content()
    assert launchctl[-1][0] == "bootstrap" and str(plist) in launchctl[-1]


def test_current_plist_is_not_drift(home, writer):
    from email_mcp import dispatcher

    plist = dispatcher._plist_path()
    plist.write_text(dispatcher._plist_content())
    assert _finding(checks.PLIST_DRIFT) is None


def test_another_seats_plist_is_not_drift(home, writer):
    """Doctor runs from several seats (operator shell, MCP server, the
    RC's wheel venv) whose renders spell the same install differently.
    A plist whose interpreter is a live sibling spelling and whose PATH
    was baked by another shell is NOT drift — byte-comparing made P14's
    precondition unsatisfiable (live, 2026-08-03)."""
    import plistlib
    import sys
    from email_mcp import dispatcher

    doc = plistlib.loads(dispatcher._plist_content().encode())
    sibling = Path(sys.executable).parent / "python3"
    assert sibling.exists()  # every venv carries this spelling
    doc["ProgramArguments"][0] = str(sibling)
    doc["EnvironmentVariables"]["PATH"] = "/some/other/shells:/path"
    dispatcher._plist_path().write_bytes(plistlib.dumps(doc))
    assert _finding(checks.PLIST_DRIFT) is None


def test_dead_interpreter_and_drifted_schedule_are_drift(home, writer):
    """The teeth the seat-tolerance must not lose: an interpreter path
    that no longer exists (the moved-venv case the check was born for),
    and any change outside the exempt fields (args, schedule), are
    still stale."""
    import plistlib
    from email_mcp import dispatcher

    plist = dispatcher._plist_path()
    doc = plistlib.loads(dispatcher._plist_content().encode())
    doc["ProgramArguments"][0] = str(home / "gone" / "bin" / "python")
    plist.write_bytes(plistlib.dumps(doc))
    assert _finding(checks.PLIST_DRIFT) is not None

    doc = plistlib.loads(dispatcher._plist_content().encode())
    doc["ProgramArguments"][-1] = "--someone-elses-flag"
    plist.write_bytes(plistlib.dumps(doc))
    assert _finding(checks.PLIST_DRIFT) is not None


def test_uninstalled_agents_are_not_drift(home, writer):
    assert _finding(checks.PLIST_DRIFT) is None


# ---------------------------------------------------------------------- #
# plan_fix — one adoption, one plan, the update slice                      #
# ---------------------------------------------------------------------- #


def test_plan_fix_only_restricts_to_the_named_checks(home, writer):
    os.chmod(writer.plans, 0o755)  # a tree_modes finding AND meta pending
    rows = checks.plan_fix(only=frozenset({checks.META_VERSION}))
    assert [type(r) for r in rows] == [plan.AdoptRoot, plan.WriteFile]
    assert rows[1].path == writer.root / META
    os.chmod(writer.plans, 0o700)


def test_plan_fix_repairs_everything_in_one_plan(home, writer, launchctl):
    from email_mcp import dispatcher

    os.chmod(writer.plans, 0o755)
    squat_bak = writer.root / "audit.bak"
    assert not squat_bak.exists()
    legacy = (home / "Library" / "LaunchAgents" /
              f"{dispatcher.LEGACY_LABELS[0]}.plist")
    legacy.write_text("<plist/>")
    before = _snapshot(home) | _snapshot(writer.root)
    rows = checks.plan_fix()
    assert _snapshot(home) | _snapshot(writer.root) == before  # build is pure
    assert isinstance(rows[0], plan.AdoptRoot)  # adoption is a row, not an effect
    results = plan.execute(rows, verb="doctor_fix")
    assert all(r.ok for r in results)
    assert checks.findings() == []
