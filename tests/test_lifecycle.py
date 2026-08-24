"""Lifecycle verbs suite (email_mcp.lifecycle) — v0.11-clean S3.

setup / update / uninstall / doctor --fix as thin content over the plan
and checks primitives; confirm_and_run's typed confirmation, --yes,
dry-run purity and the exit-code fold.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from email_mcp import checks, dispatcher, fts, lifecycle, plan, state


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake $HOME with LaunchAgents + Logs, and NO state override — the
    default-root branches are under test here."""
    h = tmp_path / "home"
    (h / "Library" / "LaunchAgents").mkdir(parents=True)
    (h / "Library" / "Logs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    monkeypatch.delenv("EMAIL_MCP_LOG_FILE", raising=False)
    monkeypatch.delenv("EMAIL_MCP_IDENTITIES", raising=False)
    return h


@pytest.fixture
def installed(home) -> Path:
    """A complete fake install: adopted tree, token cache, both agent
    plists, a legacy plist, rotated logs. Returns the state root."""
    writer = state.State.resolve().adopt()
    (writer.graph / "main.token.json").write_text("{}")
    agents = home / "Library" / "LaunchAgents"
    (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").write_text("<plist/>")
    (agents / f"{fts.LAUNCHD_LABEL}.plist").write_text("<plist/>")
    (agents / f"{dispatcher.LEGACY_LABELS[0]}.plist").write_text("<plist/>")
    logs = home / "Library" / "Logs"
    (logs / "email-mcp.log").write_text("log")
    (logs / "email-mcp.log.1").write_text("older log")
    return writer.root


@pytest.fixture
def launchctl(monkeypatch):
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(plan, "_launchctl", fake)
    return calls


def _script(monkeypatch, answers: list[str]) -> None:
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def _no_input(monkeypatch) -> None:
    def boom(prompt=""):
        raise AssertionError("input() must not be called")

    monkeypatch.setattr("builtins.input", boom)


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


# ---------------------------------------------------------------------- #
# plan_uninstall — the shapes                                              #
# ---------------------------------------------------------------------- #


def test_uninstall_plan_on_a_fresh_home(home):
    rows = lifecycle.plan_uninstall()
    assert [type(r) for r in rows] == [plan.Kept]
    (purge_row,) = lifecycle.plan_uninstall(purge=True)
    assert isinstance(purge_row, plan.Kept)
    assert "already absent" in purge_row.note


def test_uninstall_plan_installed_keeps_state(home, installed):
    rows = lifecycle.plan_uninstall()
    booted = {r.label for r in rows if isinstance(r, plan.BootoutAgent)}
    assert booted == {dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                      dispatcher.LEGACY_LABELS[0]}
    unlinked = {r.path for r in rows if isinstance(r, plan.UnlinkFile)}
    assert installed / "graph" / "main.token.json" in unlinked
    assert not any(isinstance(r, plan.RemoveTree) for r in rows)
    kept = [r for r in rows if isinstance(r, plan.Kept)]
    assert kept and kept[0].path == installed and "--purge" in kept[0].note
    # no log rows without purge:
    assert not any("Logs" in str(getattr(r, "path", "")) for r in rows)


def test_uninstall_plan_purge_adds_tree_and_logs(home, installed):
    rows = lifecycle.plan_uninstall(purge=True)
    trees = [r for r in rows if isinstance(r, plan.RemoveTree)]
    assert [t.root for t in trees] == [installed]
    logs = {r.path.name for r in rows if isinstance(r, plan.UnlinkFile)
            and "Logs" in str(r.path)}
    assert logs == {"email-mcp.log", "email-mcp.log.1"}


def test_purge_preview_counts_corrupt_undelivered_records(home, installed):
    """A purge warning counts files, not only manifests that still parse."""
    pending = installed / "spool" / "pending"
    pending.mkdir(parents=True)
    (pending / "good.json").write_text("{}")
    (pending / "good.eml").write_text("message")
    (pending / "torn.json").write_text('{"id":')

    rows = lifecycle.plan_uninstall(purge=True)
    notes = [r.text for r in rows if isinstance(r, plan.PrintOnly)]

    assert any("2 undelivered scheduled email record(s)" in n for n in notes)
    assert any("unreadable or incomplete" in n for n in notes)


def test_overridden_state_root_is_print_only(home, installed, monkeypatch,
                                             tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(other))
    rows = lifecycle.plan_uninstall(purge=True)
    assert not any(isinstance(r, plan.RemoveTree) for r in rows)
    (note,) = [r for r in rows if isinstance(r, plan.PrintOnly)]
    assert "overridden" in note.text and str(other) in note.text


def test_refused_state_root_is_print_only(home, monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "old"))
    rows = lifecycle.plan_uninstall(purge=True)
    reason = state.State.resolve().reason
    (note,) = [r for r in rows if isinstance(r, plan.PrintOnly)]
    assert "refused" in note.text and reason in note.text
    assert not any(isinstance(r, plan.RemoveTree) for r in rows)


def test_overridden_log_file_is_print_only(home, installed, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_LOG_FILE", "/tmp/custom.log")
    rows = lifecycle.plan_uninstall(purge=True)
    notes = [r.text for r in rows if isinstance(r, plan.PrintOnly)]
    assert any("EMAIL_MCP_LOG_FILE" in t and "not removed" in t
               for t in notes)


# ---------------------------------------------------------------------- #
# confirm_and_run — the one gate                                           #
# ---------------------------------------------------------------------- #


def test_preview_is_render_of_the_list_execute_consumes(
    home, installed, launchctl, monkeypatch, capsys,
):
    """THE anti-divergence acceptance: the preview text shown before the
    typed confirmation is render() of the SAME list object execute()
    then consumes."""
    rendered, executed = [], []
    real_render, real_execute = plan.render, plan.execute

    def spy_render(rows):
        rendered.append(rows)
        return real_render(rows)

    def spy_execute(rows, *, verb):
        executed.append(rows)
        return real_execute(rows, verb=verb)

    monkeypatch.setattr(plan, "render", spy_render)
    monkeypatch.setattr(plan, "execute", spy_execute)
    _script(monkeypatch, ["uninstall"])
    rows = lifecycle.plan_uninstall()
    assert lifecycle.confirm_and_run(rows, verb="uninstall") == 0
    assert rendered[0] is executed[0] is rows
    out = capsys.readouterr().out
    assert real_render(rows) in out


def test_yes_skips_the_preview_but_reports_results(
    home, installed, launchctl, monkeypatch, capsys,
):
    """Under --yes the results ARE the report: each row prints once with
    its outcome, never twice (preview + result stuttered every line in
    the first-user transcript, 2026-08-04)."""
    _no_input(monkeypatch)
    rows = lifecycle.plan_uninstall()
    assert lifecycle.confirm_and_run(rows, verb="uninstall", yes=True) == 0
    out = capsys.readouterr().out
    line = f"remove file {installed / 'graph' / 'main.token.json'}"
    assert out.count(line) == 1
    assert f"ok: {line}" in out


def test_dry_run_provably_touches_nothing(home, installed, monkeypatch,
                                          capsys):
    """THE dry-run acceptance: a full purge preview against a populated
    home changes not one path, mode, kind or size — and launchctl is
    never spawned (no seam is even installed)."""
    _no_input(monkeypatch)
    before = _snapshot(home)
    assert lifecycle.uninstall(purge=True, dry_run=True) == 0
    assert _snapshot(home) == before
    out = capsys.readouterr().out
    assert f"remove tree {installed}" in out


def test_typed_confirmation_gates_execution(home, installed, launchctl,
                                            monkeypatch, capsys):
    _script(monkeypatch, ["no thanks"])
    assert lifecycle.uninstall(purge=True) == 1
    assert installed.is_dir()  # nothing ran
    assert launchctl == []
    assert "aborted" in capsys.readouterr().out

    _script(monkeypatch, ["uninstall"])
    assert lifecycle.uninstall(purge=True) == 0
    assert not installed.exists()


def test_yes_skips_the_prompt(home, installed, launchctl, monkeypatch):
    _no_input(monkeypatch)
    assert lifecycle.uninstall(yes=True) == 0


def test_exit_code_is_a_fold_over_the_rendered_results(home, tmp_path,
                                                       monkeypatch, capsys):
    d = tmp_path / "ro"
    d.mkdir()
    locked = d / "file"
    locked.write_text("x")
    os.chmod(d, 0o500)
    try:
        code = lifecycle.confirm_and_run(
            [plan.UnlinkFile(locked)], verb="uninstall", yes=True)
    finally:
        os.chmod(d, 0o700)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED" in out and "1 left behind" in out


def test_actionless_plan_needs_no_confirmation(home, monkeypatch, capsys):
    _no_input(monkeypatch)
    code = lifecycle.confirm_and_run(
        [plan.PrintOnly("nothing here")], verb="uninstall")
    assert code == 0
    assert "nothing to do." in capsys.readouterr().out


# ---------------------------------------------------------------------- #
# uninstall end to end                                                     #
# ---------------------------------------------------------------------- #


def test_uninstall_keeps_state_without_purge(home, installed, launchctl,
                                             monkeypatch):
    _no_input(monkeypatch)
    assert lifecycle.uninstall(yes=True) == 0
    assert installed.is_dir()
    assert (installed / state.MARKER).is_file()
    assert not (installed / "graph" / "main.token.json").exists()
    agents = home / "Library" / "LaunchAgents"
    assert list(agents.iterdir()) == []
    booted = {c[1] for c in launchctl if c[0] == "bootout"}
    assert booted == {f"gui/{os.getuid()}/{label}" for label in
                      (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                       dispatcher.LEGACY_LABELS[0])}
    # the log survives without purge:
    assert (home / "Library" / "Logs" / "email-mcp.log").exists()


def test_uninstall_purge_removes_the_works(home, installed, launchctl,
                                           monkeypatch):
    _no_input(monkeypatch)
    assert lifecycle.uninstall(purge=True, yes=True) == 0
    assert not installed.exists()
    assert not (home / "Library" / "Logs" / "email-mcp.log").exists()
    assert not (home / "Library" / "Logs" / "email-mcp.log.1").exists()
    assert list((home / "Library" / "LaunchAgents").iterdir()) == []


# ---------------------------------------------------------------------- #
# update — the migration slice                                             #
# ---------------------------------------------------------------------- #


def test_update_stamps_meta_and_rerenders_stale_plists(home, launchctl,
                                                       monkeypatch, capsys):
    _no_input(monkeypatch)
    state.State.resolve().adopt()
    plist = dispatcher._plist_path()
    plist.write_text("<plist>stale</plist>")
    assert lifecycle.update(yes=True) == 0
    root = home / ".email-mcp"
    assert json.loads((root / checks.META).read_text()) == \
        {"state_version": checks.STATE_VERSION}
    assert plist.read_text() == dispatcher._plist_content()
    assert launchctl[-1][0] == "bootstrap"

    capsys.readouterr()
    assert lifecycle.update(yes=True) == 0  # idempotent: nothing pending
    assert "up to date" in capsys.readouterr().out


def test_update_never_repairs_outside_its_slice(home, monkeypatch):
    _no_input(monkeypatch)
    writer = state.State.resolve().adopt()
    plans = writer.plans  # materialize once; the accessor itself heals modes
    os.chmod(plans, 0o755)  # a tree_modes finding — doctor's business
    assert lifecycle.update(yes=True) == 0
    assert stat.S_IMODE(os.lstat(writer.root / "plans").st_mode) == 0o755
    os.chmod(plans, 0o700)


def test_update_refused_root_exits_nonzero(home, monkeypatch, tmp_path,
                                           capsys):
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "old"))
    assert lifecycle.update(yes=True) == 1
    assert "retired" in capsys.readouterr().out


