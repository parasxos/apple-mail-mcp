"""CLI dispatch suite (email_mcp.cli) — v0.11-clean S4.

Dispatch and nothing else: bare invocation serves wire-silent, dash argv
forwards verbatim to the legacy server flags, every verb gates on the
ONE resolve (reason to stderr, exit 2) except doctor, which reports it;
verb flags map 1:1 onto the lifecycle functions; passthrough verbs
forward their argv to the module mains.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import email_mcp
from email_mcp import cli, state

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake $HOME with the default (unset) state root — no test may see
    the real ~/.email-mcp or the real LaunchAgents."""
    h = tmp_path / "home"
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    (h / "Library" / "Logs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    return h


@pytest.fixture
def report(monkeypatch) -> dict:
    """Canned doctor report — the doctor verb must not probe the real
    Mail.app / osascript from inside the suite."""
    rep = {"ok": True, "read_only": False,
           "checks": {"mail_store": {"ok": True, "detail": "42 messages"}},
           "audit": {"ok": True, "detail": "no events yet"}}
    monkeypatch.setattr("email_mcp.doctor.run", lambda: rep)
    return rep


def _clean_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EMAIL_MCP_")}
    env["HOME"] = str(home)
    return env


# ---------------------------------------------------------------------- #
# bare invocation = serve                                                  #
# ---------------------------------------------------------------------- #


def test_bare_invocation_serves_wire_silent(tmp_path):
    """THE S4 acceptance: `email-mcp` with empty stdin IS the MCP server —
    it exits clean and writes not one byte to stdout (the wire)."""
    proc = subprocess.run(
        [sys.executable, "-m", "email_mcp.cli"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        env=_clean_env(tmp_path),
    )
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_bare_serve_and_dash_argv_reach_server_main(home, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("email_mcp.server.main",
                        lambda argv=None: (calls.append(argv), 7)[1])
    assert cli.main([]) == 7
    assert cli.main(["serve"]) == 7
    assert cli.main(["--selftest", "--refresh-wait", "2"]) == 7
    assert calls == [[], [], ["--selftest", "--refresh-wait", "2"]]


# ---------------------------------------------------------------------- #
# the one gate: Refused prints to stderr and exits 2                       #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("argv", [
    [], ["serve"], ["setup"], ["update"], ["uninstall", "--yes"],
    ["version"], ["audit", "--status"], ["fts", "--status"], ["graph"],
    ["dispatcher", "--status"],
])
def test_refused_root_gates_every_verb_but_doctor(home, monkeypatch,
                                                  capsys, argv):
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(home / "old"))
    reason = state.State.resolve().reason
    assert cli.main(argv) == 2
    cap = capsys.readouterr()
    assert cap.out == ""  # stdout may be the MCP wire — never touched
    assert reason in cap.err
    assert not (home / "old").exists()  # rejected, nothing created


# ---------------------------------------------------------------------- #
# doctor — reports, never gates                                            #
# ---------------------------------------------------------------------- #


def test_doctor_reports_the_refused_root(home, report, monkeypatch, capsys):
    monkeypatch.setenv("EMAIL_MCP_PLANS_DIR", str(home / "old"))
    reason = state.State.resolve().reason
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL state_root: {reason}" in out
    assert "doctor --fix" not in out  # no repair exists for a refused root


def test_doctor_healthy_exits_zero(home, report, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "mail_store: 42 messages" in out
    assert "audit: no events yet" in out
    assert "FAIL" not in out


def test_doctor_renders_findings_and_the_fix_hint(home, report, capsys):
    state.State.resolve().adopt()  # adopted but unstamped -> meta_version
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL meta_version" in out
    assert "run `email-mcp doctor --fix` to repair." in out


def test_doctor_red_environment_check_renders_its_fix(home, report, capsys):
    report["ok"] = False
    report["checks"]["mail_store"] = {
        "ok": False, "detail": "no Envelope Index", "fix": "grant FDA"}
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL mail_store: no Envelope Index" in out
    assert "fix: grant FDA" in out


def test_doctor_fix_dispatches_to_lifecycle(home, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr("email_mcp.lifecycle.doctor_fix",
                        lambda dry_run=False: (calls.append(dry_run), 5)[1])
    assert cli.main(["doctor", "--fix"]) == 5
    assert cli.main(["doctor", "--fix", "--dry-run"]) == 5
    assert calls == [False, True]


# ---------------------------------------------------------------------- #
# verb dispatch — flags map 1:1, exit codes propagate                      #
# ---------------------------------------------------------------------- #


def test_lifecycle_verbs_dispatch_with_their_flags(home, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "email_mcp.lifecycle.uninstall",
        lambda purge=False, yes=False: (calls.append((purge, yes)), 1)[1])
    monkeypatch.setattr("email_mcp.lifecycle.setup",
                        lambda: (calls.append(("setup",)), 3)[1])
    monkeypatch.setattr("email_mcp.lifecycle.update",
                        lambda: (calls.append(("update",)), 4)[1])
    assert cli.main(["uninstall"]) == 1
    assert cli.main(["uninstall", "--purge", "--yes"]) == 1
    assert cli.main(["setup"]) == 3
    assert cli.main(["update"]) == 4
    assert calls == [(False, False), (True, True), ("setup",), ("update",)]


@pytest.mark.parametrize("verb", ["audit", "fts", "graph", "dispatcher"])
def test_passthrough_forwards_rest_argv(home, monkeypatch, verb):
    calls: list[list[str]] = []
    monkeypatch.setattr(f"email_mcp.{verb}.main",
                        lambda argv=None: (calls.append(argv), 6)[1])
    assert cli.main([verb, "--status", "-x"]) == 6
    assert calls == [["--status", "-x"]]


def test_unknown_verb_prints_usage_and_exits_2(home, capsys):
    assert cli.main(["frobnicate"]) == 2
    cap = capsys.readouterr()
    assert "usage: email-mcp" in cap.err
    assert cap.out == ""


def test_verb_flag_sets_are_closed(home):
    """No speculative options: a flag the concept does not name is an
    argparse reject, not a silent no-op."""
    with pytest.raises(SystemExit) as e:
        cli.main(["update", "--yes"])
    assert e.value.code == 2


def test_version_prints_the_single_source(home, capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == email_mcp.__version__


def test_version_subprocess_round_trip(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "email_mcp.cli", "version"],
        capture_output=True, text=True, timeout=60, env=_clean_env(tmp_path),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == email_mcp.__version__


# ---------------------------------------------------------------------- #
# packaging — the version has one source, the entry point is the CLI       #
# ---------------------------------------------------------------------- #


def test_packaging_single_sources_the_version():
    py = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert py["project"]["dynamic"] == ["version"]
    assert py["tool"]["setuptools"]["dynamic"]["version"] == \
        {"attr": "email_mcp.__version__"}
    assert py["project"]["scripts"]["email-mcp"] == "email_mcp.cli:main"
    assert py["project"]["requires-python"] == ">=3.11"
    assert py["project"]["dependencies"] == ["mcp>=1.2,<2"]
