"""Registration-surface tests: the twenty-one-tool default surface, and the
exact eleven read-side tools under EMAIL_MCP_READ_ONLY=1.

The gate is lexical — in a read-only session the mutating ten are never
registered, so they cannot be re-enabled by any later call."""
from __future__ import annotations

import asyncio

from email_mcp import server

READ_ONLY_TOOLS = {
    "search_emails",
    "get_email",
    "get_emails_batch",
    "get_thread",
    "list_mailboxes",
    "list_recent",
    "get_attachment",
    "refresh_mail",
    "list_scheduled",
    "doctor",
    "audit",
}

MUTATING_TOOLS = {
    "send_email",
    "create_draft",
    "reply_email",
    "schedule_email",
    "cancel_scheduled",
    "triage_plan",
    "triage_plan_delete",
    "triage_apply",
    "mailbox_create",
    "mailbox_delete",
}

ALL_TOOLS = READ_ONLY_TOOLS | MUTATING_TOOLS


def _tool_names(mcp) -> set[str]:
    try:
        tools = asyncio.run(mcp.list_tools())
        return {t.name for t in tools}
    except Exception:
        # FastMCP API drift fallback: enumerate the tool manager directly.
        return {t.name for t in mcp._tool_manager.list_tools()}


def test_default_surface_is_exactly_twenty_one(monkeypatch):
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    names = _tool_names(server._build_mcp_server())
    assert names == ALL_TOOLS
    assert len(names) == 21


def test_read_only_surface_is_exactly_eleven(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_READ_ONLY", "1")  # BEFORE building
    names = _tool_names(server._build_mcp_server())
    assert names == READ_ONLY_TOOLS
    assert len(names) == 11
    assert not names & MUTATING_TOOLS


def test_read_only_env_is_read_at_build_time(monkeypatch):
    """Flipping the env after the server is built changes nothing — the
    gate runs once, at registration."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    monkeypatch.setenv("EMAIL_MCP_READ_ONLY", "1")
    assert _tool_names(mcp) == ALL_TOOLS


def test_from_addr_matching_semantics_are_documented(monkeypatch):
    """search/triage_plan advertise the substring truth; the destructive
    planner advertises its exact matcher (descriptions are outside the
    inputSchema freeze by design — see test_schema_snapshot)."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    try:
        desc = {t.name: t.description or ""
                for t in asyncio.run(mcp.list_tools())}
    except Exception:
        desc = {t.name: t.description or ""
                for t in mcp._tool_manager.list_tools()}
    assert "substring" in desc["search_emails"].lower()
    assert "substring" in desc["triage_plan"].lower()
    assert "exact" in desc["triage_plan_delete"].lower()
    assert "substring" in desc["triage_plan_delete"].lower()  # the contrast
