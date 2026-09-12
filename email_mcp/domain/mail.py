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


class Headers(dict):
    """Parsed message headers: keyed by wire spelling (what the caller
    sees), looked up case-insensitively (RFC 5322 field names are). A
    `reply-to` written lowercase routes a reply exactly like `Reply-To`."""

    def __getitem__(self, name: str) -> str:
        for k, v in self.items():
            if k.lower() == name.lower():
                return v
        raise KeyError(name)

    def get(self, name: str, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def __contains__(self, name) -> bool:
        return self.get(name) is not None


@dataclass(frozen=True)
class Email:
    """A complete message returned by a mailbox source."""

    ref: EmailRef
    headers: dict[str, str]  # the wire shape; a Headers at runtime
    body_text: str
    body_html: str
    attachments: list[AttachmentRef]
    flags: dict[str, bool]
    body_source: str | None = None

    def __post_init__(self) -> None:
        # Every source hands over a plain dict; the type stamps the
        # lookup rule so no consumer can spell its way past it. The
        # annotation stays dict[str, str]: that is what the MCP output
        # schema is derived from, and what the caller receives.
        object.__setattr__(self, "headers", Headers(self.headers))


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
