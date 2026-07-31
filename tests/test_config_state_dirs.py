"""The state-root model (email_mcp.config).

v0.11 replaced six independent EMAIL_MCP_*_DIR overrides with ONE
EMAIL_MCP_STATE_DIR. Three release gates in a row had found a fresh way
past the per-directory fences — a getter nobody had fenced, a fence that
ran after the mkdir it was guarding, a path comparison that missed every
spelling of $HOME on a case-insensitive volume. The configuration surface
was itself the defect, so it was removed rather than fenced harder.

Two rules carry the whole security story now:

1. **Only what we create is ours to mode.** A directory this tool creates
   is 0700; a directory that already existed is never chmodded. "email-mcp
   changed the mode of a directory I did not name" is unrepresentable, not
   guarded.
2. **A root must be ours.** An override naming an existing directory that
   holds someone else's files and carries no marker is refused — which
   makes $HOME, /Users and every case-variant and firmlink of them
   refusable without comparing a single path string.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from email_mcp import config


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture(autouse=True)
def state_root_guard(monkeypatch, tmp_path):
    """Shadow conftest's guard by name: these tests exercise the REAL
    env-driven resolution inside a fake HOME, so neither the env pin nor
    the patched resolver may be in force."""
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("HOME", str(home))
    return home / ".email-mcp"


@pytest.fixture
def home(state_root_guard) -> Path:
    return state_root_guard.parent


LEAF_GETTERS = ("spool_dir", "plans_dir", "graph_dir", "fts_dir", "audit_dir")


# --------------------------------------------------------------------- #
# rule 1 — only what we create is ours to mode                           #
# --------------------------------------------------------------------- #


def test_root_we_create_is_0700_and_marked(home):
    root = config.state_root()

    assert _mode(root) == 0o700
    assert (root / config.STATE_MARKER).is_file()
    assert _mode(root / config.STATE_MARKER) == 0o600


def test_pre_existing_root_keeps_its_own_mode(home):
    """The rule that makes the whole class go away: we did not create it,
    so its mode is not ours to change. doctor still REPORTS a loose mode
    and `doctor --fix` still repairs it — on request, not by surprise."""
    (home / ".email-mcp").mkdir(mode=0o755)

    root = config.state_root()

    assert _mode(root) == 0o755


def test_pre_existing_leaf_keeps_its_own_mode(home):
    (home / ".email-mcp" / "spool").mkdir(parents=True, mode=0o755)

    config.spool_dir()

    assert _mode(home / ".email-mcp" / "spool") == 0o755


@pytest.mark.parametrize("getter", LEAF_GETTERS)
def test_leaves_we_create_are_0700(home, getter):
    d = getattr(config, getter)()

    assert d.parent == home / ".email-mcp"
    assert _mode(d) == 0o700


def test_spool_subdirectories_are_0700(home):
    d = config.spool_dir()

    for sub in ("pending", "sending", "sent", "failed", "cancelled"):
        assert _mode(d / sub) == 0o700, sub


# --------------------------------------------------------------------- #
# rule 2 — a root must be ours                                           #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("alias", ("plain", "case", "dotdot", "double-slash",
                                   "parent", "grandparent"))
def test_home_or_above_is_refused_however_spelled(home, monkeypatch, alias):
    """macOS ships case-insensitive APFS and firmlinks /Users, so $HOME has
    many spellings and `Path.resolve()` returns whichever it was handed.
    Identity is the inode; `..` is collapsed textually BEFORE validating so
    the path checked is the path used."""
    (home / "mine.txt").write_text("irreplaceable")
    spellings = {
        "plain": home,
        "case": home.parent / home.name.upper(),
        "dotdot": Path(f"{home}/sub/.."),
        "double-slash": Path("/" + str(home)),
        "parent": home.parent,
        "grandparent": home.parent.parent,
    }
    target = spellings[alias]
    if alias == "case" and not (
            target.exists() and target.stat().st_ino == home.stat().st_ino):
        pytest.skip("case-sensitive filesystem: the variant is a real path")

    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(target))
    before = _mode(home)

    with pytest.raises(config.StateDirRefused):
        config.state_root()

    assert _mode(home) == before, "changed the mode of a refused directory"
    assert not (home / "sub").exists(), "created a path before refusing"
    assert not (home / config.STATE_MARKER).exists(), "marked a refused root"
    assert (home / "mine.txt").read_text() == "irreplaceable"


def test_non_empty_unmarked_directory_is_refused(home, monkeypatch, tmp_path):
    """Pointing the root at a directory that already holds someone's files
    is the general case of the $HOME attack — and it is caught without any
    path comparison at all."""
    docs = tmp_path / "Documents"
    docs.mkdir(mode=0o755)
    (docs / "thesis.txt").write_text("irreplaceable")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(docs))

    with pytest.raises(config.StateDirRefused):
        config.state_root()

    assert _mode(docs) == 0o755
    assert (docs / "thesis.txt").read_text() == "irreplaceable"
    assert list(docs.iterdir()) == [docs / "thesis.txt"]


def test_empty_directory_is_accepted_as_a_relocation_target(
        home, monkeypatch, tmp_path):
    """Relocation onto another volume stays supported — that is why the
    rule is 'must be ours', not 'must be under $HOME'."""
    vol = tmp_path / "Volumes" / "ext" / "email-mcp"
    vol.mkdir(parents=True)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(vol))

    root = config.state_root()

    assert root == vol
    assert (vol / config.STATE_MARKER).is_file()


def test_a_marked_root_reopens_without_complaint(home, monkeypatch, tmp_path):
    vol = tmp_path / "ext"
    vol.mkdir()
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(vol))
    config.state_root()
    config.spool_dir()

    assert config.state_root() == vol  # non-empty now, but marked


# --------------------------------------------------------------------- #
# symlinks: a chosen root may be one, a managed leaf may not             #
# --------------------------------------------------------------------- #


def test_symlinked_root_is_followed_and_its_target_keeps_its_mode(
        home, tmp_path):
    """Relocating the state root with a link is a supported shape. The rule
    is 'never change modes behind a link', not 'never follow one'."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    (home / ".email-mcp").symlink_to(target)

    config.state_root()

    assert _mode(target) == 0o755


