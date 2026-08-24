"""Production adapter for the scheduled-delivery application service."""
from __future__ import annotations

import subprocess
from datetime import datetime
from typing import Any, Callable

from .. import config, graph, identities, sender, spool
from ..application.ports import (
    BackgroundDeliveryError,
    BackgroundIdentityError,
    BackgroundProviderError,
)
from ..domain.models import ScheduledEntry


def _macos_notification(title: str, text: str) -> None:
    """Best-effort notification: a desktop failure cannot stop delivery."""
    script = """on run argv
display notification (item 2 of argv) with title (item 1 of argv)
end run"""
    try:
        subprocess.run(
            ["osascript", "-e", script, "--", title, text],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


class DefaultBackgroundGateway:
    """Translate concrete spool/provider APIs into stable application ports."""

    def __init__(
        self,
        notifier: Callable[[str, str], None] | None = None,
    ) -> None:
        self._notifier = notifier or _macos_notification

    def now(self) -> datetime:
        return spool.utcnow()

    def iso(self, value: datetime) -> str:
        return spool.iso(value)

    def entries(self, state: str) -> list[ScheduledEntry]:
        return spool.entries(state)

    def load(self, state: str, operation_id: str) -> ScheduledEntry | None:
        return spool.load(state, operation_id)

    def claim(self, operation_id: str) -> bool:
        return spool.claim(operation_id)

    def move(self, entry: ScheduledEntry, source: str, target: str) -> None:
        spool.move(entry.id, source, target, entry)

    def update(self, state: str, entry: ScheduledEntry) -> None:
        spool.update(state, entry)

    def read_message(self, state: str, operation_id: str) -> bytes:
        return spool.read_eml(state, operation_id)

    def integrity(self) -> dict:
        return spool.integrity(spool.scan_all())

    def max_retries(self) -> int:
        return config.send_max_retries()

    def identity(self, name: str) -> Any:
        try:
            return identities.get(name)
        except identities.IdentityError as error:
            raise BackgroundIdentityError(str(error)) from error

    def preflight(self, identity: Any) -> tuple[bool, str | None]:
        ok, _ = sender.preflight(identity)
        return ok, None if ok else sender._transport_unavailable(identity)

    def deliver(self, identity: Any, raw: bytes,
                recipients: list[str]) -> None:
        try:
            sender.deliver_for(identity, raw, rcpt_to=recipients)
        except sender.SendError as error:
            raise BackgroundDeliveryError(str(error)) from error

    def find_deferred_draft(self, identity: Any,
                            message_id: str) -> str | None:
        try:
            return graph.find_draft_by_message_id(identity, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(str(error)) from error

    def deferred_was_sent(self, identity: Any, message_id: str) -> bool:
        try:
            return graph.sent_by_message_id(identity, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(str(error)) from error

    def deferred_status(self, identity: Any, draft_id: str,
                        message_id: str) -> str:
        try:
            return graph.draft_status(identity, draft_id, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(str(error)) from error

    def delete_deferred_draft(self, identity: Any, draft_id: str) -> str:
        try:
            return graph.delete_draft(identity, draft_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(str(error)) from error

    def notify(self, title: str, text: str) -> None:
        self._notifier(title, text)
