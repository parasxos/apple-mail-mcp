"""Reviewed triage-plan and mailbox-management use cases."""
from __future__ import annotations

from ..domain.mail import SearchQuery
from .base import ApplicationService
from .models import PlanMessageOut, PlanReceipt
from .query import search_query


class TriageUseCases(ApplicationService):
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
        limit: int = 0,
        actions: list[dict] | None = None,
        **values,
    ) -> PlanReceipt:
        query = self._plan_query(
            cap=self._deps.triage.plan_cap(), limit=limit, **values,
        )
        return self._plan_receipt(
            self._deps.triage.build(self.source, query, actions)
        )

    def triage_plan_delete(
        self,
        *,
        limit: int = 0,
        **values,
    ) -> PlanReceipt:
        query = self._plan_query(
            cap=self._deps.triage.delete_cap(), limit=limit, **values,
        )
        return self._plan_receipt(
            self._deps.triage.build_delete(self.source, query)
        )

    def triage_apply(self, plan_id: str) -> dict:
        return self._deps.triage.apply(self.source, plan_id)

    def mailbox_create(self, account: str, path: str) -> dict:
        result = self._deps.triage.create_mailbox(self.source, account, path)
        if result.get("existed") is False:
            self._event(
                "mailbox_create", "created", tool="mailbox_create",
                account=account, mailbox=path,
            )
        return result

    def mailbox_delete(self, account: str, path: str) -> dict:
        result = self._deps.triage.delete_mailbox(self.source, account, path)
        if result.get("existed"):
            outcome = (
                "deleted" if result.get("deleted")
                else result.get("code", "delete_failed")
            )
            self._event(
                "mailbox_delete", outcome, tool="mailbox_delete",
                account=account, mailbox=path,
                detail=({"method": result["method"]}
                        if result.get("method") else None),
            )
        return result
