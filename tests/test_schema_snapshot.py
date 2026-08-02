"""inputSchema snapshot: the 20-tool input surface is FROZEN at v0.10
(docs/v1-contract.md §8 — inputs do not change in v0.11). Descriptions are
EXCLUDED from the snapshot: docstring churn is allowed, schemas are not.
The outputSchema freeze lives in test_output_schemas.py."""
from __future__ import annotations

import asyncio
import json

import pytest
from pathlib import Path

from email_mcp import server

SNAPSHOT = Path(__file__).parent / "snapshots" / "input_schemas.json"


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
    """{tool name: inputSchema} for the full 21-tool surface."""
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
    assert len(current) == 21  # +create_draft, additive 2026-08-02
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
