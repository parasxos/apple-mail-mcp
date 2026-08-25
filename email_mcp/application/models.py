"""Return records owned by the application boundary."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ..domain.mail import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    Mailbox,
)
from ..domain.models import IntegrityIssue, PlanAction, ScheduledEntry


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


@dataclass(frozen=True)
class AuditQuery:
    since: str | None = None
    until: str | None = None
    tool: str | None = None
    event: str | None = None
    plan_id: str | None = None
    operation_id: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class QueueIntegrity:
    ok: bool
    counts: dict[str, int]
    readable_counts: dict[str, int]
    message_files: dict[str, int]
    issues: list[IntegrityIssue]

    def to_wire(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScheduleListing:
    dispatcher_installed: bool
    dispatcher_label: str
    entries: dict[str, list[ScheduledEntry]]
    integrity: QueueIntegrity

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "dispatcher_installed": self.dispatcher_installed,
            "dispatcher_label": self.dispatcher_label,
            **self.entries,
        }
        if not self.integrity.ok:
            out.update({
                "ok": False,
                "code": "spool_integrity",
                "error": (
                    "scheduled-mail storage has "
                    f"{len(self.integrity.issues)} integrity issue(s); "
                    "readable records are included below and damaged "
                    "records were left untouched"
                ),
                "fix": (
                    "run `email-mcp dispatcher --status`, then reconcile "
                    "every path in integrity.issues before deleting or "
                    "rescheduling mail"
                ),
                "integrity": self.integrity.to_wire(),
            })
        return out


@dataclass(frozen=True)
class TriageFailure:
    id: str
    code: str
    detail: str


@dataclass(frozen=True)
class TriageApplyResult:
    plan_id: str
    status: str
    planned: int
    acted: int
    failures: list[TriageFailure]
    verified: int
    pending: list[dict]
    osascript_ms: int
    verify_polls: int
    duration_ms: int
    note: str | None = None

    def to_wire(self) -> dict:
        return {"ok": True, **asdict(self)}


@dataclass(frozen=True)
class MailboxCreateResult:
    account: str
    path: str
    existed: bool
    applescript: str | None
    index_verified: bool
    mail_verified: bool
    warning: str | None

    def to_wire(self) -> dict:
        return {"ok": True, **asdict(self)}


@dataclass(frozen=True)
class MailboxDeleteResult:
    ok: bool
    account: str
    path: str
    existed: bool
    deleted: bool
    mail_verified: bool
    warning: str | None = None
    method: str | None = None
    code: str | None = None
    error: str | None = None

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "ok": self.ok,
            "account": self.account,
            "path": self.path,
            "existed": self.existed,
            "deleted": self.deleted,
            "mail_verified": self.mail_verified,
        }
        if self.ok:
            out["warning"] = self.warning
            if self.method is not None:
                out["method"] = self.method
        else:
            if self.code is not None:
                out["code"] = self.code
            if self.error is not None:
                out["error"] = self.error
        return out


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    read_only: bool
    checks: dict[str, dict[str, Any]]
    audit: dict[str, Any]

    def to_wire(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TransportReport:
    ok: bool
    detail: str
    identities: dict[str, dict[str, Any]]
    default: str | None = None
    fix: str | None = None
    advisory: bool | None = None

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "ok": self.ok,
            "detail": self.detail,
            "identities": self.identities,
        }
        if self.default is not None:
            out["default"] = self.default
        if self.fix is not None:
            out["fix"] = self.fix
        if self.advisory is not None:
            out["advisory"] = self.advisory
        return out


@dataclass(frozen=True)
class DispatchSummary:
    checked_at: str
    due: int
    results: dict[str, str]
    integrity: QueueIntegrity | None = None

    def to_wire(self) -> dict:
        out = {
            "checked_at": self.checked_at,
            "due": self.due,
            "results": dict(self.results),
        }
        if self.integrity is not None:
            out["integrity"] = self.integrity.to_wire()
        return out
