"""Domain events emitted by application use cases.

Events describe completed or refused operations without knowing where they
will be recorded.  The production adapter writes them to the audit ledger;
tests and alternate front ends can collect or ignore them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class DomainEvent:
    name: str
    outcome: str
    operation_id: str | None = None
    tool: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> None: ...


class NullEventPublisher:
    def publish(self, event: DomainEvent) -> None:
        del event
