"""MCP and command-line inbound adapters for the email application.

All email decisions live in :mod:`email_mcp.application`. This module owns
only protocol registration, CLI argument handling, and presentation.
"""
from __future__ import annotations

import argparse
import json
import sys

from .application.models import (
    AttachmentOut,
    AuditPage,
    BatchItemError,
    CancelReceipt,
    EmailBatch,
    EmailMetadata,
    EmailMinimal,
    MailboxList,
    OneEmail,
    PlanMessageOut,
    PlanReceipt,
    RecentPage,
    RefreshOutcome,
    SearchHit,
    SearchPage,
    Thread,
)
from .bootstrap import get_application
from .domain.errors import InvalidInput, MailUnavailable, NotFound, ToolError
from .domain.mail import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    EmailSource,
    Mailbox,
    SearchQuery,
)
from .domain.models import (
    DraftResult,
    PlanAction,
    ScheduledEntry,
    SendResult,
)
from .mcp_api import (
    tool_audit,
    tool_cancel_scheduled,
    tool_create_draft,
    tool_doctor,
    tool_get_attachment,
    tool_get_email,
    tool_get_emails_batch,
    tool_get_thread,
    tool_list_mailboxes,
    tool_list_recent,
    tool_list_scheduled,
    tool_mailbox_create,
    tool_mailbox_delete,
    tool_refresh_mail,
    tool_reply_email,
    tool_schedule_email,
    tool_search_emails,
    tool_send_email,
    tool_triage_apply,
    tool_triage_plan,
    tool_triage_plan_delete,
)
from .transports import SendError

_READ_TOOLS = (
    tool_search_emails,
    tool_get_email,
    tool_get_emails_batch,
    tool_get_thread,
    tool_list_mailboxes,
    tool_list_recent,
    tool_get_attachment,
    tool_refresh_mail,
    tool_list_scheduled,
    tool_doctor,
    tool_audit,
)

_MUTATING_TOOLS = (
    tool_send_email,
    tool_create_draft,
    tool_reply_email,
    tool_cancel_scheduled,
    tool_triage_plan,
    tool_triage_plan_delete,
    tool_triage_apply,
    tool_mailbox_create,
    tool_mailbox_delete,
)


def _schedule_for_mcp(
    to: str,
    subject: str,
    body: str,
    send_at: str,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> dict:
    """Add the MCP-only dispatcher readiness hint to a stable result."""
    result = tool_schedule_email(
        to=to,
        subject=subject,
        body=body,
        send_at=send_at,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        from_identity=from_identity,
    )
    if result.get("ok"):
        if not get_application().dispatcher_installed():
            result["warning"] = (
                "dispatcher launchd agent NOT installed — nothing will send. "
                "Run: email-mcp dispatcher --install-launchd"
            )
    return result


_schedule_for_mcp.__name__ = "schedule_email"


def _build_mcp_server():
    """Build the full 21-tool server, or the 11-tool read-only surface."""
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # type: ignore

    from .config import read_only
    from .mcp_compat import enrich_input_schemas, register_tool

    mcp = FastMCP("apple-mail")
    for function in _READ_TOOLS:
        register_tool(mcp, function, function)
    if not read_only():
        for function in _MUTATING_TOOLS:
            register_tool(mcp, function, function)
        register_tool(mcp, _schedule_for_mcp, tool_schedule_email)
    enrich_input_schemas(mcp)
    return mcp


def _selftest() -> int:
    source = get_application().source
    mailboxes = source.mailboxes()
    recent = source.recent(None, None, 1)
    result = {
        "mailboxes": len(mailboxes),
        "newest_subject": recent[0].subject if recent else None,
        "newest_from": recent[0].from_addr if recent else None,
        "newest_date": recent[0].date.isoformat() if recent else None,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _refresh_test(wait_seconds: float = 5.0) -> int:
    """End-to-end exercise of refresh_mail against the real Mail.app."""
    result = tool_refresh_mail(wait_seconds=wait_seconds)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def _send_test(
    to: str,
    subject: str,
    body: str,
    attach: list[str] | None = None,
    from_identity: str | None = None,
) -> int:
    """End-to-end exercise of send_email against the real delivery path."""
    result = tool_send_email(
        to=to,
        subject=subject,
        body=body,
        attachments=attach,
        from_identity=from_identity,
    )
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def _transport_check() -> int:
    """Deprecated compatibility view of the doctor's transport checks."""
    check = get_application().transport_check().to_wire()
    json.dump(check, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if check.get("ok") else 1


def _doctor() -> int:
    """Run every diagnostic check and print the full JSON report."""
    report = tool_doctor()
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_mcp.server")
    parser.add_argument(
        "--selftest", action="store_true",
        help="Print a smoke-check summary against the real Mail dir and exit.",
    )
    parser.add_argument(
        "--refresh-test", action="store_true",
        help="Call refresh_mail() against the real Mail.app and print the result.",
    )
    parser.add_argument(
        "--refresh-wait", type=float, default=5.0,
        help="Seconds to wait after nudging Mail.app (default 5).",
    )
    parser.add_argument(
        "--send-test", metavar="TO",
        help="Send a test email to TO via the real delivery path and print the "
             "result. Subject/body are canned unless --subject/--body given.",
    )
    parser.add_argument("--subject", default="email-mcp send self-test")
    parser.add_argument(
        "--body",
        default="This is a send_email self-test.\n\nSecond paragraph.",
    )
    parser.add_argument(
        "--attach", action="append", default=None, metavar="PATH",
        help="Attach a file to the --send-test message (repeatable).",
    )
    parser.add_argument(
        "--from-identity", default=None, metavar="NAME",
        help="Identity to send --send-test as (from ~/.email-mcp/"
             "identities.toml; default: the file's default identity).",
    )
    parser.add_argument(
        "--doctor", action="store_true",
        help="Run every diagnostic check (permissions, identities, "
             "transports, dispatcher, spool, body index), print one JSON "
             "report, and exit 0 only when all checks are ok.",
    )
    parser.add_argument(
        "--transport-check", action="store_true",
        help="DEPRECATED — alias for the doctor's transports check; prints "
             "only that section. Prefer --doctor for the full picture.",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.refresh_test:
        return _refresh_test(wait_seconds=args.refresh_wait)
    if args.doctor:
        return _doctor()
    if args.transport_check:
        return _transport_check()
    if args.send_test:
        return _send_test(
            args.send_test,
            args.subject,
            args.body,
            args.attach,
            from_identity=args.from_identity,
        )
    _build_mcp_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
