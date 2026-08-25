"""Shared event publication for use cases that produce audit events."""
from __future__ import annotations

from ..domain.events import DomainEvent, EventPublisher


class ApplicationService:
    """Minimal base: evented services receive only the event port."""

    def __init__(self, events: EventPublisher) -> None:
        self._events = events

    def _event(
        self,
        name: str,
        outcome: str,
        *,
        operation_id: str | None = None,
        tool: str | None = None,
        **attributes,
    ) -> None:
        self._events.publish(DomainEvent(
            name=name,
            outcome=outcome,
            operation_id=operation_id,
            tool=tool,
            attributes=attributes,
        ))
