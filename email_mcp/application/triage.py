"""Reviewed triage-plan and mailbox-management use cases."""
from __future__ import annotations

from ..domain.events import EventPublisher
from ..domain.mail import EmailSource
from ..domain.mail import SearchQuery
from .base import ApplicationService
from .models import (
    MailboxCreateResult,
    MailboxDeleteResult,
    PlanMessageOut,
    PlanReceipt,
    TriageApplyResult,
)
from .ports import SourceProvider, TriageGateway
from .query import search_query


class TriageUseCases(ApplicationService):
    def __init__(
        self,
        *,
        source: SourceProvider,
        triage: TriageGateway,
        events: EventPublisher,
    ) -> None:
        super().__init__(events)
        self._source = source
        self._triage = triage

    @property
    def source(self) -> EmailSource:
        return self._source.get()

    @staticmethod
    def _plan_query(*, cap: int, limit: int = 0, **values) -> SearchQuery:
        return search_query(
            **values,
            limit=limit if 0 < limit <= cap else cap + 1,
            offset=0,
        )

    @staticmethod
    def _plan_receipt(plan) -> PlanReceipt:
        return PlanReceipt(
            plan_id=plan.id,
            count=len(plan.messages),
            expires_at=plan.expires_at,
            summary=plan.summary,
            actions=list(plan.actions),
            messages=[
                PlanMessageOut(
                    id=str(message.rowid),
                    subject=message.subject,
                    from_addr=message.from_addr,
                    date=message.date,
                    mailbox=message.mailbox,
                    unread=message.unread,
                )
                for message in plan.messages
            ],
        )

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
        query = self._plan_query(
            cap=self._triage.plan_cap(), limit=limit,
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
        )
        return self._plan_receipt(
            self._triage.build(self.source, query, actions)
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
        query = self._plan_query(
            cap=self._triage.delete_cap(), limit=limit,
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
        )
        return self._plan_receipt(
            self._triage.build_delete(self.source, query)
        )

    def triage_apply(self, plan_id: str) -> TriageApplyResult:
        return self._triage.apply(self.source, plan_id)

    def mailbox_create(self, account: str, path: str) -> MailboxCreateResult:
        result = self._triage.create_mailbox(self.source, account, path)
        if result.existed is False:
            self._event(
                "mailbox_create", "created", tool="mailbox_create",
                account=account, mailbox=path,
            )
        return result

    def mailbox_delete(self, account: str, path: str) -> MailboxDeleteResult:
        result = self._triage.delete_mailbox(self.source, account, path)
        if result.existed:
            outcome = (
                "deleted" if result.deleted
                else result.code or "delete_failed"
            )
            self._event(
                "mailbox_delete", outcome, tool="mailbox_delete",
                account=account, mailbox=path,
                detail=({"method": result.method}
                        if result.method else None),
            )
        return result
