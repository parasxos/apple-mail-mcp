"""Small, role-specific interfaces owned by the application layer."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain.mail import EmailSource, SearchQuery
from ..domain.errors import ToolError
from ..domain.models import (
    DraftRequest,
    DraftResult,
    Plan,
    ReplyRequest,
    ScheduleRequest,
    ScheduledEntry,
    SendRequest,
    SendResult,
)
from .models import (
    AuditPage,
    AuditQuery,
    DoctorReport,
    MailboxCreateResult,
    MailboxDeleteResult,
    QueueIntegrity,
    RefreshOutcome,
    ScheduleListing,
    TransportReport,
    TriageApplyResult,
)


class SourceProvider(Protocol):
    def get(self) -> EmailSource: ...


class DeliveryGateway(Protocol):
    def send(self, request: SendRequest) -> SendResult: ...

    def create_draft(self, request: DraftRequest) -> DraftResult: ...

    def reply(self, source: EmailSource,
              request: ReplyRequest) -> SendResult: ...

    def schedule(self, request: ScheduleRequest) -> ScheduledEntry: ...

    def requested_executor(self, identity: str) -> str | None: ...


class ScheduleStore(Protocol):
    states: tuple[str, ...]

    def dispatcher_installed(self) -> bool: ...

    def listing(self, state: str | None, limit: int) -> ScheduleListing: ...

    def find(self, operation_id: str) -> tuple[str, ScheduledEntry] | None: ...

    def claim(self, operation_id: str, old: str, new: str) -> bool: ...

    def update(self, state: str, entry: ScheduledEntry) -> None: ...

    def mark_delivered_now(self, entry: ScheduledEntry) -> None: ...


class IdentityResolver(Protocol):
    """Resolve a configured name to an opaque adapter-owned identity."""

    def resolve(self, name: str) -> object: ...


class DeferredDelivery(Protocol):
    """Remote provider operations needed for safe deferred delivery."""

    def find_draft(self, identity: object,
                   message_id: str) -> str | None: ...

    def delete_draft(self, identity: object, draft_id: str) -> str: ...

    def was_sent(self, identity: object, message_id: str) -> bool: ...

    def status(self, identity: object, draft_id: str,
               message_id: str) -> str: ...


class TriageGateway(Protocol):
    def plan_cap(self) -> int: ...

    def delete_cap(self) -> int: ...

    def build(self, source: EmailSource, query: SearchQuery,
              actions: list[dict] | None) -> Plan: ...

    def build_delete(self, source: EmailSource, query: SearchQuery) -> Plan: ...

    def apply(self, source: EmailSource, plan_id: str) -> TriageApplyResult: ...

    def create_mailbox(self, source: EmailSource, account: str,
                       path: str) -> MailboxCreateResult: ...

    def delete_mailbox(self, source: EmailSource, account: str,
                       path: str) -> MailboxDeleteResult: ...


class RefreshGateway(Protocol):
    def refresh(self, source: EmailSource, wait_seconds: float,
                timeout_seconds: float) -> RefreshOutcome: ...


class ErrorClassifier(Protocol):
    def classify(self, error: BaseException) -> str: ...


class OperationsGateway(Protocol):
    def doctor(self) -> DoctorReport: ...

    def transport_check(self) -> TransportReport: ...

    def audit(self, query: AuditQuery) -> AuditPage: ...


class BackgroundIdentityError(ToolError):
    """A scheduled entry refers to an unavailable identity."""


class BackgroundProviderError(ToolError):
    """A remote deferred provider could not give a safe answer."""


class BackgroundDeliveryError(ToolError):
    """The local transport rejected or could not send a message."""


class Clock(Protocol):
    def now(self) -> datetime: ...

    def format(self, value: datetime) -> str: ...


class DispatchQueue(Protocol):
    """Durable queue role used by the one-pass delivery state machine."""

    def entries(self, state: str) -> list[ScheduledEntry]: ...

    def load(self, state: str, operation_id: str) -> ScheduledEntry | None: ...

    def claim(self, operation_id: str) -> bool: ...

    def move(self, entry: ScheduledEntry, source: str, target: str) -> None: ...

    def update(self, state: str, entry: ScheduledEntry) -> None: ...

    def read_message(self, state: str, operation_id: str) -> bytes: ...

    def integrity(self) -> QueueIntegrity: ...


class LocalDelivery(Protocol):
    def preflight(self, identity: object) -> tuple[bool, str | None]: ...

    def deliver(self, identity: object, raw: bytes,
                recipients: list[str]) -> None: ...


class UserNotifier(Protocol):
    def notify(self, title: str, text: str) -> None: ...
