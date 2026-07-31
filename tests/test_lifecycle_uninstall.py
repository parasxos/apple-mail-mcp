"""`email-mcp uninstall` (email_mcp.lifecycle, uninstall half) — v0.11 L4.

Every test runs inside a fake $HOME with every EMAIL_MCP_* override
wiped, so the DEFAULT ~/.email-mcp resolution is exercised end-to-end
while the real estate stays untouchable — the test_repairs /
test_lifecycle_setup pattern (conftest fts/audit guards shadowed by
name; PATH-shimmed launchctl/osascript).

The hard promises proven here are the design-D7 purge fences: purge
removes EXACTLY the hardcoded ~/.email-mcp (plus ~/Library/Logs/
email-mcp*.log), refuses a symlinked root via os.lstat, never removes an
env-overridden dir (even one pointed at $HOME itself — it is printed,
not deleted), refuses degenerate $HOME values, never touches
~/Library/Mail; the lifecycle event is emitted BEFORE removal; Keychain
delete commands are derived from identities.toml READ before the purge
destroys it and 1Password entries are stated as never touched; the typed
confirmation gates the run; and the non-TTY refusal keeps test_cli.py's
frozen stub contract (exit 2, silent stdout, "uninstall" + "L4" on
stderr).
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from email_mcp import audit, cli, dispatcher, fts, lifecycle

LEGACY_LABEL = "com.paris.email-mcp-dispatcher"

IDENTITIES_TOML = textwrap.dedent("""\
    default = "work"

    [work]
    from_addr = "user@example.org"
    from_name = "User"
    driver = "smtp"
    host = "smtp.example.org"
    username = "user@example.org"
    keychain = "email-mcp-work"

    [cern]
    from_addr = "user@cern.ch"
    driver = "smtp"
    host = "smtp.cern.ch"
    op = "op://Work/cern-mail/password"
