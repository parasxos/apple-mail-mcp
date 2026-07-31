"""The state-dir getters' chmod fence (email_mcp.config._lock_down).

Every state dir is created 0700 — they hold outgoing message bodies, OAuth
token caches and plan metadata. The subtlety is the PARENT: locking down
``~/.email-mcp`` is ours to do, but the parent of a directory the operator
named with EMAIL_MCP_*_DIR is not.

The v0.11 red-team proved the original getters chmodded it unconditionally,
so ``EMAIL_MCP_SPOOL_DIR=/somewhere/mine/spool`` silently took
``/somewhere/mine`` from 0755 to 0700 — and pointing it one level up would
have done the same to a home or a volume root. The lifecycle fences caught
the degenerate ``$HOME`` case, but they live in the CALLERS; these getters
are reachable directly from the server and the dispatcher.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from email_mcp import config


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No EMAIL_MCP_* overrides, $HOME inside tmp."""
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def audit_dir_guard(clean_env):
    """Shadow conftest's guard (by name, on purpose — the test_audit.py
    pattern): conftest pins the config.audit_dir RESOLVER to a tmp dir,
    which would bypass the very fence these tests exercise. The fake HOME
    from clean_env is the isolation."""
    return clean_env / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def fts_dir_guard(clean_env):
    """Shadow conftest's fts guard too: it sets EMAIL_MCP_FTS_DIR, and
    fixture ordering would re-set it AFTER clean_env's wipe — turning the
    default-path test into an override test."""
    return clean_env / ".email-mcp" / "fts"


GETTERS = ("spool_dir", "graph_dir", "plans_dir", "fts_dir")


@pytest.mark.parametrize("getter", GETTERS)
def test_operator_named_parent_is_never_chmodded(clean_env, tmp_path, monkeypatch, getter):
    """An EMAIL_MCP_*_DIR the operator chose: we own the dir, not its parent."""
    var = {
        "spool_dir": "EMAIL_MCP_SPOOL_DIR", "graph_dir": "EMAIL_MCP_GRAPH_DIR",
        "plans_dir": "EMAIL_MCP_PLANS_DIR", "fts_dir": "EMAIL_MCP_FTS_DIR",
    }[getter]
    parent = tmp_path / "operators-own-dir"
    parent.mkdir(mode=0o755)
    (parent / "keepme.txt").write_text("not ours")
    monkeypatch.setenv(var, str(parent / "state"))

    getattr(config, getter)()

    assert _mode(parent) == 0o755, f"{getter} chmodded a parent it does not own"
    assert (parent / "keepme.txt").read_text() == "not ours"
    # The dir we WERE asked to make is still tightened — the point of the fence
    # is precision, not giving up.
    assert _mode(parent / "state") == 0o700


@pytest.mark.parametrize("getter", GETTERS)
def test_default_path_still_locks_down_the_state_root(clean_env, getter):
    """The fence must not over-correct: on the default path ~/.email-mcp is
    ours and must still end up 0700."""
    getattr(config, getter)()

    root = clean_env / ".email-mcp"
    assert _mode(root) == 0o700, "the state root stopped being locked down"


@pytest.mark.parametrize("getter", GETTERS)
def test_symlinked_state_dir_is_not_chmodded_through(clean_env, tmp_path, monkeypatch, getter):
    """A symlinked state dir: chmod would land on the link's target."""
    var = {
        "spool_dir": "EMAIL_MCP_SPOOL_DIR", "graph_dir": "EMAIL_MCP_GRAPH_DIR",
        "plans_dir": "EMAIL_MCP_PLANS_DIR", "fts_dir": "EMAIL_MCP_FTS_DIR",
    }[getter]
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    (victim / "data.txt").write_text("theirs")
    link = tmp_path / "link"
    link.symlink_to(victim)
    monkeypatch.setenv(var, str(link))

    getattr(config, getter)()

    assert _mode(victim) == 0o755, f"{getter} chmodded through a symlink"
    assert (victim / "data.txt").read_text() == "theirs"


