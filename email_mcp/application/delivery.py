"""Immediate delivery, drafts, replies, and schedule creation use cases."""
from __future__ import annotations

from ..domain.errors import ToolError
from ..domain.events import EventPublisher
from ..domain.mail import EmailSource
from ..domain.models import (
    DraftRequest,
    DraftResult,
    ReplyRequest,
    ScheduleRequest,
    ScheduledEntry,
    SendRequest,
    SendResult,
)
from .base import ApplicationService
from .ports import DeliveryGateway, SourceProvider


class DeliveryUseCases(ApplicationService):
    def __init__(
        self,
        *,
        source: SourceProvider,
        delivery: DeliveryGateway,
        events: EventPublisher,
    ) -> None:
        super().__init__(events)
        self._source = source
        self._delivery = delivery

    @property
    def source(self) -> EmailSource:
        return self._source.get()

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
        request = SendRequest(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=tuple(attachments or ()),
            from_identity=from_identity,
        )
        try:
            result = self._delivery.send(request)
        except ToolError as error:
            self._failed_delivery(
                "send", "send_email", error, subject=subject,
            )
            raise
        self._sent(
            "send", "send_email", result, from_identity,
            detail=({"attachments": result.attachments}
                    if result.attachments else None),
        )
        return result

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        in_reply_to: str = "",
        from_identity: str | None = None,
    ) -> DraftResult:
        request = DraftRequest(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            in_reply_to=in_reply_to,
            from_identity=from_identity,
        )
        try:
            result = self._delivery.create_draft(request)
        except ToolError as error:
            self._failed_delivery(
                "draft", "create_draft", error, subject=subject,
            )
            raise
        self._event(
            "draft", "created", tool="create_draft",
            message_id=result.message_id,
            identity=from_identity,
            to=result.to,
            cc=result.cc or None,
            subject=result.subject,
            detail={"draft_id": result.draft_id, "account": result.account},
        )
        return result

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
        request = ReplyRequest(
            id=id,
            body=body,
            reply_all=reply_all,
            cc=cc,
            bcc=bcc,
            include_history=include_history,
            attachments=tuple(attachments or ()),
            from_identity=from_identity,
        )
        try:
            result = self._delivery.reply(self.source, request)
        except ToolError as error:
            self._failed_delivery(
                "reply", "reply_email", error,
                detail={
                    "orig_id": id,
                    "reply_all": reply_all,
                },
            )
            raise
        self._sent(
            "reply", "reply_email", result, from_identity,
            detail={
                "orig_id": id,
                "reply_all": reply_all,
            },
        )
        return result

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
        request = ScheduleRequest(
            to=to,
            subject=subject,
            body=body,
            send_at=send_at,
            cc=cc,
            bcc=bcc,
            attachments=tuple(attachments or ()),
            from_identity=from_identity,
        )
        try:
            entry = self._delivery.schedule(request)
        except ToolError as error:
            self._failed_delivery(
                "schedule", "schedule_email", error,
                subject=subject,
            )
            raise
        requested = (
            self._delivery.requested_executor(entry.identity)
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