# ---------------------------------------------------------------------- #
# doctor --fix — the registry through the same executor                    #
# ---------------------------------------------------------------------- #


def test_doctor_fix_repairs_the_squat(home, monkeypatch, capsys):
    _no_input(monkeypatch)
    writer = state.State.resolve().adopt()
    (writer.root / "audit").write_text("squatter")
    assert lifecycle.doctor_fix(yes=True) == 0
    assert (writer.root / "audit.bak").read_text() == "squatter"


def test_doctor_fix_dry_run_is_pure(home, monkeypatch, capsys):
    """THE --fix dry-run acceptance, against the worst input: an
    unadopted 0755 root plus a legacy plist pending. Not one path, mode,
    kind or size changes — adoption included, because adoption is the
    plan's first row, never a plan-build effect. A declined typed
    confirmation is equally pure."""
    root = home / ".email-mcp"
    root.mkdir()
    os.chmod(root, 0o755)
    legacy = (home / "Library" / "LaunchAgents" /
              f"{dispatcher.LEGACY_LABELS[0]}.plist")
    legacy.write_text("<plist/>")
    before = _snapshot(home)
    _no_input(monkeypatch)
    assert lifecycle.doctor_fix(dry_run=True) == 0
    assert _snapshot(home) == before
    out = capsys.readouterr().out
    assert f"adopt state root {root}" in out
    assert f"remove file {legacy}" in out

    _script(monkeypatch, ["nope"])
    assert lifecycle.doctor_fix() == 1
    assert _snapshot(home) == before
    assert "aborted" in capsys.readouterr().out


