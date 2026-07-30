"""Schema snapshots: the 20-tool wire surface, frozen and self-defending.

inputSchema — FROZEN at v0.10 (docs/v1-contract.md §8; inputs did not
change in v0.11). Descriptions are EXCLUDED from the snapshot: docstring
churn is allowed, schemas are not.

outputSchema — FROZEN at v0.11 (§8), right after the bare shapes took
their one allowed break into envelopes. FastMCP 1.27 derives outputSchema
from return annotations, and every tool is annotated `-> dict` (envelopes
are dynamic dicts), so the DECLARED outputSchema is None for all 20 tools
— there is no declared schema to freeze. The strongest freeze available
is therefore structural: call every tool_* wrapper against the mail
fixture under tmp EMAIL_MCP_* state dirs (transport/osascript boundaries
faked, exactly the seams the source-layer tests fake) and snapshot the
SHAPE of each success envelope — recursive keys plus scalar type names,
values dropped. The declared-None is snapshotted too, so a future typed
return (which would put a real outputSchema on the wire) also trips the
freeze. Both freezes fail loudly when their snapshot file is missing
(audit finding F7) — never a silent re-freeze."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess

import pytest
from pathlib import Path

from email_mcp import config, dispatcher, doctor, sender, server, triage
from email_mcp.fts import FtsIndex
from email_mcp.sources.apple_mail import AppleMailSource

SNAPSHOT = Path(__file__).parent / "snapshots" / "input_schemas.json"
OUTPUT_SNAPSHOT = Path(__file__).parent / "snapshots" / "output_schemas.json"


def _strip_descriptions(node):
    """Drop every `description` key, recursively — the snapshot freezes
    shapes (properties, types, defaults, required), never prose."""
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items()
                if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(x) for x in node]
    return node


def _current_schemas(monkeypatch) -> dict:
    """{tool name: inputSchema} for the full 20-tool surface."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    try:
        pairs = {t.name: t.inputSchema
                 for t in asyncio.run(mcp.list_tools())}
    except Exception:
        # FastMCP API drift fallback: enumerate the tool manager directly.
        pairs = {t.name: t.parameters
                 for t in mcp._tool_manager.list_tools()}
    return {name: _strip_descriptions(schema)
            for name, schema in sorted(pairs.items())}


def _render(schemas: dict) -> str:
    """Canonical byte form: sorted keys, 2-space indent, trailing newline —
    regenerating an unchanged surface is a byte-identical file."""
    return json.dumps(schemas, indent=2, sort_keys=True) + "\n"


def test_input_schemas_match_snapshot(monkeypatch):
    current = _current_schemas(monkeypatch)
    assert len(current) == 20
    if not SNAPSHOT.exists():
        # The freeze must be self-defending: a deleted snapshot fails loudly
        # instead of silently re-freezing whatever the code now emits
        # (audit finding F7). Restore it from git, or regenerate DELIBERATELY:
        #   python -c "from tests.test_schema_snapshot import *; \
        #              SNAPSHOT.write_text(_render(_current_schemas(...)))"
        pytest.fail(
            "tests/snapshots/input_schemas.json is missing — the inputSchema "
            "freeze (contract §8) cannot be verified. Restore it via "
            "`git checkout tests/snapshots/` or regenerate deliberately."
        )
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == frozen, (
        "inputSchema drift against tests/snapshots/input_schemas.json — "
        "tool inputs are FROZEN at v0.10 (docs/v1-contract.md §8). If the "
        "change is genuinely intended, regenerate the snapshot and say so "
        "explicitly in the change description."
    )


def test_snapshot_regeneration_is_byte_stable(monkeypatch):
    first = _render(_current_schemas(monkeypatch))
    second = _render(_current_schemas(monkeypatch))
    assert first == second  # two builds, identical bytes
    assert first == SNAPSHOT.read_text(encoding="utf-8")


# --------------------------------------------------------------------- #
# outputSchema freeze (v0.11)                                           #
# --------------------------------------------------------------------- #


def _shape(node, depth: int = 0):
    """Structure without values: dict -> {key: shape}, list -> [shape of
    first element] (empty list -> []), scalar -> type name. Depth-capped so
    a deep payload cannot make the snapshot unstable."""
    if depth > 4:
        return "..."
    if isinstance(node, dict):
        return {k: _shape(v, depth + 1) for k, v in sorted(node.items())}
    if isinstance(node, list):
        return [_shape(node[0], depth + 1)] if node else []
    if node is None:
        return "null"
    return type(node).__name__


