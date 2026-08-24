"""Provider-neutral mailbox records and the mailbox source contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Mailbox:
    account: str
    name: str
    path: str
    total: int
    unread: int
    local_count: int


@dataclass(frozen=True)
class AttachmentRef:
    name: str
    mime: str | None
    size: int | None
    attachment_id: str


@dataclass(frozen=True)
class EmailRef:
    """Lightweight envelope returned by search, recent, and thread calls."""

    id: str
    subject: str
    from_addr: str
    to: list[str]
    cc: list[str]
    date: datetime
    mailbox: str
    account: str
    snippet: str
    unread: bool
    has_attachment: bool
    thread_id: str


@dataclass(frozen=True)
class Email:
    """A complete message returned by a mailbox source."""

    ref: EmailRef
    headers: dict[str, str]
    body_text: str
    body_html: str
    attachments: list[AttachmentRef]
    flags: dict[str, bool]
    body_source: str | None = None


@dataclass(frozen=True)
class AttachmentBlob:
    name: str
    mime: str
    size: int
    path: str


@dataclass(frozen=True)
class SearchQuery:
    query: str = ""
    from_addr: str | None = None
    to_addr: str | None = None
    mailbox: str | None = None
    account: str | None = None
    before: datetime | None = None
    after: datetime | None = None
    has_attachment: bool | None = None
    unread_only: bool = False
    limit: int = 50
    offset: int = 0
    # Delete planning narrows the same neutral query contract safely.
    exclude_trash: bool = False
    from_exact: bool = False


class EmailSource(Protocol):
    """Inbound mailbox port implemented by Apple Mail or another provider."""

    def search(self, query: SearchQuery) -> list[EmailRef]: ...

    def get(self, id: str) -> Email: ...

    def thread(self, thread_id: str) -> list[EmailRef]: ...

    def mailboxes(self) -> list[Mailbox]: ...

    def recent(
        self,
        mailbox: str | None,
        account: str | None,
        limit: int,
    ) -> list[EmailRef]: ...

    def attachment(self, id: str, attachment_id: str) -> AttachmentBlob: ...