def test_doctor_fix_nothing_to_fix(home, monkeypatch, capsys):
    _no_input(monkeypatch)
    state.State.resolve().adopt()
    lifecycle.doctor_fix(yes=True)  # stamps meta
    capsys.readouterr()
    assert lifecycle.doctor_fix(yes=True) == 0
    assert "nothing to fix." in capsys.readouterr().out


def test_doctor_fix_refused_root_exits_nonzero(home, monkeypatch, tmp_path,
                                               capsys):
    monkeypatch.setenv("EMAIL_MCP_GRAPH_DIR", str(tmp_path / "old"))
    assert lifecycle.doctor_fix(yes=True) == 1
    assert "cannot fix" in capsys.readouterr().out
    assert not (tmp_path / "old").exists()


# ---------------------------------------------------------------------- #
# setup — the smallest wizard                                              #
# ---------------------------------------------------------------------- #


@pytest.fixture
def smoke(monkeypatch):
    """Canned doctor report — setup's smoke test must not probe the real
    Mail.app / osascript from inside the suite."""
    report = {"ok": True, "read_only": False,
              "checks": {"mail_store": {"ok": True, "detail": "42 messages"}},
              "audit": {"ok": True, "detail": "no events yet"}}
    monkeypatch.setattr("email_mcp.doctor.run", lambda: report)
    return report


