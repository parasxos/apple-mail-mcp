"""Production adapters for scheduled-delivery infrastructure roles."""
from __future__ import annotations

import subprocess
from datetime import datetime

from .. import sender, spool
from ..application.models import QueueIntegrity
from ..application.ports import BackgroundDeliveryError
from .queue import queue_integrity


class SystemClock:
    def now(self) -> datetime:
        return spool.utcnow()

    def format(self, value: datetime) -> str:
        return spool.iso(value)


class SpoolDispatchQueue:
    def entries(self, state: str):
        return spool.entries(state)

    def load(self, state: str, operation_id: str):
        return spool.load(state, operation_id)

    def claim(self, operation_id: str) -> bool:
        return spool.claim(operation_id)

    def move(self, entry, source: str, target: str) -> None:
        spool.move(entry.id, source, target, entry)

    def update(self, state: str, entry) -> None:
        spool.update(state, entry)

    def read_message(self, state: str, operation_id: str) -> bytes:
        return spool.read_eml(state, operation_id)

    def integrity(self) -> QueueIntegrity:
        return queue_integrity(spool.scan_all())


class DefaultLocalDelivery:
    def preflight(self, identity: object) -> tuple[bool, str | None]:
        ok, _ = sender.preflight(identity)
        return ok, None if ok else sender._transport_unavailable(identity)

    def deliver(self, identity: object, raw: bytes,
                recipients: list[str]) -> None:
        try:
            sender.deliver_for(identity, raw, rcpt_to=recipients)
        except sender.SendError as error:
            raise BackgroundDeliveryError(
                str(error), code=error.code,
            ) from error


class MacOSNotifier:
    """Best-effort user notification with content passed as data."""

    def notify(self, title: str, text: str) -> None:
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