@pytest.mark.parametrize("getter", LEAF_GETTERS)
def test_symlinked_leaf_is_refused_and_its_victim_untouched(
        home, tmp_path, getter):
    """A link on a leaf WE own is a squat: mkdir and chmod resolve through
    it, so the ledger or the spool would be written into the target."""
    leaf = {"spool_dir": "spool", "plans_dir": "plans", "graph_dir": "graph",
            "fts_dir": "fts", "audit_dir": "audit"}[getter]
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    (victim / "keep.txt").write_text("theirs")
    (home / ".email-mcp").mkdir()
    (home / ".email-mcp" / leaf).symlink_to(victim)

    with pytest.raises(config.StateDirRefused):
        getattr(config, getter)()

    assert _mode(victim) == 0o755
    assert (victim / "keep.txt").read_text() == "theirs"
    assert list(victim.iterdir()) == [victim / "keep.txt"]


def test_emit_drops_the_event_when_the_ledger_is_refused(home, tmp_path):
    """A refused ledger costs receipts, never a mutation — audit.emit must
    return None rather than raise into the send that called it."""
    from email_mcp import audit

    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    (home / ".email-mcp").mkdir()
    (home / ".email-mcp" / "audit").symlink_to(victim)

    assert audit.emit("send", outcome="sent", operation_id="x") is None
    assert not list(victim.iterdir())


# --------------------------------------------------------------------- #
# read-side purity                                                       #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("getter", ("spool_dir", "plans_dir", "fts_dir",
                                    "audit_dir"))
def test_create_false_resolves_without_touching_the_disk(home, getter):
    """A plain `doctor` built the spool tree wherever the override pointed
    — including inside ~/Library/Mail, contradicting the guarantee the
    security posture states most absolutely."""
    d = getattr(config, getter)(create=False)

    assert d.parent == home / ".email-mcp"
    assert not (home / ".email-mcp").exists()


# --------------------------------------------------------------------- #
# v0.11 gate-4 regressions — each of these is a defect this gate found   #
# --------------------------------------------------------------------- #


