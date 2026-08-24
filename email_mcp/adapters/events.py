"""Audit-ledger adapter for application domain events."""
from __future__ import annotations

from .. import audit
from ..domain.events import DomainEvent


class AuditEventPublisher:
    def publish(self, event: DomainEvent) -> None:
        audit.emit(
            event.name,
            outcome=event.outcome,
            operation_id=event.operation_id,
            tool=event.tool,
            **event.attributes,
        )
