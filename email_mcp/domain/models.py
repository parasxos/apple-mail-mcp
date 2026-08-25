"""Infrastructure-independent records shared by use cases and adapters."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SendRequest:
    """Complete application-owned command for immediate delivery."""

    to: str
    subject: str
    body: str
    cc: str | None = None
    bcc: str | None = None
    attachments: tuple[str, ...] = ()
    from_identity: str | None = None


@dataclass(frozen=True)
class DraftRequest:
    """Complete application-owned command for creating a provider draft."""

    to: str
    subject: str
    body: str
    cc: str | None = None
    in_reply_to: str = ""
    from_identity: str | None = None


@dataclass(frozen=True)
class ReplyRequest:
    """Complete application-owned command for replying to a message."""

    id: str
    body: str
    reply_all: bool = False
    cc: str | None = None
    bcc: str | None = None
    include_history: bool = True
    attachments: tuple[str, ...] = ()
    from_identity: str | None = None


@dataclass(frozen=True)
class ScheduleRequest:
    """Complete application-owned command for durable deferred delivery."""

    to: str
    subject: str
    body: str
    send_at: str
    cc: str | None = None
    bcc: str | None = None
    attachments: tuple[str, ...] = ()
    from_identity: str | None = None


@dataclass
class SendResult:
    ok: bool
    message_id: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    attachments: list[str] = field(default_factory=list)
    bootstrapped: bool = False
    error: str | None = None


@dataclass
class DraftResult:
    """A draft filed in the selected account and never transmitted."""

    ok: bool
    draft_id: str
    message_id: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    folder: str = "drafts"
    account: str = ""


@dataclass
class PlanAction:
    action: str
    mailbox: str | None = None
    color: int | None = None


@dataclass
class PlanMessage:
    rowid: int
    account: str
    scheme: str
    mailbox: str
    mailbox_rowid: int
    subject: str
    from_addr: str
    date: str
    unread: bool
    message_id_header: str
    global_message_id: int | None
    pre: dict


@dataclass
class Plan:
    id: str
    created_at: str
    expires_at: str
    status: str
    query: dict
    actions: list[PlanAction]
    target: dict | None
    messages: list[PlanMessage]
    summary: str
    result: dict | None = None


@dataclass
class ScheduledEntry:
    id: str
    send_at: str
    created_at: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    attachments: list[str]
    message_id: str
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    delivered_at: str | None = None
    identity: str = "default"
    executor: str = "launchd"
    graph_draft_id: str | None = None


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    state: str
    id: str | None
    path: str
    detail: str


@dataclass
class ScheduledScan:
    state: str
    entries: list[ScheduledEntry] = field(default_factory=list)
    manifest_files: int = 0
    eml_files: int = 0
    issues: list[IntegrityIssue] = field(default_factory=list)
    artifact_ids: tuple[str, ...] = ()

    @property
    def readable_manifests(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        return not self.issues