def test_marker_alone_does_not_make_a_root_look_foreign(
        home, monkeypatch, tmp_path):
    """The ownership marker is OURS. Counting it as "someone else's files"
    made a root this tool had just adopted refusable.

    Found as a two-process flake: the marker is written between the
    _marked() probe and the directory scan, so the scan saw exactly one
    entry — our own marker — and refused.
    """
    vol = tmp_path / "ext"
    vol.mkdir()
    (vol / config.STATE_MARKER).write_text("")   # marker only, and EMPTY
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(vol))

    assert config.state_root_refusal() is None
    assert config.state_root() == vol


def test_root_adopted_between_probe_and_scan_is_not_refused(
        home, monkeypatch, tmp_path):
    """The same race, forced deterministically: the marker appears after
    the _marked() probe and before the scan. The refusal must be based on
    the marker as it stands AFTER the scan — server and the launchd
    dispatcher genuinely race here on the first mutation.
    """
    vol = tmp_path / "ext"
    vol.mkdir()
    (vol / "spool").mkdir()          # the other process's first leaf
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(vol))

    real_iterdir = Path.iterdir

    def adopting_iterdir(self):
        # Stand in for the other writer finishing its adoption inside the
        # window between the marker probe and this scan.
        if self == vol and not (vol / config.STATE_MARKER).exists():
            (vol / config.STATE_MARKER).write_text("{}\n")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", adopting_iterdir)

    assert config.state_root_refusal() is None
    assert config.state_root() == vol


def test_refusal_still_fires_for_genuinely_foreign_content(
        home, monkeypatch, tmp_path):
    """The race fix must not have opened the hole it was narrowing: a
    directory holding a file that is NOT our marker is still refused."""
    docs = tmp_path / "Documents"
    docs.mkdir(mode=0o755)
    (docs / "thesis.txt").write_text("irreplaceable")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(docs))

    assert config.state_root_refusal() is not None
    with pytest.raises(config.StateDirRefused):
        config.state_root()
    assert (docs / "thesis.txt").read_text() == "irreplaceable"


def test_create_false_never_yields_a_root_the_write_path_would_refuse(
        home, monkeypatch):
    """`state_root(create=False)` short-circuited BEFORE validating, so it
    handed back $HOME — and `doctor --fix` built $HOME/spool out of it.
    The path stays resolvable (uninstall and doctor must be able to NAME
    it), but the refusal is now askable without triggering the write."""
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(home))

    assert config.state_root(create=False) == home   # still total
    assert config.state_root_refusal() is not None   # and still refused
    with pytest.raises(config.StateDirRefused):
        config.state_root()


def test_doctor_fix_never_creates_state_under_a_refused_root(
        home, monkeypatch):
    """The defect that regression above describes, at the layer it bit:
    `doctor --fix` resolved <root>/spool from an unvalidated root and
    mkdir'd it. Nothing may be created under a refused root."""
    from email_mcp import repairs

    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(home))
    before = sorted(p.name for p in home.iterdir())
    home_mode = _mode(home)

    result = repairs.run_fixes()

    assert sorted(p.name for p in home.iterdir()) == before
    assert _mode(home) == home_mode
    for leaf in ("spool", "plans", "graph", "audit", "fts"):
        assert not (home / leaf).exists()
    assert isinstance(result["failed"], list)   # reported, never raised


def test_a_root_that_is_a_file_is_refused_before_any_effect(
        home, monkeypatch, tmp_path):
    """A non-directory squatting on the root: refused with a readable
    reason instead of a NotADirectoryError from somewhere deeper."""
    imposter = tmp_path / "not-a-dir"
    imposter.write_text("I am a file")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(imposter))

    assert "not a directory" in (config.state_root_refusal() or "")
    with pytest.raises(config.StateDirRefused):
        config.state_root()
    assert imposter.read_text() == "I am a file"


