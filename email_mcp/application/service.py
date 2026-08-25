"""Explicit facade over independently composed application capabilities."""
from __future__ import annotations

from datetime import datetime

from ..domain.mail import EmailSource
from ..domain.models import DraftResult, ScheduledEntry, SendResult
from .background import BackgroundUseCases
from .delivery import DeliveryUseCases
from .models import (
    AttachmentOut,
    AuditPage,
    CancelReceipt,
    DispatchSummary,
    DoctorReport,
    EmailBatch,
    MailboxCreateResult,
    MailboxDeleteResult,
    MailboxList,
    OneEmail,
    PlanReceipt,
    RecentPage,
    RefreshOutcome,
    ScheduleListing,
    SearchPage,
    Thread,
    TransportReport,
    TriageApplyResult,
)
from .operations import OperationsUseCases
from .reads import ReadUseCases
from .scheduling import SchedulingUseCases
from .triage import TriageUseCases


class EmailApplication:
    """Stable inbound API; each method delegates to one cohesive capability."""

    def __init__(
        self,
        *,
        reads: ReadUseCases,
        delivery: DeliveryUseCases,
        scheduling: SchedulingUseCases,
        triage: TriageUseCases,
        operations: OperationsUseCases,
        background: BackgroundUseCases,
    ) -> None:
        self._reads = reads
        self._delivery = delivery
        self._scheduling = scheduling
        self._triage = triage
        self._operations = operations
        self._background = background

    @property
    def source(self) -> EmailSource:
        return self._reads.source

    def search_emails(
        self,
        query: str = "",
        from_addr: str | None = None,
        to_addr: str | None = None,
        mailbox: str | None = None,
        account: str | None = None,
        before: str | None = None,
        after: str | None = None,
        has_attachment: bool | None = None,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchPage:
        return self._reads.search_emails(
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
            limit=limit, offset=offset,
        )

    def get_email(self, id: str, view: str = "full") -> OneEmail:
        return self._reads.get_email(id, view)

    def get_emails_batch(
        self, ids: list[str], view: str = "full",
    ) -> EmailBatch:
        return self._reads.get_emails_batch(ids, view)

    def get_thread(self, thread_id: str) -> Thread:
        return self._reads.get_thread(thread_id)

    def list_mailboxes(self) -> MailboxList:
        return self._reads.list_mailboxes()

    def list_recent(
        self,
        mailbox: str | None = None,
        account: str | None = None,
        limit: int = 50,
    ) -> RecentPage:
        return self._reads.list_recent(mailbox, account, limit)

    def get_attachment(self, id: str, attachment_id: str) -> AttachmentOut:
        return self._reads.get_attachment(id, attachment_id)

    def refresh_mail(
        self,
        wait_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
    ) -> RefreshOutcome:
        return self._reads.refresh_mail(wait_seconds, timeout_seconds)

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] | None = None,
        from_identity: str | None = None,
    ) -> SendResult:
        return self._delivery.send_email(
            to, subject, body, cc, bcc, attachments, from_identity,
        )

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        in_reply_to: str = "",
        from_identity: str | None = None,
    ) -> DraftResult:
        return self._delivery.create_draft(
            to, subject, body, cc, in_reply_to, from_identity,
        )

    def reply_email(
        self,
        id: str,
        body: str,
        reply_all: bool = False,
        cc: str | None = None,
        bcc: str | None = None,
        include_history: bool = True,
        attachments: list[str] | None = None,
        from_identity: str | None = None,
    ) -> SendResult:
        return self._delivery.reply_email(
            id, body, reply_all, cc, bcc, include_history, attachments,
            from_identity,
        )

    def schedule_email(
        self,
        to: str,
        subject: str,
        body: str,
        send_at: str,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] | None = None,
        from_identity: str | None = None,
    ) -> ScheduledEntry:
        return self._delivery.schedule_email(
            to, subject, body, send_at, cc, bcc, attachments, from_identity,
        )

    def list_scheduled(
        self, state: str | None = None, limit: int = 50,
    ) -> ScheduleListing:
        return self._scheduling.list_scheduled(state, limit)

    def dispatcher_installed(self) -> bool:
        return self._scheduling.dispatcher_installed()

    def cancel_scheduled(self, id: str) -> CancelReceipt:
        return self._scheduling.cancel_scheduled(id)

    def triage_plan(
        self,
        *,
        query: str = "",
        from_addr: str | None = None,
        to_addr: str | None = None,
        mailbox: str | None = None,
        account: str | None = None,
        before: str | None = None,
        after: str | None = None,
        has_attachment: bool | None = None,
        unread_only: bool = False,
        limit: int = 0,
        actions: list[dict] | None = None,
    ) -> PlanReceipt:
        return self._triage.triage_plan(
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
            limit=limit, actions=actions,
        )

    def triage_plan_delete(
        self,
        *,
        query: str = "",
        from_addr: str | None = None,
        to_addr: str | None = None,
        mailbox: str | None = None,
        account: str | None = None,
        before: str | None = None,
        after: str | None = None,
        has_attachment: bool | None = None,
        unread_only: bool = False,
        limit: int = 0,
    ) -> PlanReceipt:
        return self._triage.triage_plan_delete(
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
            limit=limit,
        )

    def triage_apply(self, plan_id: str) -> TriageApplyResult:
        return self._triage.triage_apply(plan_id)

    def mailbox_create(
        self, account: str, path: str,
    ) -> MailboxCreateResult:
        return self._triage.mailbox_create(account, path)

    def mailbox_delete(
        self, account: str, path: str,
    ) -> MailboxDeleteResult:
        return self._triage.mailbox_delete(account, path)

    def doctor(self) -> DoctorReport:
        return self._operations.doctor()

    def transport_check(self) -> TransportReport:
        return self._operations.transport_check()

    def audit(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        tool: str | None = None,
        event: str | None = None,
        plan_id: str | None = None,
        operation_id: str | None = None,
        limit: int = 50,
    ) -> AuditPage:
        return self._operations.audit(
            since=since, until=until, tool=tool, event=event,
            plan_id=plan_id, operation_id=operation_id, limit=limit,
        )

    def dispatch_scheduled(
        self, now: datetime | None = None,
    ) -> DispatchSummary:
        return self._background.dispatch_scheduled(now)

    def _fail_or_retry(self, *args, **kwargs):
        return self._background._fail_or_retry(*args, **kwargs)

    def recover_stranded(self, now: datetime) -> list[str]:
        return self._background.recover_stranded(now)

    def graph_current(self, entry: ScheduledEntry) -> bool:
        return self._background.graph_current(entry)

    def graph_mark_sent(self, entry: ScheduledEntry, now: datetime) -> str:
        return self._background.graph_mark_sent(entry, now)

    def graph_adopt(self, entry: ScheduledEntry, draft_id: str) -> str:
        return self._background.graph_adopt(entry, draft_id)

    def graph_flip_to_local(
        self, entry: ScheduledEntry, now: datetime, reason: str,
        clear_draft: bool,
    ) -> str:
        return self._background.graph_flip_to_local(
            entry, now, reason, clear_draft,
        )

    def graph_leave(
        self, entry: ScheduledEntry, error: str, note: str,
    ) -> str:
        return self._background.graph_leave(entry, error, note)

    def graph_apply_status(
        self, entry: ScheduledEntry, status: str, now: datetime,
    ) -> str:
        return self._background.graph_apply_status(entry, status, now)

    def reconcile_deferred(self, now: datetime) -> dict[str, str]:
        return self._background.reconcile_deferred(now)


__all__ = ["EmailApplication"]