def _declared_output_schemas(monkeypatch) -> dict:
    """{tool name: declared outputSchema}. Every tool is annotated `-> dict`,
    so FastMCP declares nothing today — freezing the None map means a future
    typed return (a real wire schema) trips this test deliberately."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    try:
        tools = asyncio.run(mcp.list_tools())
    except Exception:
        tools = mcp._tool_manager.list_tools()
    return {t.name: getattr(t, "outputSchema", None) for t in sorted(
        tools, key=lambda t: t.name)}


def _read_tool_shapes(monkeypatch, tmp_path, mail_fixture) -> dict:
    """Success-envelope shapes for the read surface — the tools whose shapes
    N1 broke on purpose (bare dict/array -> envelope). Mutating tools are
    deliberately NOT probed here: their shapes are pinned behaviorally by
    tests/test_sender.py, test_scheduled_send.py, test_triage.py,
    test_graph.py and test_audit_hooks.py, and driving them from a schema
    test would mean faking five transports to learn nothing extra."""
    for key in list(os.environ):
        if key.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("EMAIL_MCP_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("EMAIL_MCP_FTS_DIR", str(tmp_path / "fts"))
    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(tmp_path / "attach"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "none.toml"))
    monkeypatch.setattr(server, "_SOURCE", AppleMailSource(mail_base=mail_fixture),
                        raising=False)

    from email_mcp import audit
    audit.emit("deliver", outcome="sent", operation_id="snap-1",
               spool_id="snap-1", tool="schedule_email")

    # The fixture's attachment id is assigned by the parser, not the DB row —
    # resolve it the way the source-layer test does.
    att_id = AppleMailSource(mail_base=mail_fixture).get("101").attachments[0].attachment_id

    probes = {
        "search_emails": lambda: server.tool_search_emails(query="I2C"),
        "get_email": lambda: server.tool_get_email("100"),
        "get_emails_batch": lambda: server.tool_get_emails_batch(["100", "101"]),
        "get_thread": lambda: server.tool_get_thread("7001"),
        "list_mailboxes": lambda: server.tool_list_mailboxes(),
        "list_recent": lambda: server.tool_list_recent(limit=2),
        "get_attachment": lambda: server.tool_get_attachment("101", att_id),
        "list_scheduled": lambda: server.tool_list_scheduled(),
        "audit": lambda: server.tool_audit(limit=1),
    }
    out = {}
    for name, call in sorted(probes.items()):
        result = call()
        assert isinstance(result, dict) and result.get("ok") is True, (
            f"{name} did not return a success envelope: {result!r}")
        out[name] = _shape(result)
    return out


def test_declared_output_schemas_match_snapshot(monkeypatch, tmp_path, mail_fixture):
    declared = _declared_output_schemas(monkeypatch)
    shapes = _read_tool_shapes(monkeypatch, tmp_path, mail_fixture)
    current = {"declared": declared, "read_success_shapes": shapes}
    assert len(declared) == 20
    if not OUTPUT_SNAPSHOT.exists():
        pytest.fail(
            "tests/snapshots/output_schemas.json is missing — the outputSchema "
            "freeze (contract §8) cannot be verified. Restore it via "
            "`git checkout tests/snapshots/` or regenerate deliberately."
        )
    frozen = json.loads(OUTPUT_SNAPSHOT.read_text(encoding="utf-8"))
    assert current == frozen, (
        "output surface drift against tests/snapshots/output_schemas.json — "
        "tool RETURNS are FROZEN at v0.11 (docs/v1-contract.md §8); a "
        "breaking change is v2. Regenerate only for an intended additive "
        "change and say so explicitly."
    )


def test_output_snapshot_is_byte_stable(monkeypatch, tmp_path, mail_fixture):
    declared = _declared_output_schemas(monkeypatch)
    shapes = _read_tool_shapes(monkeypatch, tmp_path, mail_fixture)
    payload = {"declared": declared, "read_success_shapes": shapes}
    assert _render(payload) == OUTPUT_SNAPSHOT.read_text(encoding="utf-8")