def test_setup_yes_adopts_stamps_and_prints_config(home, smoke, monkeypatch,
                                                   capsys):
    _no_input(monkeypatch)  # --yes takes no answers at all
    assert lifecycle.setup(yes=True) == 0
    root = home / ".email-mcp"
    assert (root / state.MARKER).is_file()
    for leaf in ("spool", "plans", "graph", "fts", "audit"):
        assert (root / leaf).is_dir()
    assert json.loads((root / checks.META).read_text()) == \
        {"state_version": checks.STATE_VERSION}
    assert not (root / "identities.toml").exists()  # optional pieces skipped
    out = capsys.readouterr().out
    assert '"apple-mail"' in out and "email_mcp.server" in out
    assert "42 messages" in out  # the smoke test rendered


def test_setup_interactive_smtp_identity_never_stores_the_secret(
    home, smoke, launchctl, monkeypatch, capsys,
):
    _script(monkeypatch, [
        "paris@example.org",      # From: address
        "Paris",                  # display name
        "",                       # driver -> smtp (default)
        "smtp.example.org",       # SMTP host
        "",                       # port -> 587
        "",                       # username -> from_addr
        "",                       # keychain item -> email-mcp-smtp
        "n",                      # Microsoft/Exchange mailbox? -> no
        "n",                      # dispatcher agent
        "n",                      # fts agent
        "n",                      # fts build
    ])
    assert lifecycle.setup() == 0
    toml_path = home / ".email-mcp" / "identities.toml"
    text = toml_path.read_text()
    assert stat.S_IMODE(os.lstat(toml_path).st_mode) == 0o600
    assert 'from_addr = "paris@example.org"' in text
    assert 'driver = "smtp"' in text
    assert "port = 587" in text
    assert 'keychain = "email-mcp-smtp"' in text
    assert "password" not in text.lower()  # NEVER a secret value in a file
    out = capsys.readouterr().out
    assert ("security add-generic-password -a paris@example.org "
            "-s email-mcp-smtp -w") in out
    # the file parses as the identities module expects:
    from email_mcp import identities

    idents, default = identities.load()
    assert default == "main" and idents["main"].driver == "smtp"


