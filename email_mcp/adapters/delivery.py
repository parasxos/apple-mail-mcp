"""Production delivery adapter over the existing transport implementations."""
from __future__ import annotations

from typing import Any

from .. import identities, sender
from ..domain.mail import EmailSource


class DefaultDeliveryGateway:
    def send(self, **values: Any):
        return sender.send_email(**values)

    def create_draft(self, **values: Any):
        return sender.create_draft(**values)

    def reply(self, source: EmailSource, **values: Any):
        return sender.reply_email(source, **values)

    def schedule(self, **values: Any):
        return sender.schedule_email(**values)

    def requested_executor(self, identity: str) -> str | None:
        try:
            return identities.get(identity).executor
        except sender.SendError:
            return None