def test_setup_never_chmods_pre_existing_spool_subdirectories(
        home, monkeypatch, capsys):
    """`setup` used to chmod 0700 over the five spool state subdirectories
    unconditionally, which re-introduced the surprise-chmod the created-only
    rule exists to remove. Configuration never re-modes what it did not
    create — `doctor` reports it, `doctor --fix` repairs it."""
    from email_mcp import lifecycle

    (home / ".email-mcp" / "spool" / "pending").mkdir(parents=True,
                                                      mode=0o755)
    monkeypatch.setattr(lifecycle, "read_meta", lambda: {})
    monkeypatch.setattr(lifecycle, "write_meta", lambda meta: None)

    result = lifecycle._step_state_dirs({})

    assert result.status == "ok"
    assert _mode(home / ".email-mcp" / "spool" / "pending") == 0o755
    # The ones it DID create are still 0700.
    assert _mode(home / ".email-mcp" / "spool" / "sending") == 0o700


def test_doctor_fix_repairs_a_pre_existing_mode_on_request(home):
    """The other half of the contract: what configuration refuses to do
    silently, `doctor --fix` does explicitly."""
    from email_mcp import repairs

    (home / ".email-mcp").mkdir(mode=0o755)
    config.spool_dir()
    assert _mode(home / ".email-mcp") == 0o755   # untouched by config

    finding = repairs._detect_state_dir_perms()
    assert finding and "not 0700" in finding
    result = repairs.run_fixes()

    assert "state_dir_perms" in [a["repair"] for a in result["applied"]]
    assert _mode(home / ".email-mcp") == 0o700


def test_doctor_is_read_only_over_a_completely_absent_state_tree(home):
    """Read-side purity, end to end: a full doctor run over a fresh HOME
    creates nothing at all."""
    from email_mcp import doctor

    doctor.check_spool_plans()
    doctor.check_audit()
    doctor.check_fts()
    doctor.check_graph()

    assert not (home / ".email-mcp").exists()


def test_no_filesystem_effect_precedes_a_refusal(home, monkeypatch, tmp_path):
    """Refusal happens BEFORE any effect — for every managed getter, not
    just the root's own."""
    docs = tmp_path / "Documents"
    docs.mkdir(mode=0o755)
    (docs / "thesis.txt").write_text("irreplaceable")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(docs))
    before = sorted(p.name for p in docs.iterdir())

    for getter in LEAF_GETTERS:
        with pytest.raises(config.StateDirRefused):
            getattr(config, getter)()

    assert sorted(p.name for p in docs.iterdir()) == before
    assert _mode(docs) == 0o755
    assert (docs / "thesis.txt").read_text() == "irreplaceable"


def test_clean_home_with_no_mail_store_degrades_and_creates_nothing(
        home, monkeypatch, tmp_path):
    """Clean-HOME execution: no ~/Library/Mail, no inherited EMAIL_MCP_*.

    Every read surface must answer (as a coded envelope or an empty
    result), nothing may traceback, and no state directory may appear. The
    v0.11 gate ran the whole suite this way precisely because the developer
    machine has a populated Mail store and a live ~/.email-mcp, so a code
    path that quietly depends on either passes there and fails for a new
    user on first launch.
    """
    from email_mcp import audit, doctor, fts, server, spool

    assert not (home / "Library" / "Mail").exists()

    assert audit.query() == {"events": [], "files_scanned": 0,
                             "skipped_lines": 0}
    assert spool.entries("pending") == []
    assert fts.status()["state"] == "absent"

    report = doctor.run()
    assert isinstance(report["ok"], bool)
    assert report["checks"]["state_root"]["ok"] is True   # absent ≠ broken
    assert report["checks"]["mail_store"]["ok"] is False  # honestly red

    out = server.tool_list_scheduled()
    assert out["ok"] is True
    assert all(out[s] == [] for s in ("pending", "sending", "sent",
                                      "failed", "cancelled"))
    out = server.tool_audit()
    assert out["ok"] is True and out["events"] == []

    assert not (home / ".email-mcp").exists()