def test_setup_installs_the_agents_when_asked(home, smoke, launchctl,
                                              monkeypatch):
    _script(monkeypatch, [
        "",     # From: address -> skip identity
        "y",    # background sender
        "y",    # nightly index refresh
        "n",    # body-search index build
    ])
    assert lifecycle.setup() == 0
    assert dispatcher._plist_path().read_text() == \
        dispatcher._plist_content()
    assert fts._plist_path().read_text() == fts._plist_content()
    assert [c[0] for c in launchctl].count("bootstrap") == 2


def test_setup_defaults_drive_to_yes(home, smoke, launchctl, monkeypatch):
    """Bare Enter on every optional question must land the user in the
    good state: both agents installed, index built. The first user
    answered 'n' to prompts she could not evaluate and lost body search
    without knowing it existed (2026-08-04)."""
    built = []
    monkeypatch.setattr("email_mcp.fts.FtsIndex.build",
                        lambda self: built.append(True) or {"scanned": 0})
    _script(monkeypatch, ["", "", "", ""])  # identity skip + 3 bare Enters
    assert lifecycle.setup() == 0
    assert dispatcher._plist_path().exists()
    assert fts._plist_path().exists()
    assert built == [True]


def test_setup_keeps_installed_agents_without_asking(home, smoke, launchctl,
                                                     monkeypatch, capsys):
    """A returning user must not be re-asked about agents that are
    already there — and 'n' never silently meant 'remove'."""
    agents = home / "Library" / "LaunchAgents"
    (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").write_text(
        dispatcher._plist_content())
    (agents / f"{fts.LAUNCHD_LABEL}.plist").write_text(fts._plist_content())
    _script(monkeypatch, [
        "",     # From: address -> skip identity
        "n",    # body-search index build (agents never asked)
    ])
    assert lifecycle.setup() == 0
    out = capsys.readouterr().out
    assert "background sender already installed" in out
    assert "nightly index refresh already installed" in out
    assert "install the" not in out


def test_setup_skips_the_build_prompt_when_the_index_is_ready(
    home, smoke, monkeypatch, capsys,
):
    monkeypatch.setattr("email_mcp.fts.status", lambda: {"state": "ready"})
    _script(monkeypatch, [
        "",     # From: address -> skip identity
        "n",    # background sender
        "n",    # nightly index refresh
    ])
    assert lifecycle.setup() == 0
    assert "already built — kept" in capsys.readouterr().out


# ---------------------------------------------------------------------- #
# setup — nightly-refresh aftercare (FDA walk + verify)                    #
# ---------------------------------------------------------------------- #


class SeqLaunchctl:
    """plan._launchctl stand-in: kickstart always succeeds; each `print`
    pops the next canned service dump."""

    def __init__(self, dumps: list[str]):
        self.dumps = list(dumps)
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        out = ""
        if args[0] == "print":
            out = self.dumps.pop(0) if self.dumps else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def DUMP(runs: int, code: int) -> str:
    return (f"state = not running\nruns = {runs}\n"
            f"last exit code = {code}\n")


