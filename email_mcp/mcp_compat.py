"""MCP-facing metadata, JSON Schemas, and structured result adaptation.

The core ``tool_*`` functions intentionally keep returning the v1 envelope
dict.  This module adapts that stable contract to newer MCP capabilities:

* human titles and complete ToolAnnotations;
* descriptive, bounded input schemas without changing envelope-level errors;
* precise success/failure output schemas; and
* CallToolResult with both legacy JSON text and identical structuredContent.

Keeping the adapter at registration time lets old clients continue parsing the
text payload while newer clients can reason over machine-readable metadata.
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import types
import typing
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class ToolSpec:
    title: str
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool

    def annotations(self) -> ToolAnnotations:
        return ToolAnnotations(
            title=self.title,
            readOnlyHint=self.read_only,
            destructiveHint=self.destructive,
            idempotentHint=self.idempotent,
            openWorldHint=self.open_world,
        )


# The flags describe observable email/state effects, not incidental cache
# refreshes.  openWorldHint is true when the call can reach Mail.app or a mail
# provider rather than remaining within the MCP's local data.
TOOL_SPECS: dict[str, ToolSpec] = {
    "search_emails": ToolSpec("Search emails", True, False, True, False),
    "get_email": ToolSpec("Read an email", True, False, True, False),
    "get_emails_batch": ToolSpec(
        "Read multiple emails", True, False, True, False,
    ),
    "get_thread": ToolSpec("Read an email thread", True, False, True, False),
    "list_mailboxes": ToolSpec("List mailboxes", True, False, True, False),
    "list_recent": ToolSpec("List recent emails", True, False, True, False),
    "get_attachment": ToolSpec(
        "Save an attachment for reading", True, False, True, False,
    ),
    "refresh_mail": ToolSpec("Refresh mail", True, False, True, True),
    "list_scheduled": ToolSpec(
        "List scheduled emails", True, False, True, False,
    ),
    "doctor": ToolSpec("Check email setup", True, False, True, False),
    "audit": ToolSpec("Read the activity history", True, False, True, False),
    "send_email": ToolSpec("Send an email", False, False, False, True),
    "create_draft": ToolSpec("Create an email draft", False, False, False, True),
    "reply_email": ToolSpec("Reply to an email", False, False, False, True),
    "schedule_email": ToolSpec(
        "Schedule an email", False, False, False, True,
    ),
    "cancel_scheduled": ToolSpec(
        "Cancel a scheduled email", False, True, True, True,
    ),
    "triage_plan": ToolSpec(
        "Prepare mailbox changes", False, False, False, False,
    ),
    "triage_plan_delete": ToolSpec(
        "Prepare email deletion", False, False, False, False,
    ),
    "triage_apply": ToolSpec(
        "Apply reviewed mailbox changes", False, True, True, True,
    ),
    "mailbox_create": ToolSpec("Create a mailbox", False, False, True, True),
    "mailbox_delete": ToolSpec("Delete a mailbox", False, True, True, True),
}


_PARAMETER_HELP = {
    "query": "Words to find in the subject, sender, snippet, or indexed body.",
    "from_addr": "Sender name, address, or address fragment to match.",
    "to_addr": "Recipient name, address, or address fragment to match.",
    "mailbox": "Mailbox name to search, such as INBOX or Archive.",
    "account": "Account UUID; omit it to include every configured account.",
    "before": "Only include mail before this ISO-8601 date or timestamp.",
    "after": "Only include mail after this ISO-8601 date or timestamp.",
    "has_attachment": "True for mail with attachments, false for mail without them.",
    "unread_only": "When true, include only unread messages.",
    "limit": "Maximum number of results to return.",
    "offset": "Number of matching results to skip for pagination.",
    "view": "Payload size: full, metadata without bodies, or minimal.",
    "ids": "Envelope IDs returned by search, recent, or thread tools.",
    "id": "Identifier returned by an earlier email tool call.",
    "thread_id": "Conversation ID returned on an email reference.",
    "attachment_id": "Attachment part ID returned by get_email.",
    "wait_seconds": "Seconds to wait after Mail.app starts refreshing.",
    "timeout_seconds": "Maximum seconds allowed for the Mail.app refresh request.",
    "state": "Scheduled-mail state to return; omit it to include all states.",
    "since": "Inclusive ISO-8601 start bound; calendar prefixes are accepted.",
    "until": "Inclusive ISO-8601 end bound; calendar prefixes are accepted.",
    "tool": "Only return activity emitted by this tool name.",
    "event": "Only return activity with this event name.",
    "plan_id": "Triage plan ID returned by a planning tool.",
    "operation_id": "Operation ID that joins related activity across processes.",
    "to": "Comma-separated primary recipients, including optional display names.",
    "subject": "Email subject line.",
    "body": "Plain-text email content; paragraph breaks are preserved.",
    "cc": "Optional comma-separated Cc recipients.",
    "bcc": "Optional comma-separated Bcc recipients.",
    "attachments": "Optional list of local file paths, one path per item.",
    "from_identity": "Configured sending identity; omit it to use the default.",
    "in_reply_to": "Optional Message-ID used to thread the draft as a reply.",
    "reply_all": "Also copy the original To and Cc recipients, excluding yourself.",
    "include_history": "Quote the original message below the new reply.",
    "send_at": "Delivery time in ISO-8601; a timestamp without an offset is local time.",
    "actions": "Mailbox actions to stage for every selected message.",
    "path": "Mailbox path within the account; slashes create nested folders.",
}


_PARAMETER_OVERRIDES = {
    ("get_email", "id"): "Envelope ID returned by search, recent, or thread tools.",
    ("get_attachment", "id"): "Envelope ID of the email containing the attachment.",
    ("reply_email", "id"): "Envelope ID of the email being answered.",
    ("cancel_scheduled", "id"): "Scheduled-email ID returned when it was created.",
    ("triage_plan_delete", "from_addr"): (
        "Exact sender email address to match; fragments never select mail."
    ),
    ("triage_plan", "limit"): (
        "Maximum messages to stage; 0 uses the configured safe plan cap."
    ),
    ("triage_plan_delete", "limit"): (
        "Maximum messages to stage; 0 uses the configured deletion cap."
    ),
    ("mailbox_create", "account"): "Account UUID that will own the mailbox.",
    ("mailbox_delete", "account"): "Account UUID that owns the mailbox.",
}


_VIEWS = ["full", "metadata", "minimal"]
_SCHEDULE_STATES = ["pending", "sending", "sent", "failed", "cancelled"]


_INPUT_RULES: dict[str, dict[str, tuple[str, dict]]] = {
    "search_emails": {
        "limit": ("integer", {"minimum": 1, "maximum": 500}),
        "offset": ("integer", {"minimum": 0}),
    },
    "get_email": {"view": ("string", {"enum": _VIEWS})},
    "get_emails_batch": {
        "ids": ("array", {"maxItems": 50}),
        "view": ("string", {"enum": _VIEWS}),
    },
    "list_recent": {
        "limit": ("integer", {"minimum": 1, "maximum": 500}),
    },
    "refresh_mail": {
        "wait_seconds": ("number", {"minimum": 0, "maximum": 60}),
        "timeout_seconds": ("number", {"minimum": 1, "maximum": 120}),
    },
    "list_scheduled": {
        "state": ("string", {"enum": _SCHEDULE_STATES}),
        "limit": ("integer", {"minimum": 1, "maximum": 500}),
    },
    "audit": {
        "limit": ("integer", {"minimum": 1, "maximum": 500}),
    },
    "triage_plan": {"limit": ("integer", {"minimum": 0})},
    "triage_plan_delete": {"limit": ("integer", {"minimum": 0})},
    "mailbox_create": {
        "account": ("string", {"minLength": 1}),
        "path": ("string", {"minLength": 1}),
    },
    "mailbox_delete": {
        "account": ("string", {"minLength": 1}),
        "path": ("string", {"minLength": 1}),
    },
}


_ACTION_ITEMS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["move_to", "mark_read", "mark_unread", "flag", "unflag"],
            "description": "Mailbox change to stage.",
        },
        "mailbox": {
            "type": ["string", "null"],
            "description": "Required destination path for move_to.",
        },
        "color": {
            "anyOf": [
                {"type": "integer", "minimum": 0, "maximum": 6},
                {"type": "null"},
            ],
            "description": "Required flag color (0 through 6) for flag.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


class _WireEnvelope(BaseModel):
    """Runtime validator: precise per-tool schemas are supplied separately."""

    model_config = ConfigDict(extra="allow")
    ok: bool


def _json_schema(tp: typing.Any) -> dict:
    """Inline JSON Schema for the dataclass-oriented tool return vocabulary."""
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is type(None):
        return {"type": "null"}
    if tp is datetime:
        return {"type": "string", "format": "date-time"}
    if tp is typing.Any:
        return {}

    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_json_schema(member)
                          for member in typing.get_args(tp)]}
    if origin is typing.Literal:
        values = list(typing.get_args(tp))
        schema: dict = {"enum": values}
        value_types = {type(value) for value in values}
        if len(value_types) == 1:
            schema.update(_json_schema(next(iter(value_types))))
        return schema
    if origin is list:
        return {"type": "array", "items": _json_schema(typing.get_args(tp)[0])}
    if origin is dict or tp is dict:
        args = typing.get_args(tp)
        values = _json_schema(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": values or True}
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        fields = dataclasses.fields(tp)
        return {
            "type": "object",
            "properties": {
                field.name: _json_schema(hints[field.name]) for field in fields
            },
            # Dataclass serialisation includes fields even when they have a
            # Python default, so they are required on the actual wire.
            "required": [field.name for field in fields],
            # Additive response keys remain compatible with the v1 contract.
            "additionalProperties": True,
        }
    raise TypeError(f"cannot derive MCP JSON Schema for {tp!r}")


def _success_schema(return_type: typing.Any) -> dict:
    payload = _json_schema(return_type)
    if payload.get("type") != "object" or "properties" not in payload:
        return {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": True,
        }

    properties = {"ok": {"const": True}, **payload["properties"]}
    required = ["ok", *[name for name in payload.get("required", [])
                         if name != "ok"]]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


def _output_schema(return_type: typing.Any) -> dict:
    failure = {
        "type": "object",
        "properties": {
            "ok": {"const": False},
            "code": {"type": "string"},
            "error": {"type": "string"},
            "fix": {"type": "string"},
            "operation_id": {"type": "string"},
        },
        "required": ["ok", "code", "error"],
        "additionalProperties": True,
    }
    # anyOf is intentional: health-reporting tools such as doctor and
    # refresh_mail can carry ok:false as data without being tool failures.
    return {
        "type": "object",
        "anyOf": [_success_schema(return_type), failure],
    }


@lru_cache(maxsize=None)
def _output_model(tool_name: str, return_type: typing.Any) -> type[_WireEnvelope]:
    schema = _output_schema(return_type)

    def _schema(cls, core_schema, handler):
        return copy.deepcopy(schema)

    class_name = "".join(part.title() for part in tool_name.split("_")) + "Result"
    return type(
        class_name,
        (_WireEnvelope,),
        {
            "__module__": __name__,
            "__get_pydantic_json_schema__": classmethod(_schema),
        },
    )


def _call_result(data: dict) -> CallToolResult:
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise TypeError("MCP tool broke the v1 envelope contract: want {ok: bool}")
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps(data, indent=2, ensure_ascii=False),
        )],
        structuredContent=data,
        # ok:false is a documented application envelope, not a JSON-RPC
        # failure.  Keeping isError false preserves existing client behavior.
        isError=False,
    )


def register_tool(mcp, function, implementation):
    """Register one nested server function with complete MCP metadata."""
    name = function.__name__
    try:
        spec = TOOL_SPECS[name]
    except KeyError as exc:
        raise RuntimeError(f"missing MCP metadata for tool {name!r}") from exc

    return_type = typing.get_type_hints(implementation)["return"]
    model = _output_model(name, return_type)
    wire_return = typing.Annotated[CallToolResult, model]

    signature = inspect.signature(function)
    hints = typing.get_type_hints(function)
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
        for parameter in signature.parameters.values()
    ]

    def wrapped(*args, **kwargs):
        return _call_result(function(*args, **kwargs))

    # Avoid __wrapped__: FastMCP intentionally follows it and would rediscover
    # the nested function's old -> dict annotation instead of CallToolResult.
    wrapped.__name__ = function.__name__
    wrapped.__qualname__ = function.__qualname__
    wrapped.__module__ = function.__module__
    wrapped.__doc__ = function.__doc__
    wrapped.__annotations__ = {**hints, "return": wire_return}
    wrapped.__signature__ = signature.replace(
        parameters=parameters, return_annotation=wire_return,
    )

    return mcp.tool(
        title=spec.title,
        annotations=spec.annotations(),
        structured_output=True,
    )(wrapped)


def _branch(schema: dict, wanted_type: str) -> dict:
    if schema.get("type") == wanted_type:
        return schema
    for candidate in schema.get("anyOf", []):
        if candidate.get("type") == wanted_type:
            return candidate
    raise RuntimeError(f"schema has no {wanted_type!r} branch: {schema!r}")


def enrich_input_schemas(mcp) -> None:
    """Add client guidance to FastMCP's generated schemas in one place.

    These are advertised constraints, matching validation already performed by
    the core functions.  The FastMCP argument model is deliberately unchanged,
    so an older or non-validating client still receives the stable ok:false
    envelope instead of a framework exception.
    """
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    unknown = set(registered) - set(TOOL_SPECS)
    if unknown:
        raise RuntimeError(f"registered tools lack MCP metadata: {sorted(unknown)}")

    for tool_name, tool in registered.items():
        properties = tool.parameters.get("properties", {})
        for parameter, schema in properties.items():
            description = _PARAMETER_OVERRIDES.get(
                (tool_name, parameter), _PARAMETER_HELP.get(parameter),
            )
            if not description:
                raise RuntimeError(
                    f"missing MCP parameter help for {tool_name}.{parameter}"
                )
            schema["description"] = description

        for parameter, (schema_type, additions) in \
                _INPUT_RULES.get(tool_name, {}).items():
            _branch(properties[parameter], schema_type).update(additions)

        if tool_name == "triage_plan":
            array = _branch(properties["actions"], "array")
            array["items"] = copy.deepcopy(_ACTION_ITEMS)