def test_the_package_never_resolves_a_path_out_of_its_own_directory():
    """Wheel-installed behaviour: an installed package has no tests/,
    docs/, tools/ or pyproject.toml beside it.

    A module that walks up from ``__file__`` works in an editable install
    and dies in a wheel — the whole class of "it worked in the checkout"
    packaging bug. Only actual path arithmetic counts: the word "docs" is a
    JSON key in the FTS status dict and says nothing about the filesystem,
    so this matches code, not prose.
    """
    import ast
    import email_mcp
    from pathlib import Path

    pkg = Path(email_mcp.__file__).parent
    offenders: list[str] = []
    for py in sorted(pkg.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        for node in ast.walk(tree):
            # Path(__file__).parent.parent … — one .parent lands in the
            # package directory, two leaves it.
            if not (isinstance(node, ast.Attribute) and node.attr == "parent"):
                continue
            inner = node.value
            if isinstance(inner, ast.Attribute) and inner.attr == "parent":
                offenders.append(f"{py.relative_to(pkg)}:{node.lineno}")
    assert not offenders, (
        "package walks above its own directory (works in an editable "
        "install, breaks in a wheel): " + ", ".join(offenders))


def test_an_unreadable_root_is_refused_not_assumed_empty(
        home, monkeypatch, tmp_path):
    """Fail-open found at the v0.11 gate: a root whose contents cannot be
    listed was treated as EMPTY, and therefore adoptable.

    `chmod 0300` on a directory holding another tool's files made it
    silently adoptable — the marker was written and spool/ and audit/ were
    created inside it. The refusal exists precisely to stop email-mcp
    managing a directory that already holds someone else's data, and an
    undeterminable answer has to count as "refuse", not "proceed".
    """
    root = tmp_path / "someone-elses"
    root.mkdir(mode=0o700)
    (root / "their-secret.txt").write_text("not ours")
    root.chmod(0o300)                    # writable + traversable, NOT readable
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))
    try:
        reason = config.state_root_refusal()
        assert reason is not None and "cannot be read" in reason
        with pytest.raises(config.StateDirRefused):
            config.state_root()
        for getter in LEAF_GETTERS:
            with pytest.raises(config.StateDirRefused):
                getattr(config, getter)()
        # …and a mutation's receipt is dropped rather than the mutation
        # being blocked, or the foreign directory being written into.
        from email_mcp import audit
        assert audit.emit("send", outcome="sent") is None
    finally:
        root.chmod(0o700)

    assert sorted(p.name for p in root.iterdir()) == ["their-secret.txt"]
    assert (root / "their-secret.txt").read_text() == "not ours"


# --------------------------------------------------------------------- #
# gate-4 round 2 — findings from the independent audit                   #
# --------------------------------------------------------------------- #


def test_retired_var_fails_read_paths_closed_not_silently_empty(
        home, monkeypatch, tmp_path):
    """MAJOR: rejection lived only on the WRITE path, so read tools
    silently ignored a retired variable.

    With EMAIL_MCP_SPOOL_DIR set, `list_scheduled` resolved the DEFAULT
    root, found nothing there, and returned {"ok": true, "pending": []} —
    while the operator's queued mail sat in the directory the variable
    named, with nothing delivering it. An empty success is the worst
    possible answer to "where is my mail": indistinguishable from "you
    have none". Every tool must fail closed.
    """
    from email_mcp import server

    old = tmp_path / "oldspool" / "pending"
    old.mkdir(parents=True)
    (old / "m1.json").write_text('{"id": "x", "subject": "queued"}')
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "oldspool"))

    for tool in ("tool_list_scheduled", "tool_search_emails", "tool_audit",
                 "tool_list_mailboxes"):
        out = getattr(server, tool)()
        assert out["ok"] is False, f"{tool} answered ok:true"
        assert "retired" in out["error"], tool

    # doctor is the ONE exemption: it exists to report the fault, and the
    # error message sends the operator to it.
    report = server.tool_doctor()
    assert report["ok"] is False
    assert "retired" in report["checks"]["state_root"]["detail"]