RUNNING = "state = running\nruns = 1\n"


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def _write_agent_log(text: str) -> None:
    log = fts._log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text)


def test_aftercare_is_a_noop_without_the_agent(home, monkeypatch):
    _no_input(monkeypatch)
    lifecycle._fts_agent_aftercare()  # no plist — returns silently


def test_aftercare_verifies_a_green_agent_and_asks_nothing(
    home, monkeypatch, capsys, no_sleep,
):
    fts._plist_path().write_text("<plist/>")
    # baseline runs=4; post-kick runs=5 exit 0 — a FRESH verdict
    monkeypatch.setattr(plan, "_launchctl",
                        SeqLaunchctl([DUMP(4, 0), DUMP(5, 0)]))
    _no_input(monkeypatch)
    lifecycle._fts_agent_aftercare()
    assert "verified" in capsys.readouterr().out


def test_verify_never_trusts_a_stale_exit_code(home, monkeypatch, no_sleep):
    """The loaded-machine trap: launchd has not spawned the kicked run
    yet, so every dump still shows LAST NIGHT's exit 0 with an
    unchanged runs counter. That is not evidence about the kicked run —
    the verdict must be 'cannot judge', never 'verified'."""
    fts._plist_path().write_text("<plist/>")
    monkeypatch.setattr(
        plan, "_launchctl",
        SeqLaunchctl([DUMP(3, 0), DUMP(3, 0), DUMP(3, 0)]))
    assert lifecycle._verify_fts_agent() is None


def test_aftercare_walks_the_fda_grant_until_the_agent_runs(
    home, monkeypatch, capsys, no_sleep,
):
    """The first-user machine (2026-08-04): agent installed three days
    earlier, dying with PermissionError every night. Aftercare kicks it,
    sees the death, reads the CAUSE off the agent's own log, opens the
    FDA pane + reveals the python binary, waits for the human,
    re-verifies — green only when a real run survives."""
    fts._plist_path().write_text("<plist/>")
    _write_agent_log("PermissionError: [Errno 1] Operation not permitted: "
                     "'/Users/u/Library/Mail'\n")
    fake = SeqLaunchctl([DUMP(1, 1), DUMP(2, 1),             # verify 1: died
                         DUMP(2, 1),                          # baseline 2
                         RUNNING, RUNNING, RUNNING, RUNNING])  # verify 2
    monkeypatch.setattr(plan, "_launchctl", fake)
    opened = []
    monkeypatch.setattr(
        lifecycle, "_open_in_macos",
        lambda target, reveal=False: opened.append((target, reveal)))
    _script(monkeypatch, [""])                # Enter after granting
    lifecycle._fts_agent_aftercare()
    out = capsys.readouterr().out
    assert "Full Disk Access" in out
    assert "verified" in out
    assert any("Privacy_AllFiles" in t for t, _ in opened)   # the pane
    assert any(reveal for _, reveal in opened)               # the binary
    assert [c[0] for c in fake.calls].count("kickstart") == 2


def test_aftercare_skip_removes_the_agent_instead_of_leaving_it_red(
    home, monkeypatch, capsys, no_sleep,
):
    """The wizard's way out must land green: a user who cannot grant FDA
    right now removes the agent (re-added any time) rather than keeping
    a nightly failure the doctor reddens forever."""
    fts._plist_path().write_text("<plist/>")
    _write_agent_log("PermissionError: Operation not permitted\n")
    monkeypatch.setattr(plan, "_launchctl",
                        SeqLaunchctl([DUMP(1, 1), DUMP(2, 1)]))
    monkeypatch.setattr(lifecycle, "_open_in_macos", lambda *a, **k: None)
    removed = []
    monkeypatch.setattr(
        fts, "uninstall_launchd",
        lambda: (removed.append(True), f"removed {fts.LAUNCHD_LABEL}")[1])
    _script(monkeypatch, ["skip"])
    lifecycle._fts_agent_aftercare()
    assert removed == [True]
    assert "removed" in capsys.readouterr().out