@pytest.mark.parametrize("getter", GETTERS)
def test_override_at_or_above_home_is_refused(clean_env, monkeypatch, getter):
    """`EMAIL_MCP_*_DIR=/Users` must not become `chmod 0700 /Users`.

    Skipping the PARENT chmod was not enough: the target itself was still
    tightened, so pointing an override at the parent of $HOME still changed
    a directory the user did not name, system-wide on a real Mac. The fence
    refuses, and compares RESOLVED paths so `$HOME/sub/..` cannot slip past.
    """
    var = {
        "spool_dir": "EMAIL_MCP_SPOOL_DIR", "graph_dir": "EMAIL_MCP_GRAPH_DIR",
        "plans_dir": "EMAIL_MCP_PLANS_DIR", "fts_dir": "EMAIL_MCP_FTS_DIR",
    }[getter]
    home = clean_env
    ancestor = home.parent

    for spelling in (str(home), f"{home}/sub/..", str(ancestor)):
        monkeypatch.setenv(var, spelling)
        before = _mode(ancestor)
        contents = set(p.name for p in ancestor.iterdir())
        with pytest.raises(config.StateDirRefused):
            getattr(config, getter)()
        assert _mode(ancestor) == before, f"{spelling} changed the mode anyway"
        # Contents too, not just mode: an earlier fence ran AFTER spool_dir's
        # subdir mkdir loop, so the refusal fired with five directories
        # already created at the refused location — and a release gate that
        # measured only the mode reported that defect closed.
        assert set(p.name for p in ancestor.iterdir()) == contents, (
            f"{spelling} created entries before refusing")


def test_read_side_resolution_never_creates(clean_env, tmp_path, monkeypatch):
    """create=False resolves a path without materialising it.

    A plain `doctor` used to build the spool tree wherever the override
    pointed — including inside ~/Library/Mail, contradicting the one
    guarantee the security posture states most absolutely.
    """
    target = tmp_path / "not-created-by-a-read"
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(target))

    resolved = config.spool_dir(create=False)

    assert resolved == target
    assert not target.exists()


def test_audit_dir_refuses_a_degenerate_override_before_any_effect(
        clean_env, monkeypatch):
    """audit_dir was the getter the first fence pass missed entirely —
    and audit.emit() calls it on EVERY mutation, so an unfenced ledger
    meant EMAIL_MCP_AUDIT_DIR=/Users chmodded it and wrote 2026-MM.jsonl
    into it on the first send."""
    ancestor = clean_env.parent
    for spelling in (str(clean_env), f"{clean_env}/sub/..", str(ancestor)):
        monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", spelling)
        before = _mode(ancestor)
        contents = set(p.name for p in ancestor.iterdir())
        with pytest.raises(config.StateDirRefused):
            config.audit_dir(create=True)
        assert _mode(ancestor) == before
        assert set(p.name for p in ancestor.iterdir()) == contents


def test_emit_drops_the_event_when_the_ledger_location_is_refused(
        clean_env, monkeypatch):
    """The emit-failure policy: a refused ledger costs receipts, never a
    mutation. emit must return None with nothing written — not raise into
    the mutation that called it."""
    from email_mcp import audit

    ancestor = clean_env.parent
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(ancestor))
    before_mode = _mode(ancestor)

    op = audit.emit("send", outcome="sent", operation_id="x")

    assert op is None
    assert _mode(ancestor) == before_mode
    assert not list(ancestor.glob("*.jsonl"))


def test_attach_dir_refuses_a_degenerate_override(clean_env, monkeypatch):
    """attach_dir has no chmod, but mkdir at $HOME-or-above is still not
    ours to do — it was the last unfenced state-dir getter."""
    ancestor = clean_env.parent
    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(ancestor))
    with pytest.raises(config.StateDirRefused):
        config.attach_dir()


def test_case_variant_of_home_is_refused(clean_env, monkeypatch, tmp_path):
    """macOS ships case-insensitive APFS, so $HOME has many spellings.

    The fence compared RESOLVED STRINGS, and resolve() preserves whatever
    spelling it was handed — so /users/paris, /USERS/PARIS and the
    /System/Volumes/Data firmlink each named the real home directory while
    comparing unequal, and every one of them walked straight through.
    Identity is the inode, not the text.
    """
    home = clean_env
    upper = home.parent / home.name.upper()
    if not upper.exists() or upper.stat().st_ino != home.stat().st_ino:
        pytest.skip("filesystem is case-sensitive; the variant is a real path")

    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(upper))
    with pytest.raises(config.StateDirRefused):
        config.spool_dir()
    assert _mode(home) == 0o755
    assert not list(home.iterdir())


def test_symlinked_default_spool_root_never_chmods_its_target(
        clean_env, tmp_path):
    """The subdir loop ran BEFORE the link check and mkdir/chmod resolve
    through a link, so a squat on the default spool root had its target's
    five pre-existing subdirectories taken to 0700."""
    victim = tmp_path / "victim"
    victim.mkdir()
    for sub in ("pending", "sending", "sent", "failed", "cancelled"):
        (victim / sub).mkdir(mode=0o755)
    (clean_env / ".email-mcp").mkdir()
    (clean_env / ".email-mcp" / "spool").symlink_to(victim)

    config.spool_dir()

    for sub in ("pending", "sending", "sent", "failed", "cancelled"):
        assert _mode(victim / sub) == 0o755, f"{sub} chmodded through the link"
