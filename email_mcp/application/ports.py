"""Outbound interfaces used by the application layer."""
from __future__ import annotations

from typing import Any, Protocol

from ..domain.events import EventPublisher
from ..domain.mail import EmailSource, SearchQuery
from ..domain.models import DraftResult, Plan, ScheduledEntry, SendResult
from .models import RefreshOutcome


class SourceProvider(Protocol):
    def get(self) -> EmailSource: ...


class DeliveryGateway(Protocol):
    def send(self, **values: Any) -> SendResult: ...

    def create_draft(self, **values: Any) -> DraftResult: ...

    def reply(self, source: EmailSource, **values: Any) -> SendResult: ...

    def schedule(self, **values: Any) -> ScheduledEntry: ...

    def requested_executor(self, identity: str) -> str | None: ...


class ScheduleStore(Protocol):
    states: tuple[str, ...]

    def dispatcher_installed(self) -> bool: ...

    def listing(self, state: str | None, limit: int) -> dict: ...

    def find(self, operation_id: str) -> tuple[str, ScheduledEntry] | None: ...

    def claim(self, operation_id: str, old: str, new: str) -> bool: ...

    def update(self, state: str, entry: ScheduledEntry) -> None: ...

    def mark_delivered_now(self, entry: ScheduledEntry) -> None: ...


class DeferredScheduler(Protocol):
    def identity(self, name: str) -> Any: ...

    def find_draft(self, identity: Any, message_id: str) -> str | None: ...

    def delete_draft(self, identity: Any, draft_id: str) -> str: ...

    def was_sent(self, identity: Any, message_id: str) -> bool: ...


class TriageGateway(Protocol):
    def plan_cap(self) -> int: ...

    def delete_cap(self) -> int: ...

    def build(self, source: EmailSource, query: SearchQuery,
              actions: list[dict] | None) -> Plan: ...

    def build_delete(self, source: EmailSource, query: SearchQuery) -> Plan: ...

    def apply(self, source: EmailSource, plan_id: str) -> dict: ...

    def create_mailbox(self, source: EmailSource, account: str,
                       path: str) -> dict: ...

    def delete_mailbox(self, source: EmailSource, account: str,
                       path: str) -> dict: ...


class RefreshGateway(Protocol):
    def refresh(self, source: EmailSource, wait_seconds: float,
                timeout_seconds: float) -> RefreshOutcome: ...


class OperationsGateway(Protocol):
    def doctor(self) -> dict: ...

    def transport_check(self) -> dict: ...

    def audit(self, **filters: Any) -> dict: ...

    def classify(self, error: BaseException) -> str: ...


class ApplicationPorts(Protocol):
    source: SourceProvider
    delivery: DeliveryGateway
    schedules: ScheduleStore
    deferred: DeferredScheduler
    triage: TriageGateway
    refresh: RefreshGateway
    operations: OperationsGateway
    events: EventPublisher