def test_aftercare_names_the_rebuild_for_a_damaged_index(
    home, monkeypatch, capsys, no_sleep,
):
    """A damaged index is not a permissions problem: the FDA walk must
    never be prescribed for it (it used to loop such a machine through
    System Settings until the only green exit uninstalled a healthy
    agent). The log names the cause; the remedy is the rebuild, and the
    agent stays."""
    fts._plist_path().write_text("<plist/>")
    _write_agent_log("sqlite3.DatabaseError: database disk image is "
                     "malformed\n")
    monkeypatch.setattr(plan, "_launchctl",
                        SeqLaunchctl([DUMP(1, 1), DUMP(2, 1)]))
    opened = []
    monkeypatch.setattr(
        lifecycle, "_open_in_macos",
        lambda target, reveal=False: opened.append(target))
    _no_input(monkeypatch)                     # asks NOTHING
    removed = []
    monkeypatch.setattr(fts, "uninstall_launchd",
                        lambda: removed.append(True) or "removed")
    lifecycle._fts_agent_aftercare()
    out = capsys.readouterr().out
    assert "--rebuild" in out
    assert "Full Disk Access" not in out
    assert opened == [] and removed == []


def test_aftercare_unknown_failure_names_the_log_and_keeps_the_agent(
    home, monkeypatch, capsys, no_sleep,
):
    """No marker in the log: aftercare refuses to guess — it names the
    log, keeps the agent, and leaves the flagging to doctor (which now
    sees unloaded and failing agents alike)."""
    fts._plist_path().write_text("<plist/>")
    monkeypatch.setattr(plan, "_launchctl",
                        SeqLaunchctl([DUMP(1, 1), DUMP(2, 1)]))
    opened = []
    monkeypatch.setattr(
        lifecycle, "_open_in_macos",
        lambda target, reveal=False: opened.append(target))
    _no_input(monkeypatch)
    lifecycle._fts_agent_aftercare()
    out = capsys.readouterr().out
    assert str(fts._log_path()) in out
    assert "Full Disk Access" not in out
    assert opened == []


def test_setup_reconfigure_is_backup_first(home, smoke, monkeypatch):
    root = state.State.resolve().adopt().root
    old = 'default = "old"\n\n[old]\nfrom_addr = "old@example.org"\ndriver = "pipe"\ncommand = "cat"\n'
    (root / "identities.toml").write_text(old)
    _script(monkeypatch, [
        "y",                      # reconfigure? (current kept as .bak)
        "paris@example.org",      # From: address
        "",                       # display name
        "pipe",                   # driver
        "sendmail -t",            # delivery command
        "n",                      # Microsoft/Exchange mailbox? -> no
        "n", "n", "n",            # agents + fts build
    ])
    assert lifecycle.setup() == 0
    assert (root / "identities.toml.bak").read_text() == old
    assert 'command = "sendmail -t"' in (root / "identities.toml").read_text()


def test_setup_keep_existing_identities(home, smoke, monkeypatch, capsys):
    root = state.State.resolve().adopt().root
    old = 'default = "o"\n\n[o]\nfrom_addr = "o@x.org"\ndriver = "pipe"\ncommand = "cat"\n'
    (root / "identities.toml").write_text(old)
    # keep + not-a-Microsoft-mailbox + no agents + no fts
    _script(monkeypatch, ["n", "n", "n", "n", "n"])
    assert lifecycle.setup() == 0
    assert (root / "identities.toml").read_text() == old
    assert "existing identities kept" in capsys.readouterr().out