def test_retired_var_is_refused_at_every_entry_point(home, monkeypatch,
                                                     tmp_path):
    """The startup gates, not just the lazy resolver: server, CLI and the
    launchd dispatcher each refuse before doing any work. The dispatcher
    matters most — it would otherwise drain the DEFAULT spool while the
    operator's mail sat elsewhere."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EMAIL_MCP_")}
    env["HOME"] = str(home)
    env["EMAIL_MCP_AUDIT_DIR"] = str(tmp_path / "old-ledger")
    for module, args in (("email_mcp.server", ["--selftest"]),
                         ("email_mcp.cli", ["version"]),
                         ("email_mcp.dispatcher", ["--status"])):
        p = subprocess.run([sys.executable, "-m", module, *args],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 2, f"{module} did not refuse: rc={p.returncode}"
        assert "retired" in p.stderr, module


def test_resolution_is_total_when_the_path_cannot_be_expanded(
        home, monkeypatch):
    """MAJOR: `~nosuchuser/foo` raised RuntimeError out of
    state_root(create=False), state_root_refusal() and every create=False
    getter — all documented as total. Uninstall planning and doctor call
    exactly those, so a typo'd variable was a traceback, not a message."""
    from email_mcp import audit, doctor, lifecycle

    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", "~nosuchuser12345/foo")

    assert isinstance(config.state_root(create=False), Path)   # no raise
    for getter in LEAF_GETTERS:
        assert isinstance(getattr(config, getter)(create=False), Path)
    assert isinstance(lifecycle.StatePaths.resolve(), lifecycle.StatePaths)
    assert isinstance(lifecycle.uninstall_plan(purge=True), dict)
    assert audit.query()["events"] == []
    check = doctor.check_state_root()
    assert check["ok"] is False and "cannot be resolved" in check["detail"]

    # …and the WRITE path still refuses, as a typed refusal.
    with pytest.raises(config.StateDirRefused):
        config.state_root()


def test_a_missing_parent_is_refused_not_created_world_readable(
        home, monkeypatch, tmp_path):
    """`mkdir(parents=True)` created every missing intermediate at the
    process umask: EMAIL_MCP_STATE_DIR=/a/b/c/root left a/, b/ and c/ at
    0755 — directories the user never named, made by a mail tool. We may
    not chmod them (not ours to mode), so they are not created at all."""
    deep = tmp_path / "deep" / "a" / "b" / "root"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(deep))

    reason = config.state_root_refusal()
    assert reason is not None and "does not exist" in reason
    with pytest.raises(config.StateDirRefused):
        config.state_root()
    assert not (tmp_path / "deep").exists()   # not one intermediate

    # With the parent present it is an ordinary relocation target again.
    deep.parent.mkdir(parents=True)
    root = config.state_root()
    assert _mode(root) == 0o700


# --------------------------------------------------------------------- #
# gate-4 round 3 — findings from the completed independent audits        #
# --------------------------------------------------------------------- #


def test_no_read_side_command_creates_or_marks_the_state_tree(
        home, monkeypatch):
    """MAJOR: `dispatcher --status` — a documented read-only overview —
    resolved config.spool_dir() with create=True, so *diagnosing* claimed
    the state root, marker and all. Three sibling leaks were found with
    it: spool.load / read_eml, plans.load / all_plans / gc, and
    fts._log_path (which built <root>/fts to format a plist string).

    The whole read surface, in one place, so the next one cannot hide.
    """
    from email_mcp import (audit, dispatcher, doctor, fts, lifecycle,
                           plans, server, spool)

    dispatcher.status()
    doctor.run()
    audit.query()
    fts.status()
    fts._log_path()
    for state in spool.STATES:
        spool.entries(state)
    spool.load("pending", "nope")
    plans.all_plans()
    plans.load("nope")
    plans.gc(plan_ids=[])
    lifecycle.StatePaths.resolve()
    lifecycle.uninstall_plan(purge=True)
    server.tool_list_scheduled()
    server.tool_audit()

    assert not (home / ".email-mcp").exists(), \
        "a read-side operation created the state tree"