""")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake $HOME with every EMAIL_MCP_* override wiped: uninstall must
    operate the default tree, and nothing may reach the real estate."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    yield h
    audit.set_process("server")  # run_uninstall tags "cli"; restore

@pytest.fixture(autouse=True)
def state_root_guard(home, monkeypatch):
    """Shadow conftest's root guard by name: conftest PATCHES
    config.state_root, which would override this module's fake HOME. These
    tests need the real env-driven resolution inside that HOME."""
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    return home / ".email-mcp"

@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name, on purpose): the fake HOME is
    the isolation; the env pin would point outside it."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason): the lifecycle event must
    land through the real env-driven resolver, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def tool_shims(home, tmp_path, monkeypatch):
    """PATH shims: stateful launchctl (test_repairs pattern) plus a
    no-op osascript — no test may ever reach the real launchd."""
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
    (shim / "osascript").write_text("#!/bin/sh\nexit 0\n")
    (shim / "osascript").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("LAUNCHCTL_STATE_DIR", str(state))
    return log


def _calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line]


def _uninstall_events() -> list[dict]:
    return [e for e in audit.query(event="lifecycle")["events"]
            if e["outcome"] == "uninstall"]


def _install_estate(home: Path) -> dict[str, Path]:
    """A lived-in installation: full state tree, identities with a
    Keychain AND a 1Password reference, a Graph token cache, both agent
    plists plus a legacy one, log files, and a sentinel Mail store."""
    root = home / ".email-mcp"
    for d in (root, root / "spool", root / "plans", root / "graph",
              root / "audit"):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    identities = root / "identities.toml"
    identities.write_text(IDENTITIES_TOML)
    identities.chmod(0o600)
    (root / "meta.json").write_text(json.dumps({"state_version": 1}))
    (root / "meta.json").chmod(0o600)
    token = root / "graph" / "work.token.json"
    token.write_text("{}")
    token.chmod(0o600)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                  LEGACY_LABEL):
        (agents / f"{label}.plist").write_text("<plist/>")
    logs = home / "Library" / "Logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "email-mcp.log").write_text("mcp log")
    (logs / "unrelated.log").write_text("keep me")
    mail = home / "Library" / "Mail" / "V10" / "MailData"
    mail.mkdir(parents=True, exist_ok=True)
    (mail / "Envelope Index").write_text("mail-store-sentinel")
    return {"root": root, "token": token, "agents": agents,
            "logs": logs, "mail": home / "Library" / "Mail"}


# --------------------------------------------------------------------- #
# base uninstall: agents + tokens out, state kept                        #
# --------------------------------------------------------------------- #


def test_uninstall_removes_agents_and_tokens_keeps_state(
    home, tool_shims, capsys,
):
    estate = _install_estate(home)
    assert cli.main(["uninstall", "--yes"]) == 0
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                  LEGACY_LABEL):
        assert not (estate["agents"] / f"{label}.plist").exists(), label
    calls = _calls(tool_shims)
    assert f"bootout gui/{os.getuid()}/{LEGACY_LABEL}" in calls
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL):
        assert any(c.startswith("bootout") and label in c
                   for c in calls), label
    assert not any(c.startswith("bootstrap") for c in calls)
    assert not estate["token"].exists()
    # Data KEPT: state tree, identities, meta, ledger, logs.
    root = estate["root"]
    assert (root / "identities.toml").read_text() == IDENTITIES_TOML
    assert (root / "meta.json").exists()
    assert (root / "audit").is_dir()
    assert (estate["logs"] / "email-mcp.log").exists()
    out = capsys.readouterr().out
    assert "--purge" in out  # the kept-state pointer names the way out


def test_uninstall_plan_shape_is_side_effect_free(home):
    estate = _install_estate(home)
    before = sorted(str(p) for p in home.rglob("*"))
    plan = lifecycle.uninstall_plan(purge=True)
    assert sorted(plan) == ["env_overrides", "keep", "print_only",
                            "remove"]
    assert sorted(str(p) for p in home.rglob("*")) == before
    assert plan["env_overrides"] == {}
    assert any(str(estate["root"]) in line for line in plan["remove"])
    assert any(str(estate["token"]) == line for line in plan["remove"])
    assert any("Mail" in line and "never touched" in line
               for line in plan["keep"])
    labels = "\n".join(plan["remove"])
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                  LEGACY_LABEL):
        assert label in labels


# --------------------------------------------------------------------- #
# purge: exactly ~/.email-mcp (+ logs), fenced                           #
# --------------------------------------------------------------------- #


def test_purge_removes_exactly_home_email_mcp(home, capsys):
    estate = _install_estate(home)
    sibling = home / "other-data"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not estate["root"].exists()
    assert not (estate["logs"] / "email-mcp.log").exists()
    # ...and NOTHING else:
    assert (sibling / "keep.txt").read_text() == "keep"
    assert (estate["logs"] / "unrelated.log").read_text() == "keep me"
    assert (estate["mail"] / "V10" / "MailData"
            / "Envelope Index").read_text() == "mail-store-sentinel"
    out = capsys.readouterr().out
    assert "receipts.jsonl" in out  # the receipts-export hint printed


def test_purge_refuses_symlinked_root(home, tmp_path, capsys):
    """A symlink squatting on ~/.email-mcp is a redirection attack:
    os.lstat sees it, PurgeRefused fires, the target survives — and a
    plain file squatting there is refused too."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data.txt").write_text("precious")
    root = home / ".email-mcp"
    root.symlink_to(victim)

    # "refusing to follow", not "symlink": this test's own name puts the
    # word "symlink" in tmp_path, so it appears in every interpolated
    # path and would match whichever fence happened to fire.
    with pytest.raises(lifecycle.PurgeRefused, match="refusing to follow"):
        lifecycle.purge_state()
    assert root.is_symlink()  # the link itself was not removed either
    assert (victim / "data.txt").read_text() == "precious"

    # Through the CLI: exit 1, target still intact.
    assert cli.main(["uninstall", "--purge", "--yes"]) == 1
    assert (victim / "data.txt").read_text() == "precious"
    assert "purge refused" in capsys.readouterr().err

    root.unlink()
    root.write_text("file squatting on the state root")
    with pytest.raises(lifecycle.PurgeRefused, match="not a directory"):
        lifecycle.purge_state()
    assert root.read_text() == "file squatting on the state root"


