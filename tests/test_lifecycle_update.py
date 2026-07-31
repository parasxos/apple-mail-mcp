"""`email-mcp update` (email_mcp.lifecycle, update half) — v0.11 L4.

Every test runs inside a fake $HOME with every EMAIL_MCP_* override
wiped (then EMAIL_MCP_MAIL_DIR pointed at the in-tmp mail fixture), so
the DEFAULT ~/.email-mcp resolution is exercised end-to-end while the
real estate stays untouchable — the test_repairs / test_lifecycle_setup
pattern. The conftest fts/audit guards are shadowed by name for the same
reason; a PATH-shimmed launchctl/osascript/ssh/security/op set fences
every test from the real launchd, Mail.app, SSH and secret stores.

Update's promises proven here: the MIGRATIONS hook runs pending steps in
ascending target order exactly once (fake migrations — the tuple is
empty at v0.11 BY DESIGN), a failed migration keeps its predecessors'
progress and exits 1, a stale plist (a moved venv bakes a dead
sys.executable into ProgramArguments) is re-rendered + re-bootstrapped
while absent agents are never installed, meta.json is stamped with
state_version/package_version/updated_at, the doctor runs and the
releases URL prints, ONE lifecycle event (outcome `update`) goes on
record per run, and the non-TTY refusal keeps test_cli.py's frozen stub
contract (exit 2, silent stdout, "update" + the remedy on stderr).
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from email_mcp import audit, cli, dispatcher, fts, lifecycle

SPOOL_STATES = ("pending", "sending", "sent", "failed", "cancelled")


@pytest.fixture
def home(tmp_path, monkeypatch, mail_fixture):
    """Fake $HOME, EMAIL_MCP_* wiped, mail store pointed at the in-tmp
    fixture so the closing doctor pass stays inside the sandbox."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    yield h
    audit.set_process("server")  # run_update tags "cli"; restore for the suite


