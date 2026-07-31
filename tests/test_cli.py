"""The v0.11 CLI front door (email_mcp.cli) — dispatch, wire safety,
delegation, doctor exit codes.

The one hard compatibility promise here is that BARE invocation serves:
Paris's ~/.claude.json points MCP registrations at .venv/bin/email-mcp
with no arguments, and that must keep starting the stdio server — with
nothing on stdout before mcp.run() (contract §7). Everything else is the
subcommand surface: leading-dash argv forwards to server.main()'s legacy
flags verbatim, audit/fts/graph get their argv untouched, dispatcher and
serve go through the sys.argv shim, unknown names are an argparse exit-2
listing the registry.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from email_mcp import cli, server


def _mode(p) -> int:
    import stat

    return stat.S_IMODE(p.stat().st_mode)

COMMAND_NAMES = [
    "serve", "setup", "doctor", "update", "uninstall",
    "audit", "fts", "graph", "dispatcher", "version",
]


# --------------------------------------------------------------------- #
# dispatch: serve + legacy forwarding                                    #
# --------------------------------------------------------------------- #


def test_commands_registry_is_exactly_the_frozen_list():
    assert list(cli.COMMANDS) == COMMAND_NAMES


def test_bare_invocation_serves_with_clean_argv_and_silent_stdout(
    monkeypatch, capsys,
):
    seen = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(server, "main", fake_main)
    assert cli.main([]) == 0
    assert seen["argv"][1:] == []  # no stray tokens for server argparse
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the MCP wire


def test_serve_subcommand_serves_with_clean_argv(monkeypatch, capsys):
    seen = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(server, "main", fake_main)
    assert cli.main(["serve"]) == 0
    assert seen["argv"][1:] == []  # "serve" itself is consumed
    assert capsys.readouterr().out == ""


def test_serve_restores_sys_argv(monkeypatch):
    monkeypatch.setattr(server, "main", lambda: 0)
    before = list(sys.argv)
    cli.main([])
    assert sys.argv == before


def test_legacy_flags_forward_whole_argv_verbatim(monkeypatch):
    seen = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 5

    monkeypatch.setattr(server, "main", fake_main)
    argv = ["--send-test", "a@b.example", "--subject", "hi"]
    assert cli.main(argv) == 5  # exit code passes through untouched
    assert seen["argv"][1:] == argv


def test_legacy_doctor_flag_forwards(monkeypatch):
    seen = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(server, "main", fake_main)
    assert cli.main(["--doctor"]) == 0
    assert seen["argv"][1:] == ["--doctor"]


def test_pipe_empty_stdin_serves_and_exits_clean(tmp_path):
    """`printf '' | email-mcp` — the real server over a closed stdin must
    exit 0 with an empty stdout (subprocess: no monkeypatching)."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EMAIL_MCP_")}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-m", "email_mcp.cli"],
        input=b"", capture_output=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout == b""


# --------------------------------------------------------------------- #
# dispatch: unknown + stubs                                              #
# --------------------------------------------------------------------- #


