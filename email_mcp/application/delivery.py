"""Immediate delivery, drafts, replies, and schedule creation use cases."""
from __future__ import annotations

from ..domain.errors import ToolError
from ..domain.models import DraftResult, ScheduledEntry, SendResult
from .base import ApplicationService


class DeliveryUseCases(ApplicationService):
    def _failed_delivery(
        self,
        name: str,
        tool: str,
        error: ToolError,
        *,
        subject: str | None = None,
        detail: dict | None = None,
    ) -> None:
        failure_detail = dict(detail or {})
        failure_detail["error"] = str(error)[:300]
        self._event(
            name, "failed", tool=tool, subject=subject, detail=failure_detail,
        )

    def _sent(
        self,
        name: str,
        tool: str,
        result: SendResult,
        identity: str | None,
        *,
        detail: dict | None = None,
    ) -> None:
        self._event(
            name, "sent", tool=tool,
            message_id=result.message_id,
            identity=identity,
            to=result.to,
            cc=result.cc or None,
            bcc=result.bcc or None,
            subject=result.subject,
            detail=detail,
        )

    def send_email(self, **values) -> SendResult:
        try:
            result = self._deps.delivery.send(**values)
        except ToolError as error:
            self._failed_delivery(
                "send", "send_email", error, subject=values.get("subject")
            )
            raise
        self._sent(
            "send", "send_email", result, values.get("from_identity"),
            detail=({"attachments": result.attachments}
                    if result.attachments else None),
        )
        return result

    def create_draft(self, **values) -> DraftResult:
        try:
            result = self._deps.delivery.create_draft(**values)
        except ToolError as error:
            self._failed_delivery(
                "draft", "create_draft", error, subject=values.get("subject")
            )
            raise
        self._event(
            "draft", "created", tool="create_draft",
            message_id=result.message_id,
            identity=values.get("from_identity"),
            to=result.to,
            cc=result.cc or None,
            subject=result.subject,
            detail={"draft_id": result.draft_id, "account": result.account},
        )
        return result

    def reply_email(self, **values) -> SendResult:
        original_id = values["id"]
        try:
            result = self._deps.delivery.reply(self.source, **values)
        except ToolError as error:
            self._failed_delivery(
                "reply", "reply_email", error,
                detail={
                    "orig_id": original_id,
                    "reply_all": values.get("reply_all", False),
                },
            )
            raise
        self._sent(
            "reply", "reply_email", result, values.get("from_identity"),
            detail={
                "orig_id": original_id,
                "reply_all": values.get("reply_all", False),
            },
        )
        return result

    def schedule_email(self, **values) -> ScheduledEntry:
        try:
            entry = self._deps.delivery.schedule(**values)
        except ToolError as error:
            self._failed_delivery(
                "schedule", "schedule_email", error,
                subject=values.get("subject"),
            )
            raise
        requested = (
            self._deps.delivery.requested_executor(entry.identity)
            if entry.executor == "launchd" else None
        )
        self._event(
            "schedule", "scheduled", operation_id=entry.id,
            tool="schedule_email", spool_id=entry.id,
            message_id=entry.message_id, identity=entry.identity,
            to=entry.to, cc=entry.cc or None, bcc=entry.bcc or None,
            subject=entry.subject,
            detail={
                "executor": entry.executor,
                "send_at": entry.send_at,
                "draft_id": entry.graph_draft_id,
                "graph_fallback": requested == "graph",
            },
        )
        return entry
