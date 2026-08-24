"""Shared application dependencies and event publication."""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.events import DomainEvent, EventPublisher
from ..domain.mail import EmailSource
from .ports import (
    BackgroundGateway,
    DeferredScheduler,
    DeliveryGateway,
    OperationsGateway,
    RefreshGateway,
    ScheduleStore,
    SourceProvider,
    TriageGateway,
)


@dataclass(frozen=True)
class ApplicationDependencies:
    source: SourceProvider
    delivery: DeliveryGateway
    schedules: ScheduleStore
    deferred: DeferredScheduler
    triage: TriageGateway
    refresh: RefreshGateway
    operations: OperationsGateway
    events: EventPublisher
    background: BackgroundGateway | None = None


class ApplicationService:
    """Base shared by cohesive use-case groups."""

    def __init__(self, dependencies: ApplicationDependencies):
        self._deps = dependencies

    @property
    def source(self) -> EmailSource:
        return self._deps.source.get()

    def _event(
        self,
        name: str,
        outcome: str,
        *,
        operation_id: str | None = None,
        tool: str | None = None,
        **attributes,
    ) -> None:
        self._deps.events.publish(DomainEvent(
            name=name,
            outcome=outcome,
            operation_id=operation_id,
            tool=tool,
            attributes=attributes,
        ))