def test_unknown_subcommand_exits_2_listing_commands(capsys):
    assert cli.main(["frobnicate"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    for name in COMMAND_NAMES:
        assert name in err


@pytest.mark.parametrize("name,stage", [
    ("setup", "L3"), ("update", "L4"), ("uninstall", "L4"),
])
def test_lifecycle_stubs_exit_2_with_pointer(capsys, name, stage):
    assert cli.main([name]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert name in captured.err
    assert stage in captured.err


# --------------------------------------------------------------------- #
# delegation                                                             #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["audit", "fts", "graph"])
def test_delegates_pass_argv_verbatim(monkeypatch, name):
    mod = importlib.import_module(f"email_mcp.{name}")
    seen = {}

    def fake_main(argv=None) -> int:
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(mod, "main", fake_main)
    assert cli.main([name, "--flag", "value"]) == 7
    assert seen["argv"] == ["--flag", "value"]


def test_dispatcher_delegates_via_sys_argv_shim(monkeypatch):
    from email_mcp import dispatcher

    seen = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 3

    monkeypatch.setattr(dispatcher, "main", fake_main)
    assert cli.main(["dispatcher", "--status"]) == 3
    assert seen["argv"][1:] == ["--status"]


# --------------------------------------------------------------------- #
# version                                                                #
# --------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake $HOME so ~/.email-mcp/meta.json resolution is exercised
    without ever touching the real state tree."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _version_lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_version_without_meta_reports_state_version_0(home, capsys):
    assert cli.main(["version"]) == 0
    pkg, state = _version_lines(capsys)
    assert pkg.startswith("email-mcp ")
    assert pkg.split(" ", 1)[1]  # a non-empty version string
    assert state == "state_version 0"


def test_version_reads_state_version_from_meta_json(home, capsys):
    meta = home / ".email-mcp"
    meta.mkdir()
    (meta / "meta.json").write_text(json.dumps({"state_version": 3}))
    assert cli.main(["version"]) == 0
    assert _version_lines(capsys)[1] == "state_version 3"


def test_version_treats_corrupt_meta_json_as_0(home, capsys):
    meta = home / ".email-mcp"
    meta.mkdir()
    (meta / "meta.json").write_text("{not json")
    assert cli.main(["version"]) == 0
    assert _version_lines(capsys)[1] == "state_version 0"


# --------------------------------------------------------------------- #
# doctor                                                                 #
# --------------------------------------------------------------------- #


def _report(ok: bool) -> dict:
    return {
        "ok": ok,
        "read_only": False,
        "checks": {
            "mail_store": {"ok": True},
            "audit": {"ok": ok, "error": None if ok else "ledger sick"},
        },
        "audit": {"ok": ok},
    }


def test_doctor_green_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(server, "tool_doctor", lambda: _report(True))
    assert cli.main(["doctor"]) == 0
    assert "doctor: green" in capsys.readouterr().out


def test_doctor_red_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(server, "tool_doctor", lambda: _report(False))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL audit" in out
    assert "doctor: RED" in out


def test_doctor_json_emits_the_report_verbatim(monkeypatch, capsys):
    monkeypatch.setattr(server, "tool_doctor", lambda: _report(True))
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _report(True)


FIXES = {
    "op": "opid-1",
    "applied": [{"repair": "state_dir_perms",
                 "finding": "1 state dir not 0700",
                 "action": "chmod 0700 on 1 dir"}],
    "skipped": [{"repair": "legacy_launchd", "reason": "healthy"}],
    "failed": [],
}


def test_doctor_fix_runs_repairs_between_two_doctor_passes(
    monkeypatch, capsys,
):
    calls: list[str] = []
    reports = iter([_report(False), _report(True)])

    def fake_doctor() -> dict:
        calls.append("doctor")
        return next(reports)

    def fake_run_fixes(*, dry_run: bool = False) -> dict:
        calls.append("fixes")
        return FIXES

    from email_mcp import repairs

    monkeypatch.setattr(server, "tool_doctor", fake_doctor)
    monkeypatch.setattr(repairs, "run_fixes", fake_run_fixes)
    assert cli.main(["doctor", "--fix"]) == 0  # green AFTER the fix pass
    assert calls == ["doctor", "fixes", "doctor"]
    out = capsys.readouterr().out
    assert "doctor before: RED" in out
    # per-repair before/after: the finding and what was done about it
    assert "state_dir_perms" in out
    assert "1 state dir not 0700" in out
    assert "chmod 0700 on 1 dir" in out
    assert "doctor: green" in out


def test_doctor_fix_exit_reflects_the_rerun_not_the_fixes(
    monkeypatch, capsys,
):
    reports = iter([_report(False), _report(False)])
    from email_mcp import repairs

    monkeypatch.setattr(server, "tool_doctor", lambda: next(reports))
    monkeypatch.setattr(repairs, "run_fixes",
                        lambda *, dry_run=False: FIXES)
    assert cli.main(["doctor", "--fix"]) == 1  # still red after fixing


def test_doctor_fix_json_shape_is_before_fixes_after(monkeypatch, capsys):
    reports = iter([_report(False), _report(True)])
    from email_mcp import repairs

    monkeypatch.setattr(server, "tool_doctor", lambda: next(reports))
    monkeypatch.setattr(repairs, "run_fixes",
                        lambda *, dry_run=False: FIXES)
    assert cli.main(["doctor", "--fix", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out) == ["before", "fixes", "after"]
    assert out["before"] == _report(False)
    assert out["fixes"] == FIXES
    assert out["after"] == _report(True)


def test_doctor_rejects_unknown_flags_exit_2(capsys):
    assert cli.main(["doctor", "--bogus"]) == 2
    assert "email-mcp doctor" in capsys.readouterr().err


def _pyproject_text() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()


def test_pyproject_declares_no_second_version_literal():
    """The version lives in exactly one place: email_mcp/__init__.py.

    v0.11 audit finding F-NEW-2 was pyproject saying 0.11.0 while the module
    said 0.10.0 — two literals for one fact, disagreeing precisely where it
    is hardest to see (installed package vs bare checkout, then stamped into
    ~/.email-mcp/meta.json). Asserting the two are *equal* would only detect
    the drift; declaring the version dynamic makes it unrepresentable. This
    test fails if anyone reintroduces the second literal.
    """
    import re

    text = _pyproject_text()
    assert not re.search(r'(?m)^version\s*=\s*"', text), (
        "pyproject.toml has a hardcoded [project] version again — it must "
        "stay dynamic and derive from email_mcp.__version__"
    )
    assert re.search(r'(?m)^dynamic\s*=\s*\[\s*"version"\s*\]', text)
    assert re.search(r'(?m)^version\s*=\s*\{\s*attr\s*=\s*"email_mcp\.__version__"', text)


def test_setuptools_expands_the_dynamic_version_to_the_module_literal():
    """The declaration is not merely well-formed — setuptools really resolves
    it, so the wheel metadata carries the module's literal."""
    from pathlib import Path
    import warnings
    import email_mcp

    root = Path(__file__).resolve().parent.parent
    from setuptools.config.pyprojecttoml import read_configuration

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = read_configuration(root / "pyproject.toml", expand=True)
    assert cfg["project"]["version"] == email_mcp.__version__


def test_cli_version_reports_the_running_source_version(capsys):
    """`email-mcp version` must report the version of the code executing."""
    import email_mcp

    assert cli.main(["version"]) == 0
    assert f"email-mcp {email_mcp.__version__}" in capsys.readouterr().out


def test_package_version_ignores_a_stale_installed_distribution(monkeypatch):
    """A stale editable install must not win over the running source.

    lifecycle stamps _package_version() into ~/.email-mcp/meta.json, so
    resolving through importlib.metadata (which answers for the interpreter,
    not the source on sys.path) makes a wrong number durable and invisible.
    """
    import importlib.metadata as md
    import email_mcp

    monkeypatch.setattr(md, "version", lambda name: "0.0.1-stale")
    assert cli._package_version() == email_mcp.__version__


def test_doctor_fix_dry_run_reports_without_touching_anything(
    home, monkeypatch, capsys,
):
    """The registry has implemented dry_run since v0.10 and its docstring
    advertises "a dry run truly touches nothing" — but no CLI flag reached
    it, so the safest way to use the fixer was unreachable from the
    command line."""
    root = home / ".email-mcp"
    root.mkdir(mode=0o755)
    before = _mode(root)

    rc = cli.main(["doctor", "--fix", "--dry-run"])

    out = capsys.readouterr().out
    assert "dry-run: would fix" in out
    assert _mode(root) == before          # nothing repaired
    assert sorted(p.name for p in root.iterdir()) == []   # nothing created
    assert rc in (0, 1)

    # --dry-run without --fix is a usage error, not a silent no-op.
    assert cli.main(["doctor", "--dry-run"]) == 2
