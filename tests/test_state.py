"""State-tree policy suite (email_mcp.state) — v0.11-clean S1.

The whole refusal policy as ONE table; adoption's effects; reader purity.
Concept: one promise, one place, one test — a policy row that fails here
has no second fence anywhere else to catch it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from email_mcp import audit, config, state
from email_mcp.state import MARKER, Refused, RefusedError, Resolved, State


def _set_root(monkeypatch, root) -> None:
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))


def _fake_home(tmp_path, monkeypatch) -> Path:
    """A fake $HOME inside tmp so home-identity rows are testable."""
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _restore_perms(root: Path) -> None:
    """Undo hostile-permission rows so tmp cleanup stays quiet."""
    for p in [root, *root.rglob("*")]:
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o700)


# ---------------------------------------------------------------------- #
# THE refusal policy table — every row of State.resolve()'s policy        #
# ---------------------------------------------------------------------- #


def _row_home(t, m):
    _set_root(m, _fake_home(t, m))


def _row_home_by_symlink(t, m):  # identity by inode, never path spelling
    home = _fake_home(t, m)
    link = t / "innocent-looking"
    link.symlink_to(home)
    _set_root(m, link)


def _row_home_ancestor(t, m):
    _fake_home(t, m)  # home = t/home, so t is an ancestor
    _set_root(m, t)


def _row_filesystem_root(t, m):
    _fake_home(t, m)  # "/" is an ancestor of any home
    _set_root(m, "/")


def _row_squatting_file(t, m):
    (t / "state").write_text("not a directory")
    _set_root(m, t / "state")


def _row_dangling_symlink(t, m):
    (t / "state").symlink_to(t / "gone")
    _set_root(m, t / "state")


def _row_missing_parent(t, m):
    _set_root(m, t / "no" / "such" / "state")


def _row_unwritable_parent(t, m):
    p = t / "ro"
    p.mkdir()
    p.chmod(0o500)
    _set_root(m, p / "state")


def _row_unlistable_root(t, m):
    r = t / "state"
    r.mkdir()
    r.chmod(0o000)
    _set_root(m, r)


def _row_foreign_nonempty_no_marker(t, m):
    r = t / "state"
    r.mkdir()
    (r / "thesis.tex").write_text("precious")
    _set_root(m, r)


def _row_unknown_user(t, m):
    _set_root(m, "~nosuchuser99/state")


REFUSAL_ROWS = [
    *[
        pytest.param(
            (lambda t, m, v=var: m.setenv(v, str(t / "old"))),
            (var, "retired", "EMAIL_MCP_STATE_DIR"),
            id=f"retired-{var}",
        )
        for var in state.RETIRED_VARS
    ],
    pytest.param(_row_home, ("home directory",), id="root-is-home"),
    pytest.param(_row_home_by_symlink, ("home directory",),
                 id="root-is-home-by-inode"),
    pytest.param(_row_home_ancestor, ("home directory",), id="home-ancestor"),
    pytest.param(_row_filesystem_root, ("home directory",), id="fs-root"),
    pytest.param(_row_squatting_file, ("not a directory",), id="root-squat"),
    pytest.param(_row_dangling_symlink, ("symlink",), id="dangling-symlink"),
    pytest.param(_row_missing_parent, ("cannot be created",),
                 id="missing-parent"),
    pytest.param(_row_unwritable_parent, ("not writable",),
                 id="unwritable-parent"),
    pytest.param(_row_unlistable_root, ("cannot be listed",),
                 id="unlistable-root"),
    pytest.param(_row_foreign_nonempty_no_marker, (MARKER, "foreign"),
                 id="foreign-dir-no-marker"),
    pytest.param(_row_unknown_user, ("expanded",), id="unknown-user"),
]


@pytest.mark.parametrize("setup, fragments", REFUSAL_ROWS)
def test_refusal_policy_table(setup, fragments, tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    try:
        r = State.resolve()
    finally:
        _restore_perms(tmp_path)
    assert isinstance(r, Refused)
    for fragment in fragments:
        assert fragment in r.reason, (fragment, r.reason)


def test_all_set_retired_vars_are_named_at_once(tmp_path, monkeypatch):
    for var in state.RETIRED_VARS:
        monkeypatch.setenv(var, str(tmp_path / "old"))
    r = State.resolve()
    assert isinstance(r, Refused)
    for var in state.RETIRED_VARS:
        assert var in r.reason


# ---------------------------------------------------------------------- #
# resolution — the accepted rows                                          #
# ---------------------------------------------------------------------- #


def test_explicit_empty_root_resolves(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    _set_root(monkeypatch, root)
    r = State.resolve()
    assert isinstance(r, Resolved) and r.root == root


def test_explicit_nonempty_root_with_marker_resolves(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    (root / MARKER).write_text("email-mcp state root\n")
    (root / "audit").mkdir()
    _set_root(monkeypatch, root)
    assert isinstance(State.resolve(), Resolved)


def test_default_root_is_exempt_from_content_check(tmp_path, monkeypatch):
    """The v0.10 upgrade path: an unmarked, populated ~/.email-mcp still
    resolves, and adoption marks it — only explicit overrides must prove
    themselves with the marker."""
    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)
    root = home / ".email-mcp"
    (root / "spool" / "pending").mkdir(parents=True)
    (root / "plans").mkdir()
    r = State.resolve()
    assert isinstance(r, Resolved) and r.root == root
    r.adopt()
    assert (root / MARKER).is_file()
    assert (root / "spool" / "pending").is_dir()  # v0.10 content preserved


def test_relative_override_resolves_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_root(monkeypatch, "relative-state")
    r = State.resolve()
    assert isinstance(r, Resolved)
    assert r.root == tmp_path / "relative-state"


def test_refused_reason_is_the_one_string_everywhere(tmp_path, monkeypatch):
    """Every surface gets the SAME string: the value's reason, the raising
    accessors, and a config getter all agree by construction."""
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "old"))
    r = State.resolve()
    assert isinstance(r, Refused)
    with pytest.raises(RefusedError) as via_reader:
        r.reader()
    with pytest.raises(RefusedError) as via_adopt:
        r.adopt()
    with pytest.raises(RefusedError) as via_config:
        config.audit_dir()
    assert str(via_reader.value) == str(via_adopt.value) \
        == str(via_config.value) == r.reason
    # ...and a write path drops rather than honoring the retired var:
    assert audit.emit("send", outcome="sent") is None
    assert not (tmp_path / "old").exists()


# ---------------------------------------------------------------------- #
# adoption — the one effectful door                                       #
# ---------------------------------------------------------------------- #


def _adopt(monkeypatch, root) -> state.StateWriter:
    _set_root(monkeypatch, root)
    return State.resolve().adopt()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_adopt_creates_root_0700_and_marker_0600(tmp_path, monkeypatch):
    root = tmp_path / "state"
    _adopt(monkeypatch, root)
    assert root.is_dir() and _mode(root) == 0o700
    marker = root / MARKER
    assert marker.is_file() and not marker.is_symlink()
    assert _mode(marker) == 0o600


def test_adopt_is_umask_proof(tmp_path, monkeypatch):
    old = os.umask(0o277)  # would clip mkdir/open modes to 0400
    try:
        leaf = _adopt(monkeypatch, tmp_path / "state").audit
    finally:
        os.umask(old)
    assert _mode(tmp_path / "state") == 0o700
    assert _mode(tmp_path / "state" / MARKER) == 0o600
    assert _mode(leaf) == 0o700


def test_adopt_never_creates_parents(tmp_path, monkeypatch):
    """mkdir(parents=False) by policy: a missing parent is a refusal, never
    a silently manufactured path chain."""
    _set_root(monkeypatch, tmp_path / "a" / "state")
    with pytest.raises(RefusedError):
        State.resolve().adopt()
    assert not (tmp_path / "a").exists()


def test_adopt_is_idempotent(tmp_path, monkeypatch):
    root = tmp_path / "state"
    _adopt(monkeypatch, root).spool
    before = (root / MARKER).read_bytes()
    _adopt(monkeypatch, root).spool
    assert (root / MARKER).read_bytes() == before


def test_adopt_refuses_symlinked_marker(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    (root / MARKER).symlink_to(tmp_path / "elsewhere")
    _set_root(monkeypatch, root)
    with pytest.raises(RefusedError, match="symlink"):
        State.resolve().adopt()
    assert not (tmp_path / "elsewhere").exists()  # never written through


def test_writer_creates_all_leaves_0700(tmp_path, monkeypatch):
    root = tmp_path / "state"
    writer = _adopt(monkeypatch, root)
    assert writer.plans == root / "plans"
    assert writer.graph == root / "graph"
    assert writer.fts == root / "fts"
    assert writer.audit == root / "audit"
    spool = writer.spool
    assert spool == root / "spool"
    leaves = [spool, writer.plans, writer.graph, writer.fts, writer.audit]
    leaves += [spool / s for s in state.SPOOL_STATES]
    for leaf in leaves:
        assert leaf.is_dir() and not leaf.is_symlink(), leaf
        assert _mode(leaf) == 0o700, leaf


def test_writer_refuses_symlinked_leaf(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "state"
    writer = _adopt(monkeypatch, root)
    (root / "fts").symlink_to(target)
    with pytest.raises(RefusedError, match="symlink"):
        writer.fts
    assert list(target.iterdir()) == []


def test_writer_refuses_squatted_leaf_but_not_its_siblings(
    tmp_path, monkeypatch,
):
    """Leaves are independent: a file squatting on audit refuses audit
    only — the spool still adopts (contract §6: a broken ledger never
    blocks mail)."""
    root = tmp_path / "state"
    writer = _adopt(monkeypatch, root)
    (root / "audit").write_text("squatter")
    with pytest.raises(RefusedError, match="not a directory"):
        writer.audit
    assert (writer.spool / "pending").is_dir()
    assert (root / "audit").read_text() == "squatter"


def test_writer_heals_owned_chmod_accident(tmp_path, monkeypatch):
    root = tmp_path / "state"
    writer = _adopt(monkeypatch, root)
    leaf = writer.plans
    leaf.chmod(0o500)
    assert writer.plans == leaf
    assert _mode(leaf) == 0o700


# ---------------------------------------------------------------------- #
# the reader — no effectful surface exists                                #
# ---------------------------------------------------------------------- #


def test_reader_answers_every_path_question_purely(tmp_path, monkeypatch):
    root = tmp_path / "state"
    _set_root(monkeypatch, root)
    reader = State.resolve().reader()
    assert reader.root == root
    assert reader.marker == root / MARKER
    assert reader.spool == root / "spool"
    assert reader.plans == root / "plans"
    assert reader.graph == root / "graph"
    assert reader.fts == root / "fts"
    assert reader.audit == root / "audit"
    for s in state.SPOOL_STATES:
        assert reader.spool_state(s) == root / "spool" / s
    assert not root.exists()  # asking questions created nothing


def test_reads_on_a_fresh_home_create_nothing(tmp_path, monkeypatch):
    """THE reader-purity acceptance: on a virgin $HOME with no override,
    every read surface — reader questions, the config getters, an audit
    query, the fts db path — leaves ~/.email-mcp uncreated. A read path
    cannot create state because StateReader has nothing effectful to
    call; there is no create= flag to forget."""
    from email_mcp import fts

    h = tmp_path / "fresh-home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR", raising=False)

    reader = State.resolve().reader()
    _ = (reader.marker, reader.spool, reader.plans, reader.graph,
         reader.fts, reader.audit)
    assert config.spool_dir() == h / ".email-mcp" / "spool"
    assert config.plans_dir() == h / ".email-mcp" / "plans"
    assert config.graph_dir() == h / ".email-mcp" / "graph"
    assert config.fts_dir() == h / ".email-mcp" / "fts"
    assert config.audit_dir() == h / ".email-mcp" / "audit"
    assert audit.query() == {"events": [], "files_scanned": 0,
                             "skipped_lines": 0}
    assert fts.db_path() == h / ".email-mcp" / "fts" / "fts.db"
    assert list(h.iterdir()) == []  # the home is exactly as we found it
