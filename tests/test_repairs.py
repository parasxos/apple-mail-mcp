"""doctor --fix repair registry (email_mcp.repairs) — v0.11 L1.

Every test runs inside a fake $HOME so the DEFAULT (~/.email-mcp) path
resolution is exercised end-to-end while the real state tree stays
untouchable. The conftest fts/audit guards are shadowed by name for the
same reason test_audit.py shadows them: these tests need the real
env-driven resolvers, pointed into the fake HOME. A PATH-shimmed fake
launchctl (with per-label load state) fences every test from the real
launchd; a no-op osascript rides along so no repair can ever surface a
real macOS notification.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import textwrap
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest

from email_mcp import audit, dispatcher, ids, repairs, spool

REPAIR_IDS = [
    "state_dir_missing", "state_dir_perms", "state_file_perms",
    # meta_missing (v0.11): adoption via `doctor --fix` or the documented
    # migration wrote the ownership marker but never a meta.json, leaving
    # every UPGRADED user unstamped — the population a future
    # state_version migration has to identify.
    "meta_missing",
    "audit_path_is_file", "legacy_launchd", "launchd_stale",
    "stranded_sending", "stale_plan_claims",
]

SPOOL_STATES = ("pending", "sending", "sent", "failed", "cancelled")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake $HOME with every EMAIL_MCP_* override wiped: repairs must
    heal the default tree, and nothing may reach the real ~/.email-mcp."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    yield h
    audit.set_process("server")  # run_fixes tags "cli"; restore for the suite

@pytest.fixture(autouse=True)
def state_root_guard(home, monkeypatch):
    """Shadow conftest's root guard by name: conftest PATCHES
    config.state_root, which would override this module's fake HOME. These
    tests need the real env-driven resolution inside that HOME."""
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    return home / ".email-mcp"

@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name, on purpose): the env pin would
    point the FTS dir OUTSIDE the fake HOME and break the default-path
    resolution these tests exercise. The fake HOME is the isolation."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason as fts_dir_guard): the audit
    resolver must be the real env-driven one, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def launchctl_shim(home, tmp_path, monkeypatch):
    """PATH-shimmed fake launchctl: logs every invocation and keeps
    per-label load state (bootstrap loads, bootout unloads, print answers
    0 iff loaded). Returns the invocation log path."""
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


def _applied_ids(result: dict) -> list[str]:
    return [a["repair"] for a in result["applied"]]


def _tree_dirs(home: Path) -> list[Path]:
    root = home / ".email-mcp"
    return [root, root / "spool",
            *(root / "spool" / s for s in SPOOL_STATES),
            root / "plans", root / "audit", root / "graph"]


def _make_tree(home: Path) -> Path:
    """A healthy default state tree, all dirs 0700."""
    for d in _tree_dirs(home):
        d.mkdir(exist_ok=True)
        d.chmod(0o700)
    return home / ".email-mcp"


def _spell_home(home: Path, alias: str) -> str:
    """$HOME written three ways. Only "plain" is what a value compare on
    unresolved paths recognises: pathlib preserves a ".." segment and a
    leading "//" verbatim, so the other two name the same directory while
    reading as somewhere else entirely."""
    if alias == "dotdot":
        return f"{home}/sub/.."
    if alias == "double-slash":
        return f"/{home}"
    return str(home)


HOME_ALIASES = ("plain", "dotdot", "double-slash")


def _plant_stranded(home: Path, minutes_ago: int = 30) -> str:
    """A claim abandoned in sending/ well past STALE_SENDING_MINUTES."""
    sid = "20260101T000000Z-deadbeef0001"
    old = ids.iso(ids.utcnow() - timedelta(minutes=minutes_ago))
    entry = spool.Entry(
        id=sid, send_at=old, created_at=old, to=["paris@example.org"],
        cc=[], bcc=[], subject="stranded", attachments=[],
        message_id="<stranded@example.org>", status="sending",
        attempts=0, next_attempt_at=old)
    d = home / ".email-mcp" / "spool" / "sending"
    (d / f"{sid}.json").write_bytes(
        json.dumps(asdict(entry), indent=2).encode())
    (d / f"{sid}.eml").write_bytes(b"From: x@example.org\r\n\r\nbody\r\n")
    return sid


# --------------------------------------------------------------------- #
# the frozen whitelist                                                   #
# --------------------------------------------------------------------- #


def test_registry_is_exactly_the_frozen_whitelist():
    """The registry ids are pinned: growing (or renaming) the whitelist
    is a deliberate act that must show up as a diff HERE."""
    assert [r.id for r in repairs.REPAIRS] == REPAIR_IDS
    with pytest.raises(dataclasses.FrozenInstanceError):
        repairs.REPAIRS[0].id = "mutated"
    # The dispatcher seams repairs rides on are true aliases.
    assert dispatcher.recover_stranded is dispatcher._recover_stranded
    assert dispatcher.remove_legacy_plists is dispatcher._remove_legacy_plists


# --------------------------------------------------------------------- #
# state tree: mkdir 0700, never-create-fts, chmod dirs/files             #
# --------------------------------------------------------------------- #


def test_state_dir_missing_creates_tree_0700(home):
    result = repairs.run_fixes()
    for d in _tree_dirs(home):
        assert d.is_dir(), d
        assert (d.stat().st_mode & 0o777) == 0o700, d
    assert "state_dir_missing" in _applied_ids(result)
    assert result["failed"] == []


def test_run_fixes_never_creates_fts_dir(home):
    """The FTS index is derived state: doctor --fix must never create
    even its directory — --build is the only builder."""
    repairs.run_fixes()
    assert not (home / ".email-mcp" / "fts").exists()


def test_state_dir_perms_chmod_0700(home):
    root = _make_tree(home)
    (root / "spool").chmod(0o755)
    (root / "plans").chmod(0o777)
    result = repairs.run_fixes()
    assert (root / "spool").stat().st_mode & 0o777 == 0o700
    assert (root / "plans").stat().st_mode & 0o777 == 0o700
    assert "state_dir_perms" in _applied_ids(result)
    assert "state_dir_missing" not in _applied_ids(result)


def test_state_file_perms_chmod_0600(home):
    root = _make_tree(home)
    ident = root / "identities.toml"
    ident.write_text("[identities.work]\n")
    ident.chmod(0o644)
    ledger = root / "audit" / "2026-01.jsonl"
    ledger.write_text('{"v":1}\n')
    ledger.chmod(0o644)
    token = root / "graph" / "work.token.json"
    token.write_text("{}")
    token.chmod(0o666)
    result = repairs.run_fixes()
    for f in (ident, ledger, token):
        assert (f.stat().st_mode & 0o777) == 0o600, f
    assert "state_file_perms" in _applied_ids(result)


@pytest.mark.parametrize("alias", HOME_ALIASES)
def test_degenerate_override_at_home_however_spelled_is_dropped(
    home, monkeypatch, alias,
):
    """A state root pointed at $HOME is never managed here, and the three
    spellings of $HOME are one fence: mkdir would scatter spool
    subdirectories through the home directory and chmod 0700 would
    retighten the home directory itself.

    Since v0.11 there is one variable to point (the five per-directory
    ones are rejected outright), and the refusal is absolute: with the
    root refused NOTHING is managed — including ~/.email-mcp, which is not
    the configured root in this scenario. `doctor` reports the refusal;
    it is not silently worked around.
    """
    home.chmod(0o755)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", _spell_home(home, alias))

    result = repairs.run_fixes()
    assert result["failed"] == []
    assert (home.stat().st_mode & 0o777) == 0o755  # $HOME not retightened
    assert not any((home / s).exists() for s in SPOOL_STATES)
    assert repairs._state_dirs() == []             # nothing is managed
    assert not (home / ".email-mcp").exists()      # nor is the default tree
    # …and the doctor says why, instead of going green over it.
    from email_mcp import doctor
    check = doctor.check_state_root()
    assert check["ok"] is False and "home directory" in check["detail"]


@pytest.mark.parametrize("var", ("EMAIL_MCP_SPOOL_DIR", "EMAIL_MCP_PLANS_DIR",
                                 "EMAIL_MCP_GRAPH_DIR", "EMAIL_MCP_FTS_DIR",
                                 "EMAIL_MCP_AUDIT_DIR"))
def test_retired_per_directory_override_is_rejected_not_ignored(
    home, monkeypatch, var, tmp_path,
):
    """Migration policy: the five retired variables are REJECTED.

    Ignoring one silently relocates live state — a user with
    EMAIL_MCP_SPOOL_DIR=/Volumes/big/spool would find scheduled mail
    apparently gone (still spooled in the old directory, with nothing
    delivering it). Refusing costs one legible error.
    """
    from email_mcp import audit, config, doctor

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv(var, str(elsewhere))

    reason = config.state_root_refusal()
    assert reason is not None and var in reason and "retired" in reason
    with pytest.raises(config.StateDirRefused, match="retired"):
        config.state_root()

    check = doctor.check_state_root()
    assert check["ok"] is False and var in check["fix"]
    # Fail closed everywhere, and never by crashing a mutation.
    assert audit.emit("send", outcome="sent") is None
    assert not (home / ".email-mcp").exists()
    assert list(elsewhere.iterdir()) == []


# --------------------------------------------------------------------- #
# audit path squatted by a file                                          #
# --------------------------------------------------------------------- #


def test_audit_path_is_file_renamed_aside(home):
    root = home / ".email-mcp"
    root.mkdir()
    root.chmod(0o700)
    squatter = root / "audit"
    squatter.write_text('{"v":1}\n')
    result = repairs.run_fixes()
    aside = [p for p in root.glob("audit.bak-*")]
    assert len(aside) == 1
    assert re.fullmatch(r"audit\.bak-\d{8}T\d{6}Z(-\d+)?", aside[0].name)
    assert aside[0].read_text() == '{"v":1}\n'  # renamed, never deleted
    assert "audit_path_is_file" in _applied_ids(result)
    assert result["failed"] == []
    # The repair's own doctor_fix emit recreated the ledger dir and is on
    # record in it.
    assert (root / "audit").is_dir()
    events = audit.query(event="doctor_fix")["events"]
    assert any(e["detail"]["repair"] == "audit_path_is_file"
               and e["outcome"] == "fixed" for e in events)


# --------------------------------------------------------------------- #
# launchd: rebootstrap only what exists; legacy bootout                  #
# --------------------------------------------------------------------- #


def test_launchd_rebootstrap_only_when_plist_exists(home, launchctl_shim):
    plist = (home / "Library" / "LaunchAgents"
             / f"{dispatcher.LAUNCHD_LABEL}.plist")
    # Never installed: the repair must NOT install (or bootstrap) anything.
    result = repairs.run_fixes()
    assert not plist.exists()
    assert "launchd_stale" not in _applied_ids(result)
    assert not any(c.startswith(("bootstrap", "bootout"))
                   for c in _calls(launchctl_shim))
    # Installed but stale: re-render + bootout + bootstrap.
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist>stale render</plist>")
    result = repairs.run_fixes()
    assert "launchd_stale" in _applied_ids(result)
    assert plist.read_text() == dispatcher._plist_content()
    calls = _calls(launchctl_shim)
    assert any(c.startswith("bootstrap") for c in calls)
    assert any(c.startswith("bootout") for c in calls)


def test_legacy_bootout_via_path_shimmed_launchctl(home, launchctl_shim):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.paris.email-mcp-dispatcher.plist"
    legacy.write_text("<plist/>")
    result = repairs.run_fixes()
    assert "legacy_launchd" in _applied_ids(result)
    assert not legacy.exists()
    assert (f"bootout gui/{os.getuid()}/com.paris.email-mcp-dispatcher"
            in _calls(launchctl_shim))
    # Migration never installs the CURRENT agent as a side effect.
    assert not (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").exists()


# --------------------------------------------------------------------- #
# stranded claims + stale plan claims                                    #
# --------------------------------------------------------------------- #


def test_stranded_recovery_goes_through_the_public_alias(home, monkeypatch):
    _make_tree(home)
    sid = _plant_stranded(home)
    calls: list = []
    real = dispatcher.recover_stranded
    monkeypatch.setattr(dispatcher, "recover_stranded",
                        lambda now: calls.append(now) or real(now))
    result = repairs.run_fixes()
    assert calls, "repair must ride dispatcher.recover_stranded"
    assert "stranded_sending" in _applied_ids(result)
    moved = home / ".email-mcp" / "spool" / "pending" / f"{sid}.json"
    assert moved.exists()  # requeued, attempt consumed
    assert json.loads(moved.read_text())["attempts"] == 1
    assert not (home / ".email-mcp" / "spool" / "sending"
                / f"{sid}.json").exists()


def test_stale_plan_claims_finalised_with_plan_finish(home):
    _make_tree(home)
    plan = {
        "id": "p1", "created_at": ids.iso(ids.utcnow()),
        "expires_at": ids.iso(ids.utcnow()), "status": "draft",
        "query": {}, "actions": [], "target": None, "messages": [],
        "summary": "2 messages: mark_read", "result": None,
    }
    claim = home / ".email-mcp" / "plans" / "p1.json.applying"
    claim.write_text(json.dumps(plan))
    old = time.time() - (2 * 600 + 300)  # past 2 x TTL, inside 7-day GC
    os.utime(claim, (old, old))
    result = repairs.run_fixes()
    assert "stale_plan_claims" in _applied_ids(result)
    assert not claim.exists()
    final = home / ".email-mcp" / "plans" / "p1.json"
    assert json.loads(final.read_text())["status"] == "failed"
    events = audit.query(event="plan_finish")["events"]
    assert events and events[0]["outcome"] == "failed"
    assert events[0]["plan_id"] == "p1"


# --------------------------------------------------------------------- #
# idempotency, containment, dry run                                      #
# --------------------------------------------------------------------- #


def test_second_run_is_a_noop(home, launchctl_shim):
    root = _make_tree(home)
    (root / "spool").chmod(0o755)
    ident = root / "identities.toml"
    ident.write_text("[identities.work]\n")
    ident.chmod(0o644)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").write_text("stale")
    _plant_stranded(home)

    first = repairs.run_fixes()
    assert first["applied"] and not first["failed"]

    second = repairs.run_fixes()
    assert second["applied"] == []
    assert second["failed"] == []
    assert [s["repair"] for s in second["skipped"]] == REPAIR_IDS
    assert all(s["reason"] == "healthy" for s in second["skipped"])


def test_apply_failure_is_reported_never_raised(home, monkeypatch):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.paris.email-mcp-dispatcher.plist").write_text("<plist/>")
    (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").write_text("stale")

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatcher, "remove_legacy_plists", boom)
    result = repairs.run_fixes()  # must not raise
    failures = [f for f in result["failed"] if f["repair"] == "legacy_launchd"]
    assert failures and "boom" in failures[0]["error"]
    assert failures[0]["finding"]
    # The pass kept going: the LATER launchd_stale repair still applied.
    assert "launchd_stale" in _applied_ids(result)
    # And the failure is on the ledger, threaded under the run's op.
    events = audit.query(event="doctor_fix")["events"]
    failed_events = [e for e in events if e["outcome"] == "failed"]
    assert any(e["detail"]["repair"] == "legacy_launchd"
               and e["op"] == result["op"] for e in failed_events)


def test_dry_run_touches_nothing(home, launchctl_shim):
    root = home / ".email-mcp"
    root.mkdir()
    root.chmod(0o755)                       # wrong dir mode
    (root / "audit").write_text("squat")    # file on the ledger path
    ident = root / "identities.toml"
    ident.write_text("[identities.work]\n")
    ident.chmod(0o644)                      # wrong file mode
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.paris.email-mcp-dispatcher.plist"
    legacy.write_text("<plist/>")
    plist = agents / f"{dispatcher.LAUNCHD_LABEL}.plist"
    plist.write_text("stale")

    result = repairs.run_fixes(dry_run=True)

    assert result["applied"] == []
    assert result["failed"] == []
    dry = {s["repair"] for s in result["skipped"]
           if s["reason"].startswith("dry-run")}
    assert dry == {"state_dir_missing", "state_dir_perms",
                   "state_file_perms", "audit_path_is_file",
                   "legacy_launchd", "launchd_stale"}
    healthy = {s["repair"] for s in result["skipped"]
               if s["reason"] == "healthy"}
    assert healthy == {"stranded_sending", "stale_plan_claims",
                       # unmarked root in this fixture: not ours to stamp
                       "meta_missing"}
    # Every finding is attached, nothing was touched:
    assert all(s["finding"] for s in result["skipped"]
               if s["repair"] in dry)
    assert (root.stat().st_mode & 0o777) == 0o755
    assert not (root / "spool").exists()
    assert (root / "audit").is_file()
    assert sorted(p.name for p in root.iterdir()) == [
        "audit", "identities.toml"]        # no .bak, no ledger dir, no events
    assert (ident.stat().st_mode & 0o777) == 0o644
    assert legacy.exists()
    assert plist.read_text() == "stale"
    # detect may only PROBE launchd (print); never bootout/bootstrap.
    assert all(c.startswith("print") for c in _calls(launchctl_shim))


def test_fix_pass_never_writes_the_ledger_through_a_symlinked_audit_dir(
    home, launchctl_shim, tmp_path
):
    """The repair registry refuses to chmod through a symlinked state dir —
    and the doctor_fix events RECORDING that refusal must not defeat it.
    emit's own mkdir/chmod/open would otherwise follow the same link."""
    victim = tmp_path / "victim"
    victim.mkdir()
    keep = victim / "keep.txt"
    keep.write_text("victim content")
    victim.chmod(0o755)
    root = home / ".email-mcp"
    root.mkdir()
    (root / "audit").symlink_to(victim)

    result = repairs.run_fixes()

    assert (victim.stat().st_mode & 0o777) == 0o755   # never chmodded
    assert sorted(p.name for p in victim.iterdir()) == ["keep.txt"]
    assert keep.read_text() == "victim content"
    assert (root / "audit").is_symlink()
    # The pass still ran and still flagged the link, without a single event.
    assert "state_dir_perms" in {f["repair"] for f in result["failed"]}
    assert audit.query()["events"] == []
