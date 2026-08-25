"""Thin MCP-facing functions over the application use cases."""
from __future__ import annotations

from . import envelope
from .application.models import (
    AttachmentOut,
    AuditPage,
    CancelReceipt,
    EmailBatch,
    MailboxList,
    OneEmail,
    PlanReceipt,
    RecentPage,
    RefreshOutcome,
    SearchPage,
    Thread,
)
from .bootstrap import get_application
from .domain.models import DraftResult, ScheduledEntry, SendResult


@envelope.tool
def tool_search_emails(
    query: str = "", from_addr: str | None = None,
    to_addr: str | None = None, mailbox: str | None = None,
    account: str | None = None, before: str | None = None,
    after: str | None = None, has_attachment: bool | None = None,
    unread_only: bool = False, limit: int = 50, offset: int = 0,
) -> SearchPage:
    return get_application().search_emails(
        query=query, from_addr=from_addr, to_addr=to_addr,
        mailbox=mailbox, account=account, before=before, after=after,
        has_attachment=has_attachment, unread_only=unread_only,
        limit=limit, offset=offset,
    )


@envelope.tool
def tool_get_email(id: str, view: str = "full") -> OneEmail:
    return get_application().get_email(id=id, view=view)


@envelope.tool
def tool_get_emails_batch(ids: list[str], view: str = "full") -> EmailBatch:
    return get_application().get_emails_batch(ids=ids, view=view)


@envelope.tool
def tool_get_thread(thread_id: str) -> Thread:
    return get_application().get_thread(thread_id=thread_id)


@envelope.tool
def tool_list_mailboxes() -> MailboxList:
    return get_application().list_mailboxes()


@envelope.tool
def tool_list_recent(
    mailbox: str | None = None, account: str | None = None,
    limit: int = 50,
) -> RecentPage:
    return get_application().list_recent(
        mailbox=mailbox, account=account, limit=limit,
    )


@envelope.tool
def tool_get_attachment(id: str, attachment_id: str) -> AttachmentOut:
    return get_application().get_attachment(id=id, attachment_id=attachment_id)


@envelope.tool
def tool_refresh_mail(
    wait_seconds: float = 5.0, timeout_seconds: float = 30.0,
) -> RefreshOutcome:
    return get_application().refresh_mail(
        wait_seconds=wait_seconds, timeout_seconds=timeout_seconds,
    )


@envelope.tool
def tool_send_email(
    to: str, subject: str, body: str, cc: str | None = None,
    bcc: str | None = None, attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> SendResult:
    return get_application().send_email(
        to=to, subject=subject, body=body, cc=cc, bcc=bcc,
        attachments=attachments, from_identity=from_identity,
    )


@envelope.tool
def tool_create_draft(
    to: str, subject: str, body: str, cc: str | None = None,
    in_reply_to: str = "", from_identity: str | None = None,
) -> DraftResult:
    return get_application().create_draft(
        to=to, subject=subject, body=body, cc=cc,
        in_reply_to=in_reply_to, from_identity=from_identity,
    )


@envelope.tool
def tool_reply_email(
    id: str, body: str, reply_all: bool = False,
    cc: str | None = None, bcc: str | None = None,
    include_history: bool = True, attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> SendResult:
    return get_application().reply_email(
        id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
        include_history=include_history, attachments=attachments,
        from_identity=from_identity,
    )


@envelope.tool
def tool_schedule_email(
    to: str, subject: str, body: str, send_at: str,
    cc: str | None = None, bcc: str | None = None,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> ScheduledEntry:
    return get_application().schedule_email(
        to=to, subject=subject, body=body, send_at=send_at,
        cc=cc, bcc=bcc, attachments=attachments,
        from_identity=from_identity,
    )


@envelope.tool
def tool_list_scheduled(state: str | None = None, limit: int = 50) -> dict:
    return get_application().list_scheduled(
        state=state, limit=limit,
    ).to_wire()


@envelope.tool
def tool_triage_plan(
    query: str = "", from_addr: str | None = None,
    to_addr: str | None = None, mailbox: str | None = None,
    account: str | None = None, before: str | None = None,
    after: str | None = None, has_attachment: bool | None = None,
    unread_only: bool = False, limit: int = 0,
    actions: list[dict] | None = None,
) -> PlanReceipt:
    return get_application().triage_plan(
        query=query, from_addr=from_addr, to_addr=to_addr,
        mailbox=mailbox, account=account, before=before, after=after,
        has_attachment=has_attachment, unread_only=unread_only,
        limit=limit, actions=actions,
    )


@envelope.tool
def tool_triage_plan_delete(
    query: str = "", from_addr: str | None = None,
    to_addr: str | None = None, mailbox: str | None = None,
    account: str | None = None, before: str | None = None,
    after: str | None = None, has_attachment: bool | None = None,
    unread_only: bool = False, limit: int = 0,
) -> PlanReceipt:
    return get_application().triage_plan_delete(
        query=query, from_addr=from_addr, to_addr=to_addr,
        mailbox=mailbox, account=account, before=before, after=after,
        has_attachment=has_attachment, unread_only=unread_only,
        limit=limit,
    )


@envelope.tool(op_from="plan_id")
def tool_triage_apply(plan_id: str) -> dict:
    return get_application().triage_apply(plan_id=plan_id).to_wire()


@envelope.tool
def tool_mailbox_create(account: str, path: str) -> dict:
    return get_application().mailbox_create(
        account=account, path=path,
    ).to_wire()


@envelope.tool
def tool_mailbox_delete(account: str, path: str) -> dict:
    return get_application().mailbox_delete(
        account=account, path=path,
    ).to_wire()


@envelope.tool
def tool_cancel_scheduled(id: str) -> CancelReceipt:
    return get_application().cancel_scheduled(id=id)


@envelope.tool
def tool_doctor() -> dict:
    return get_application().doctor().to_wire()


@envelope.tool
def tool_audit(
    since: str | None = None, until: str | None = None,
    tool: str | None = None, event: str | None = None,
    plan_id: str | None = None, operation_id: str | None = None,
    limit: int = 50,
) -> AuditPage:
    return get_application().audit(
        since=since, until=until, tool=tool, event=event,
        plan_id=plan_id, operation_id=operation_id, limit=limit,
    )