def test_doctor_fix_never_chmods_an_identities_file_outside_the_root(
        home, monkeypatch, tmp_path):
    """MAJOR: EMAIL_MCP_IDENTITIES was entirely unfenced, and
    `doctor --fix` chmodded whatever it named — measured on a decoy
    ~/.zshrc going 644 -> 600. Naming a path as "my identities file" is
    not consent to have its mode changed, and a stale or mistyped value
    points it at something else entirely.

    This was the last live instance of "email-mcp changed the mode of
    something I did not name".
    """
    from email_mcp import repairs

    decoy = home / ".zshrc"
    decoy.write_text("export PATH=...\n")
    decoy.chmod(0o644)
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(decoy))

    repairs.run_fixes()

    assert _mode(decoy) == 0o644, "chmodded a file the user did not name"
    assert decoy not in repairs._state_files()


def test_doctor_fix_still_repairs_the_identities_file_inside_the_root(
        home):
    """The fence must be targeted, not a blanket opt-out: the real
    identities.toml, in the managed root, is still repaired."""
    from email_mcp import repairs

    root = home / ".email-mcp"
    root.mkdir()
    ident = root / "identities.toml"
    ident.write_text("default = 'x'\n")
    ident.chmod(0o644)

    result = repairs.run_fixes()

    assert _mode(ident) == 0o600
    assert "state_file_perms" in [a["repair"] for a in result["applied"]]


def test_every_read_path_degrades_over_an_unreadable_root(
        home, monkeypatch, tmp_path):
    """MINOR: a mode-000 root made even the stat probes raise, so
    audit.query, spool.entries, fts.status, plans.all_plans and
    uninstall planning threw PermissionError out of functions documented
    as total. A read must ANSWER."""
    from email_mcp import (audit, dispatcher, doctor, fts, lifecycle,
                           plans, server, spool)

    root = tmp_path / "opaque"
    root.mkdir()
    (root / "foreign.txt").write_text("theirs")
    root.chmod(0o000)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))
    try:
        assert "cannot be read" in (config.state_root_refusal() or "")
        assert audit.query()["events"] == []
        assert fts.status()["state"] == "absent"
        assert plans.all_plans() == []
        for state in spool.STATES:
            assert spool.entries(state) == []
        dispatcher.status()
        lifecycle.uninstall_plan(purge=True)
        assert doctor.run()["checks"]["state_root"]["ok"] is False
        assert server.tool_audit()["ok"] is True
    finally:
        root.chmod(0o700)

    assert (root / "foreign.txt").read_text() == "theirs"
    assert sorted(p.name for p in root.iterdir()) == ["foreign.txt"]


def test_a_default_root_that_is_a_regular_file_is_refused(home):
    """MINOR: the `if not explicit: return None` short-circuit ran BEFORE
    the non-directory check, so only an explicitly named root got it. A
    stray file at ~/.email-mcp escaped as a raw NotADirectoryError from
    whichever getter ran first."""
    (home / ".email-mcp").write_text("i am a file")

    reason = config.state_root_refusal()
    assert reason is not None and "not a directory" in reason
    for getter in LEAF_GETTERS:
        with pytest.raises(config.StateDirRefused):
            getattr(config, getter)()
    assert (home / ".email-mcp").read_text() == "i am a file"


def test_state_paths_reports_the_meta_json_that_is_actually_written(
        home, monkeypatch, tmp_path):
    """MINOR: StatePaths.resolve() reported <root>/meta.json while
    write_meta() wrote ~/.email-mcp/meta.json, so with a relocated root
    the reported path named a file that did not exist."""
    from email_mcp import lifecycle

    relocated = tmp_path / "elsewhere"
    relocated.mkdir()
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(relocated))
    lifecycle.write_meta({"state_version": 1})

    assert lifecycle.StatePaths.resolve().meta == lifecycle.meta_path()
    assert lifecycle.StatePaths.resolve().meta.is_file()


def test_the_migration_message_names_the_adoption_step(home, monkeypatch):
    """MAJOR (usability): the runtime message told the user to move their
    contents into the new root — which then makes it non-empty and
    unmarked, and therefore refused. Following the message literally was
    a dead end, and `doctor --fix` did not rescue it. The message now
    names the marker and points at the full procedure."""
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", "/tmp/whatever")

    msg = config.retired_state_var_error()
    assert msg is not None
    assert config.STATE_MARKER in msg
    assert "doctor --fix" in msg
    assert "docs/reference.md" in msg