def test_purge_ignores_env_overridden_dirs_prints_them(
    home, tmp_path, monkeypatch, capsys,
):
    estate = _install_estate(home)
    outside_spool = tmp_path / "outside-spool"
    outside_spool.mkdir()
    (outside_spool / "pending.json").write_text("spooled")
    outside_graph = tmp_path / "outside-graph"
    outside_graph.mkdir()
    (outside_graph / "work.token.json").write_text("{}")
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(outside_spool))
    monkeypatch.setenv("EMAIL_MCP_GRAPH_DIR", str(outside_graph))

    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not estate["root"].exists()  # the hardcoded root went
    # The env-overridden dirs were ONLY printed, never removed:
    assert (outside_spool / "pending.json").read_text() == "spooled"
    assert (outside_graph / "work.token.json").exists()
    out = capsys.readouterr().out
    assert "EMAIL_MCP_SPOOL_DIR" in out and "never removed" in out
    assert any("EMAIL_MCP_GRAPH_DIR override" in line
               for line in out.splitlines())


def test_purge_env_override_at_home_survives(home, monkeypatch):
    """The L6 red-team headline case: EMAIL_MCP_SPOOL_DIR=$HOME. A naive
    purge of "the spool dir" would delete the entire home directory; the
    hardcoded-target fence means exactly ~/.email-mcp goes and every
    sibling survives."""
    estate = _install_estate(home)
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.txt").write_text("irreplaceable")
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(home))

    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not estate["root"].exists()
    assert home.is_dir()
    assert (home / "Documents" / "thesis.txt").read_text() \
        == "irreplaceable"
    assert (estate["mail"] / "V10" / "MailData"
            / "Envelope Index").exists()


@pytest.mark.parametrize("raw_home", ["/", "", "//", "/..", "/../.."])
def test_purge_paranoia_fences_home_at_root_however_spelled(
    monkeypatch, raw_home,
):
    """A $HOME that names the filesystem root must refuse before lstat,
    let alone rmtree — target /.email-mcp. HOME= (empty) resolves to /
    via expanduser's fallback; the rest reach it through ".." and "//"
    segments that pathlib carries around verbatim, so the fence has to
    compare the realpath, not the spelling."""
    monkeypatch.setenv("HOME", raw_home)
    with pytest.raises(lifecycle.PurgeRefused, match="refusing to purge"):
        lifecycle.purge_state()


