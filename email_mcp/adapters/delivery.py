"""Production delivery adapter over the existing transport implementations."""
from __future__ import annotations

from .. import identities, sender
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


class DefaultDeliveryGateway:
    def send(self, request: SendRequest) -> SendResult:
        return sender.send_email(
            to=request.to,
            subject=request.subject,
            body=request.body,
            cc=request.cc,
            bcc=request.bcc,
            attachments=list(request.attachments) or None,
            from_identity=request.from_identity,
        )

    def create_draft(self, request: DraftRequest) -> DraftResult:
        return sender.create_draft(
            to=request.to,
            subject=request.subject,
            body=request.body,
            cc=request.cc,
            in_reply_to=request.in_reply_to,
            from_identity=request.from_identity,
        )

    def reply(self, source: EmailSource,
              request: ReplyRequest) -> SendResult:
        return sender.reply_email(
            source,
            id=request.id,
            body=request.body,
            reply_all=request.reply_all,
            cc=request.cc,
            bcc=request.bcc,
            include_history=request.include_history,
            attachments=list(request.attachments) or None,
            from_identity=request.from_identity,
        )

    def schedule(self, request: ScheduleRequest) -> ScheduledEntry:
        return sender.schedule_email(
            to=request.to,
            subject=request.subject,
            body=request.body,
            send_at=request.send_at,
            cc=request.cc,
            bcc=request.bcc,
            attachments=list(request.attachments) or None,
            from_identity=request.from_identity,
        )

    def requested_executor(self, identity: str) -> str | None:
        try:
            return identities.get(identity).executor
        except sender.SendError:
            return None