@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name, on purpose): the env pin would
    point the FTS dir OUTSIDE the fake HOME and defeat the
    update-never-creates-it assertion. The fake HOME is the isolation."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason): the lifecycle event must
    land through the real env-driven resolver, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def tool_shims(home, tmp_path, monkeypatch):
    """PATH shims: stateful launchctl (test_repairs pattern), osascript
    answering green, ssh/security/op failing fast so the doctor pass
    never leaves the sandbox. Returns the launchctl invocation log."""
    shim = tmp_path / "shim"
    state = tmp_path / "launchd-state"
    shim.mkdir()
    state.mkdir()
    log = tmp_path / "launchctl.log"
    log.write_text("")
    script = shim / "launchctl"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$LAUNCHCTL_LOG"
        d="$LAUNCHCTL_STATE_DIR"
        case "$1" in
          bootstrap) b=$(basename "$3" .plist); : > "$d/$b"; exit 0 ;;
          bootout)
            if [ -n "$3" ]; then b=$(basename "$3" .plist)
            else b=$(basename "$2"); fi
            rm -f "$d/$b"; exit 0 ;;
          print) b=$(basename "$2"); [ -e "$d/$b" ] && exit 0 || exit 1 ;;
        esac
        exit 0
    """))
    script.chmod(0o755)
    (shim / "osascript").write_text("#!/bin/sh\necho true\nexit 0\n")
    (shim / "osascript").chmod(0o755)
    for tool in ("ssh", "security", "op"):
        (shim / tool).write_text("#!/bin/sh\nexit 1\n")
        (shim / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("LAUNCHCTL_STATE_DIR", str(state))
    return log


def _calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line]


def _update_events() -> list[dict]:
    return [e for e in audit.query(event="lifecycle")["events"]
            if e["outcome"] == "update"]


# --------------------------------------------------------------------- #
# migrations: order, once, failure containment                           #
# --------------------------------------------------------------------- #


def test_migrations_run_in_order_once(home, monkeypatch, capsys):
    """Fake migrations through the real CLI wiring: pending steps run in
    ascending target order (however the tuple is declared), each printed;
    a second run has nothing pending — migrations run ONCE."""
    calls: list[int] = []

    def widen_ledger(meta):
        calls.append(2)
        meta["widened"] = True
        return meta

    def rekey_tokens(meta):
        calls.append(3)
        meta["rekeyed"] = True
        return meta

    # Declared OUT of order on purpose: update must sort by target.
    monkeypatch.setattr(lifecycle, "MIGRATIONS",
                        ((3, rekey_tokens), (2, widen_ledger)))
    monkeypatch.setattr(lifecycle, "STATE_VERSION", 3)
    lifecycle.write_meta({"state_version": 1})

    assert cli.main(["update", "--yes"]) == 0
    assert calls == [2, 3]
    meta = lifecycle.read_meta()
    assert meta["state_version"] == 3
    assert meta["widened"] is True and meta["rekeyed"] is True
    out = capsys.readouterr().out
    assert "migration -> state_version 2: widen_ledger" in out
    assert "migration -> state_version 3: rekey_tokens" in out

    assert cli.main(["update", "--yes"]) == 0
    assert calls == [2, 3]  # nothing pending — never re-run
    assert "no migrations pending" in capsys.readouterr().out


def test_migration_failure_keeps_progress_and_exits_1(
    home, monkeypatch, capsys,
):
    def first(meta):
        meta["first"] = True
        return meta

    def second(meta):
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "MIGRATIONS", ((2, first), (3, second)))
    monkeypatch.setattr(lifecycle, "STATE_VERSION", 3)
    lifecycle.write_meta({"state_version": 1})

    assert cli.main(["update", "--yes"]) == 1
    meta = lifecycle.read_meta()
    assert meta["state_version"] == 2  # first step's progress kept, not 3
    assert meta["first"] is True
    assert "FAILED" in capsys.readouterr().out
    events = _update_events()
    assert len(events) == 1
    assert events[0]["detail"]["failed_migration"] == {
        "target": 3, "migration": "second"}
    assert events[0]["detail"]["migrations"] == ["first"]
    # The re-run resumes AT the failed step — the completed one is not
    # repeated.
    ran: list[str] = []
    monkeypatch.setattr(lifecycle, "MIGRATIONS", (
        (2, lambda meta: ran.append("first") or meta),
        (3, lambda meta: ran.append("second") or meta),
    ))
    assert cli.main(["update", "--yes"]) == 0
    assert ran == ["second"]
    assert lifecycle.read_meta()["state_version"] == 3


def test_update_adopts_metaless_state_version_0(home):
    """The v0.9/v0.10 upgrade path: a real ~/.email-mcp with no
    meta.json reads as state_version 0 and gets stamped current."""
    root = home / ".email-mcp"
    root.mkdir()
    root.chmod(0o700)
    assert cli.main(["update", "--yes"]) == 0
    meta = lifecycle.read_meta()
    assert meta["state_version"] == lifecycle.STATE_VERSION == 1
    assert ((root / "meta.json").stat().st_mode & 0o777) == 0o600
    events = _update_events()
    assert len(events) == 1
    assert events[0]["detail"]["from_state_version"] == 0
    assert events[0]["detail"]["state_version"] == 1


def test_newer_state_version_left_untouched(home, capsys):
    """State written by a NEWER package is never stamped down."""
    lifecycle.write_meta({"state_version": 99})
    assert cli.main(["update", "--yes"]) == 0
    assert lifecycle.read_meta()["state_version"] == 99
    assert "newer than this package" in capsys.readouterr().out


# --------------------------------------------------------------------- #
# plist drift: re-render installed agents, never install absent ones     #
# --------------------------------------------------------------------- #


def test_stale_plist_rerendered(home, tool_shims):
    """A stale plist (dead interpreter path baked in — the moved-venv
    case) on BOTH agents is re-rendered byte-identical to a fresh render
    and re-bootstrapped through the PATH-shimmed launchctl."""
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").write_text(
        "<plist>stale venv path</plist>")
    (agents / f"{fts.LAUNCHD_LABEL}.plist").write_text(
        "<plist>stale venv path</plist>")

    assert cli.main(["update", "--yes"]) == 0

    assert (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").read_text() \
        == dispatcher._plist_content()
    assert (agents / f"{fts.LAUNCHD_LABEL}.plist").read_text() \
        == fts._plist_content()
    calls = _calls(tool_shims)
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL):
        assert any(c.startswith("bootstrap") and label in c
                   for c in calls), label
    events = _update_events()
    assert events[0]["detail"]["agents_refreshed"] == [
        dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL]


def test_current_plists_left_alone_second_run_noop(home, tool_shims):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / f"{dispatcher.LAUNCHD_LABEL}.plist"
    plist.write_text("stale")
    assert cli.main(["update", "--yes"]) == 0
    healed = plist.read_text()
    tool_shims.write_text("")  # reset the launchctl log

    assert cli.main(["update", "--yes"]) == 0
    assert plist.read_text() == healed  # byte-identical, not re-rendered
    assert not any(c.startswith(("bootstrap", "bootout"))
                   for c in _calls(tool_shims))
    assert _update_events()[0]["detail"]["agents_current"] == [
        dispatcher.LAUNCHD_LABEL]


def test_update_never_installs_absent_agents(home, tool_shims):
    """No plists installed: update must not install any (that is setup's
    decision), must not bootstrap anything, and — because the fts render
    is never invoked — must not create the derived fts dir."""
    assert cli.main(["update", "--yes"]) == 0
    agents = home / "Library" / "LaunchAgents"
    assert not (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").exists()
    assert not (agents / f"{fts.LAUNCHD_LABEL}.plist").exists()
    assert not (home / ".email-mcp" / "fts").exists()
    assert not any(c.startswith(("bootstrap", "bootout"))
                   for c in _calls(tool_shims))


# --------------------------------------------------------------------- #
# meta stamp, doctor, releases URL, the event, the TTY gate              #
# --------------------------------------------------------------------- #


def test_update_stamps_meta_runs_doctor_prints_releases(home, capsys):
    assert cli.main(["update", "--yes"]) == 0
    meta = lifecycle.read_meta()
    assert meta["state_version"] == lifecycle.STATE_VERSION
    assert meta["created_at"] and meta["updated_at"]
    assert meta["package_version"] == cli._package_version()
    assert meta["env_overrides"] == ["EMAIL_MCP_MAIL_DIR"]
    out = capsys.readouterr().out
    assert "doctor:" in out  # the doctor verdict line rendered
    assert "https://github.com/parasxos/email-mcp/releases" in out


def test_update_emits_one_lifecycle_event_per_run(home):
    assert cli.main(["update", "--yes"]) == 0
    events = _update_events()
    assert len(events) == 1
    assert events[0]["src"] == "cli"
    detail = events[0]["detail"]
    assert detail["from_state_version"] == 0
    assert detail["state_version"] == 1
    assert detail["migrations"] == []
    assert detail["agents_refreshed"] == []
    assert detail["agents_failed"] == []
    assert detail["package_version"] == cli._package_version()
    assert cli.main(["update", "--yes"]) == 0
    assert len(_update_events()) == 2  # one per run, on record


def test_no_terminal_without_yes_exits_2(home, capsys):
    """Under a captured/closed stdin (as here) update refuses with a
    usage error, printing nothing to stdout and touching nothing. The
    refusal keeps the frozen stub contract that
    test_cli.py::test_lifecycle_stubs_exit_2_with_pointer pins for
    `update`: exit 2, silent stdout, "update" + the remedy on stderr.
    The internal stage label ("L4") used to be pinned here, which is how it
    reached operator-facing text; an audit called that out."""
    assert lifecycle.run_update_cli([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "update" in captured.err and "--yes" in captured.err
    import re as _re
    assert not _re.search(r"\bL[0-9]\b", captured.err), "internal jargon"
    assert "--yes" in captured.err
    assert not (home / ".email-mcp").exists()  # refused = nothing created
    # And through the real dispatch, as the frozen test hits it:
    assert cli.main(["update"]) == 2