def test_purge_refuses_relative_home_without_resolving_it(
    tmp_path, monkeypatch,
):
    """A relative $HOME is nonsense and stays refused. It is judged on
    the RAW value on purpose: realpath would anchor it to the cwd and
    hand back a plausible-looking absolute path, turning a refusal into
    a purge of whatever ./relhome/.email-mcp happens to be."""
    victim = tmp_path / "relhome" / ".email-mcp"
    victim.mkdir(parents=True)
    (victim / "meta.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "relhome")

    with pytest.raises(lifecycle.PurgeRefused, match="refusing to purge"):
        lifecycle.purge_state()
    assert (victim / "meta.json").exists()


def test_purge_already_absent_is_idempotent(home):
    """Repeat purges are safe. (Via the CLI the pre-removal audit emit
    recreates ~/.email-mcp/audit — the ledger's last words — so the CLI
    always finds a root to remove; the absent-root branch is
    purge_state()'s None, never an error.)"""
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not (home / ".email-mcp").exists()
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not (home / ".email-mcp").exists()
    assert lifecycle.purge_state() is None  # absent root: no-op, no error


# --------------------------------------------------------------------- #
# ordering, sentinels, keychain honesty                                  #
# --------------------------------------------------------------------- #


def test_event_emitted_before_removal(home, monkeypatch):
    """The lifecycle event is the ledger's record of the run — it must
    be durable BEFORE the first removal step executes."""
    estate = _install_estate(home)
    seen: dict = {}
    real = dispatcher.uninstall_launchd

    def spy() -> str:
        seen["events"] = _uninstall_events()
        seen["token_still_there"] = estate["token"].exists()
        return real()

    monkeypatch.setattr(dispatcher, "uninstall_launchd", spy)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert seen["events"], "no lifecycle event on the ledger at removal time"
    event = seen["events"][0]
    assert event["src"] == "cli"
    assert event["detail"]["purge"] is False
    assert seen["token_still_there"] is True  # nothing removed yet
    # And no SECOND event after the removals:
    assert len(_uninstall_events()) == 1


def test_never_touches_library_mail(home):
    """The sentinel test: a populated ~/Library/Mail must be
    byte-for-byte identical after the most destructive run we ship."""
    estate = _install_estate(home)
    mail = estate["mail"]
    extra = mail / "V10" / "Accounts.plist"
    extra.write_text("accounts-sentinel")
    before = {str(p): (p.read_bytes() if p.is_file() else None)
              for p in mail.rglob("*")}
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    after = {str(p): (p.read_bytes() if p.is_file() else None)
             for p in mail.rglob("*")}
    assert after == before


def test_keychain_instructions_read_before_purge(home, capsys):
    """The delete commands come from identities.toml, which --purge is
    about to destroy: they must still print (the file was parsed FIRST),
    and the 1Password reference must be declared never-touched."""
    _install_estate(home)
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    out = capsys.readouterr().out
    assert ("security delete-generic-password -s email-mcp-work "
            "-a user@example.org") in out
    assert "1Password" in out and "never touched" in out
    assert not (home / ".email-mcp").exists()  # source file gone, yet printed


def test_keychain_instructions_lines():
    """Unit shape: no refs → nothing; op-only → just the 1Password
    statement; keychain → one command per item, account preferred from
    `username` then `from_addr`."""
    assert lifecycle.keychain_instructions({}) == []
    assert lifecycle.keychain_instructions({"default": "x"}) == []
    op_only = lifecycle.keychain_instructions(
        {"a": {"op": "op://v/i/f", "from_addr": "a@example.org"}})
    assert len(op_only) == 1 and "1Password" in op_only[0]
    lines = lifecycle.keychain_instructions({
        "b": {"keychain": "item-b", "from_addr": "b@example.org"},
        "c": {"keychain": "item-c", "username": "cuser",
              "from_addr": "c@example.org"},
    })
    joined = "\n".join(lines)
    assert "security delete-generic-password -s item-b -a b@example.org" \
        in joined
    assert "security delete-generic-password -s item-c -a cuser" in joined
    assert "never deleted" in joined


# --------------------------------------------------------------------- #
# confirmation + the TTY gate                                            #
# --------------------------------------------------------------------- #


def test_typed_confirmation_gate(home, monkeypatch, capsys):
    estate = _install_estate(home)
    plist = estate["agents"] / f"{dispatcher.LAUNCHD_LABEL}.plist"

    # Wrong word: abort, nothing removed, no event.
    monkeypatch.setattr(lifecycle, "_input", lambda _: "nope")
    assert lifecycle.run_uninstall(purge=False, assume_yes=False) == 1
    assert plist.exists() and estate["token"].exists()
    assert _uninstall_events() == []
    assert "aborted" in capsys.readouterr().out

    # "uninstall" typed while --purge demands "purge": abort too.
    monkeypatch.setattr(lifecycle, "_input", lambda _: "uninstall")
    assert lifecycle.run_uninstall(purge=True, assume_yes=False) == 1
    assert estate["root"].exists()

    # The right word proceeds.
    monkeypatch.setattr(lifecycle, "_input", lambda _: "uninstall")
    assert lifecycle.run_uninstall(purge=False, assume_yes=False) == 0
    assert not plist.exists() and not estate["token"].exists()
    assert estate["root"].exists()  # no --purge: state kept


def test_no_terminal_without_yes_exits_2(home, capsys):
    """Under a captured/closed stdin (as here) the typed confirmation is
    impossible: refuse with a usage error, print nothing to stdout,
    remove nothing. Keeps the frozen stub contract that
    test_cli.py::test_lifecycle_stubs_exit_2_with_pointer pins for
    `uninstall`: exit 2, silent stdout, "uninstall" + "L4" on stderr."""
    estate = _install_estate(home)
    assert lifecycle.run_uninstall_cli([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "uninstall" in captured.err and "L4" in captured.err
    assert "--yes" in captured.err
    assert (estate["agents"]
            / f"{dispatcher.LAUNCHD_LABEL}.plist").exists()
    assert estate["token"].exists()
    # And through the real dispatch, as the frozen test hits it:
    assert cli.main(["uninstall", "--purge"]) == 2
    assert estate["root"].exists()


# --------------------------------------------------------------------- #
# partial failure: "complete" must not be a lie                          #
# --------------------------------------------------------------------- #


def test_purge_with_undeletable_subtree_reports_instead_of_raising(
    home, tool_shims, capsys,
):
    """An undeletable subtree (0500 dir holding a file) makes rmtree
    raise partway. The CLI never dumps a traceback as UX: it says the
    purge is incomplete, does NOT claim completion, and exits 1."""
    estate = _install_estate(home)
    locked = estate["root"] / "spool" / "pending"
    locked.mkdir(parents=True, exist_ok=True)
    (locked / "stuck.json").write_text("{}")
    locked.chmod(0o500)
    try:
        assert cli.main(["uninstall", "--purge", "--yes"]) == 1
        captured = capsys.readouterr()
        assert "purge incomplete" in captured.err
        assert "INCOMPLETE" in captured.err
        assert "uninstall complete." not in captured.out
        assert estate["root"].exists()  # the half-deleted tree is named
    finally:
        locked.chmod(0o700)


def test_purge_refuses_a_symlinked_logs_dir(home, tool_shims, capsys):
    """~/Library/Logs redirected at someone else's directory: the log
    sweep follows the same D7 rule as the graph token sweep — refuse,
    never glob through the link. The victim's own email-mcp.log lives."""
    victim = home / "victim-logs"
    victim.mkdir()
    (victim / "email-mcp.log").write_text("someone else's log")
    estate = _install_estate(home)
    logs = estate["logs"]
    (logs / "email-mcp.log").unlink()
    (logs / "unrelated.log").unlink()
    logs.rmdir()
    logs.symlink_to(victim)

    # The preview says so BEFORE the confirmation, not after the fact.
    plan = lifecycle.uninstall_plan(purge=True)
    assert any(str(logs) in line and "never followed" in line
               for line in plan["print_only"])
    assert not any("email-mcp.log" in line for line in plan["remove"])

    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert (victim / "email-mcp.log").read_text() == "someone else's log"
    assert logs.is_symlink()  # the link itself was not removed either
    assert not estate["root"].exists()  # the real purge still happened


def test_uninstall_without_launchctl_exits_nonzero(
    home, tmp_path, monkeypatch, capsys,
):
    """launchctl missing: the plists are printed under "remove:" and
    then survive. Reporting success would leave the agents RUNNING after
    a "successful" uninstall — exit 1 and name what is still there."""
    estate = _install_estate(home)
    empty = tmp_path / "no-tools"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert cli.main(["uninstall", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "uninstall complete." not in captured.out
    assert "INCOMPLETE" in captured.err
    assert "launchd" in captured.err
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL):
        assert (estate["agents"] / f"{label}.plist").exists(), label
    # The rest of the run still happened: token caches are gone.
    assert not estate["token"].exists()
