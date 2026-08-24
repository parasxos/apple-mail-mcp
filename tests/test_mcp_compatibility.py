"""MCP client-facing metadata and structured-result compatibility.

These tests deliberately exercise the advertised protocol surface, not the
underlying ``tool_*`` Python functions.  An MCP client has only titles,
descriptions, JSON Schemas, annotations, and CallToolResult to guide it.
"""
from __future__ import annotations

import asyncio
import json

from jsonschema import Draft202012Validator

from email_mcp import server
from tests._mcp_sdk import sdk_attr
from tests.test_server_registration import ALL_TOOLS


def _tools(monkeypatch):
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    return {tool.name: tool
            for tool in asyncio.run(server._build_mcp_server().list_tools())}


def _string_branch(schema: dict) -> dict:
    """Return the string branch of a nullable/string JSON Schema."""
    if schema.get("type") == "string":
        return schema
    return next(branch for branch in schema.get("anyOf", [])
                if branch.get("type") == "string")


def test_all_tools_advertise_human_titles_safety_hints_and_parameter_help(
    monkeypatch,
):
    tools = _tools(monkeypatch)
    assert set(tools) == ALL_TOOLS

    for name, tool in tools.items():
        assert tool.title, f"{name} has no client-facing title"
        assert tool.description, f"{name} has no client-facing description"
        assert tool.annotations is not None, f"{name} has no safety hints"
        assert sdk_attr(tool.annotations, "readOnlyHint", "read_only_hint") is not None
        assert sdk_attr(tool.annotations, "destructiveHint", "destructive_hint") is not None
        assert sdk_attr(tool.annotations, "idempotentHint", "idempotent_hint") is not None
        assert sdk_attr(tool.annotations, "openWorldHint", "open_world_hint") is not None
        input_schema = sdk_attr(tool, "inputSchema", "input_schema")
        for parameter, schema in input_schema.get("properties", {}).items():
            assert schema.get("description"), (
                f"{name}.{parameter} has no client-facing help"
            )


def test_mutation_annotations_distinguish_safe_destructive_and_external_tools(
    monkeypatch,
):
    tools = _tools(monkeypatch)

    def hint(name, v1, v2):
        return sdk_attr(tools[name].annotations, v1, v2)

    assert hint("search_emails", "readOnlyHint", "read_only_hint") is True
    assert hint("send_email", "readOnlyHint", "read_only_hint") is False
    assert hint("send_email", "destructiveHint", "destructive_hint") is False
    assert hint("send_email", "openWorldHint", "open_world_hint") is True
    assert hint("send_email", "idempotentHint", "idempotent_hint") is False
    assert hint("cancel_scheduled", "destructiveHint", "destructive_hint") is True
    assert hint("triage_apply", "destructiveHint", "destructive_hint") is True
    assert hint("mailbox_delete", "destructiveHint", "destructive_hint") is True
    assert hint("mailbox_create", "idempotentHint", "idempotent_hint") is True
    assert hint("triage_plan", "openWorldHint", "open_world_hint") is False


def test_advertised_input_schemas_expose_existing_bounds_and_vocabularies(
    monkeypatch,
):
    tools = _tools(monkeypatch)

    search = sdk_attr(tools["search_emails"], "inputSchema", "input_schema")["properties"]
    assert search["limit"]["minimum"] == 1
    assert search["limit"]["maximum"] == 500
    assert search["offset"]["minimum"] == 0

    view = _string_branch(
        sdk_attr(tools["get_email"], "inputSchema", "input_schema")["properties"]["view"]
    )
    assert view["enum"] == ["full", "metadata", "minimal"]
    ids = sdk_attr(
        tools["get_emails_batch"], "inputSchema", "input_schema",
    )["properties"]["ids"]
    assert ids["maxItems"] == 50

    refresh = sdk_attr(
        tools["refresh_mail"], "inputSchema", "input_schema",
    )["properties"]
    assert refresh["wait_seconds"]["minimum"] == 0
    assert refresh["wait_seconds"]["maximum"] == 60
    assert refresh["timeout_seconds"]["minimum"] == 1
    assert refresh["timeout_seconds"]["maximum"] == 120

    state = _string_branch(
        sdk_attr(
            tools["list_scheduled"], "inputSchema", "input_schema",
        )["properties"]["state"]
    )
    assert state["enum"] == [
        "pending", "sending", "sent", "failed", "cancelled",
    ]


def test_all_tools_declare_the_same_envelope_and_their_success_shape(
    monkeypatch,
):
    tools = _tools(monkeypatch)
    for name, tool in tools.items():
        schema = sdk_attr(tool, "outputSchema", "output_schema")
        assert schema is not None, f"{name} has no structured output schema"
        assert schema["type"] == "object"
        assert len(schema["anyOf"]) == 2
        success, failure = schema["anyOf"]
        assert "ok" in success["required"]
        assert failure["required"] == ["ok", "code", "error"]
        assert failure["properties"]["ok"] == {"const": False}

    assert "mailboxes" in \
        sdk_attr(tools["list_mailboxes"], "outputSchema", "output_schema")["anyOf"][0]["properties"]
    assert "email" in \
        sdk_attr(tools["get_email"], "outputSchema", "output_schema")["anyOf"][0]["properties"]
    assert "events" in \
        sdk_attr(tools["audit"], "outputSchema", "output_schema")["anyOf"][0]["properties"]


def test_every_advertised_input_and_output_is_valid_json_schema(monkeypatch):
    for tool in _tools(monkeypatch).values():
        Draft202012Validator.check_schema(
            sdk_attr(tool, "inputSchema", "input_schema")
        )
        Draft202012Validator.check_schema(
            sdk_attr(tool, "outputSchema", "output_schema")
        )


def test_call_result_keeps_legacy_text_and_adds_identical_structured_content(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    mcp = server._build_mcp_server()

    result = asyncio.run(mcp.call_tool("audit", {"since": "not-a-date"}))

    assert sdk_attr(result, "isError", "is_error") is False
    structured = sdk_attr(result, "structuredContent", "structured_content")
    assert structured is not None
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == structured
    assert structured["ok"] is False
    assert structured["code"] == "invalid_input"
    audit_tool = next(tool for tool in asyncio.run(mcp.list_tools())
                      if tool.name == "audit")
    Draft202012Validator(
        sdk_attr(audit_tool, "outputSchema", "output_schema")
    ).validate(structured)
