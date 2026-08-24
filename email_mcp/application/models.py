"""Return records owned by the application boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.mail import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    Mailbox,
)
from ..domain.models import PlanAction


@dataclass(frozen=True)
class SearchHit(EmailRef):
    body_match: bool = False


@dataclass(frozen=True)
class SearchPage:
    fts: dict
    results: list[SearchHit]
    note: str | None


@dataclass(frozen=True)
class EmailMetadata:
    ref: EmailRef
    headers: dict
    attachments: list[AttachmentRef]
    flags: dict


@dataclass(frozen=True)
class EmailMinimal:
    id: str
    subject: str
    from_addr: str
    date: datetime
    mailbox: str
    unread: bool


@dataclass(frozen=True)
class OneEmail:
    email: Email | EmailMetadata | EmailMinimal


@dataclass(frozen=True)
class BatchItemError:
    id: str
    error: str
    code: str


@dataclass(frozen=True)
class EmailBatch:
    view: str
    emails: list[Email | EmailMetadata | EmailMinimal]
    errors: list[BatchItemError]


@dataclass(frozen=True)
class Thread:
    thread: list[EmailRef]


@dataclass(frozen=True)
class MailboxList:
    mailboxes: list[Mailbox]


@dataclass(frozen=True)
class RecentPage:
    messages: list[EmailRef]
    note: str | None


@dataclass(frozen=True)
class AttachmentOut:
    attachment: AttachmentBlob


@dataclass(frozen=True)
class RefreshOutcome:
    ok: bool
    applescript_duration_ms: int | None
    waited_seconds: float
    before: dict | None
    after: dict | None
    new_messages: int | None
    error: str | None
    error_code: int | None
    code: str | None


@dataclass(frozen=True)
class PlanMessageOut:
    id: str
    subject: str
    from_addr: str
    date: str
    mailbox: str
    unread: bool


@dataclass(frozen=True)
class PlanReceipt:
    plan_id: str
    count: int
    expires_at: str
    summary: str
    actions: list[PlanAction]
    messages: list[PlanMessageOut]


@dataclass(frozen=True)
class CancelReceipt:
    id: str
    status: str
    subject: str
    was_due: str


@dataclass(frozen=True)
class AuditPage:
    events: list[dict]
    files_scanned: int
    skipped_lines: int
