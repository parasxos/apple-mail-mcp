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