def test_setup_kept_identities_can_still_gain_the_drafts_lane(
    home, smoke, monkeypatch, capsys,
):
    """The Camilla path: a returning user answers "n" to reconfigure
    (their working transport must survive untouched) and STILL gets the
    Exchange question — the missing lines are inserted, nothing else
    moves, and the old file is the .bak."""
    root = state.State.resolve().adopt().root
    old = textwrap.dedent("""\
        default = "main"

        [main]
        from_addr = "camilla@example.org"
        driver = "ssh_sendmail"
        host = "lxplus.example.org"
        user = "cv"
        socket = "~/.ssh/s"
    """)
    (root / "identities.toml").write_text(old)
    logins = []
    monkeypatch.setattr("email_mcp.graph.device_login",
                        lambda ident: logins.append(ident.from_addr))
    # keep + IS a Microsoft mailbox + enable now + no agents + no fts
    _script(monkeypatch, ["n", "y", "y", "n", "n", "n"])
    assert lifecycle.setup() == 0
    assert logins == ["camilla@example.org"]     # signed in during setup
    text = (root / "identities.toml").read_text()
    assert 'drafts   = "graph"' in text
    assert 'executor = "graph"' in text
    assert '[main.graph]' in text
    assert 'host = "lxplus.example.org"' in text  # transport untouched
    assert (root / "identities.toml.bak").read_text() == old
    from email_mcp import identities
    idents, default = identities.load()
    assert idents[default].drafts == "graph"     # parses and declares


def test_setup_refused_root_exits_nonzero(home, monkeypatch, tmp_path,
                                          capsys):
    monkeypatch.setenv("EMAIL_MCP_FTS_DIR", str(tmp_path / "old"))
    assert lifecycle.setup(yes=True) == 1
    assert "cannot set up" in capsys.readouterr().out
    assert not (home / ".email-mcp").exists()


def test_purge_plan_names_an_unlistable_logs_dir(home, installed):
    """A plan that cannot enumerate must say so: Path.glob swallows
    PermissionError, so an unreadable ~/Library/Logs used to yield a
    preview with no log rows and no note — silent under-description."""
    logs = home / "Library" / "Logs"
    logs.chmod(0o000)
    try:
        rows = lifecycle.plan_uninstall(purge=True)
    finally:
        logs.chmod(0o755)
    notes = [r for r in rows if isinstance(r, plan.PrintOnly)
             and "cannot list" in r.text]
    assert notes and str(logs) in notes[0].text
    assert not any(isinstance(r, plan.UnlinkFile)
                   and str(logs) in str(r.target()) for r in rows)


def test_uninstall_plan_names_an_unlistable_graph_dir(home, installed):
    """The logs row's rule, applied to the token sweep: a graph/ the plan
    cannot enumerate must say so — an unreadable directory used to read
    as 'no token caches' while the caches sat inside it."""
    graph = state.State.resolve().reader().graph
    graph.chmod(0o000)
    try:
        rows = lifecycle.plan_uninstall(purge=False)
    finally:
        graph.chmod(0o700)
    notes = [r for r in rows if isinstance(r, plan.PrintOnly)
             and "cannot list" in r.text]
    assert notes and str(graph) in notes[0].text
    assert "token caches not removed" in notes[0].text
    assert not any(isinstance(r, plan.UnlinkFile)
                   and str(r.target()).endswith(".token.json")
                   for r in rows)


def test_setup_survives_a_failing_fts_build(home, smoke, monkeypatch, capsys):
    """The first-user crash (2026-08-01): 'y' to the optional FTS build
    with no Full Disk Access killed setup mid-run — no client config, no
    smoke test, no diagnosis, launchd agents left half-configured. The
    build's failure is a finding; the tail that names the real problem
    must always run."""
    _script(monkeypatch, [
        "",     # From: address -> skip identity
        "n",    # dispatcher agent
        "n",    # fts agent
        "y",    # fts build -> boom
    ])
    def _boom(self):
        raise PermissionError(1, "Operation not permitted",
                              str(home / "Library" / "Mail"))
    monkeypatch.setattr("email_mcp.fts.FtsIndex.build", _boom)
    assert lifecycle.setup() == 0
    out = capsys.readouterr().out
    assert "fts build FAILED" in out
    assert "python -m email_mcp.fts --build" in out
    assert '"apple-mail"' in out          # client config still printed
    assert "42 messages" in out           # the smoke test still ran
