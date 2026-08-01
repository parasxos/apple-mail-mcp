"""Source-agnostic types + the EmailSource Protocol.

Every adapter (Apple Mail today, Gmail/IMAP/mbox in Phase 2) returns these
dataclasses so the MCP server layer never knows what backend it's talking to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Mailbox:
    account: str           # account UUID or alias
    name: str              # folder path within the account, e.g. "INBOX" or "[Gmail]/All Mail"
    path: str              # implementation-defined identifier (URL for Apple Mail)
    total: int             # server-side count the account advertises
    unread: int
    local_count: int       # messages actually in the local store — what reads can serve


@dataclass(frozen=True)
class AttachmentRef:
    name: str
    mime: str | None
    size: int | None
    attachment_id: str     # part path inside the .emlx (e.g. "1.2") or backend-specific id


@dataclass(frozen=True)
class EmailRef:
    """Lightweight envelope returned by search/list/thread."""
    id: str                # source-defined stable id (Apple Mail: messages.ROWID as str)
    subject: str
    from_addr: str         # "Display Name <email@host>" or just email
    to: list[str]
    cc: list[str]
    date: datetime         # tz-aware UTC
    mailbox: str           # mailbox.name
    account: str           # account UUID
    snippet: str           # Apple Mail's stored summary (or empty)
    unread: bool
    has_attachment: bool
    thread_id: str         # conversation_id as str (empty if none)


@dataclass(frozen=True)
class Email:
    """Full email body returned by get_email."""
    ref: EmailRef
    headers: dict[str, str]
    body_text: str
    body_html: str
    attachments: list[AttachmentRef]
    flags: dict[str, bool]  # {"flagged": bool, "read": bool, ...}


@dataclass(frozen=True)
class AttachmentBlob:
    name: str
    mime: str
    size: int
    path: str              # on-disk path to a tmp file holding the bytes


@dataclass(frozen=True)
class SearchQuery:
    query: str = ""        # plain-text search over subject + sender + snippet
    from_addr: str | None = None
    to_addr: str | None = None
    mailbox: str | None = None  # mailbox name (e.g. "INBOX")
    account: str | None = None  # account UUID
    before: datetime | None = None
    after: datetime | None = None
    has_attachment: bool | None = None
    unread_only: bool = False
    limit: int = 50
    offset: int = 0
    # Skip every account's Trash mailbox. Set ONLY by the delete-plan path
    # (triage.build_delete_plan) — trashed mail keeps deleted=0 and lives on
    # under a fresh id, so a delete selection that saw the Trash would
    # re-select its own prior work forever. Plain searches leave this False.
    exclude_trash: bool = False
    # Narrow from_addr from its substring semantics (over address AND
    # display name) to case-insensitive equality against the bare sender
    # address. Set ONLY by the delete-plan path — a substring there turns
    # "google.com" into a domain-wide delete (field-observed 2026-08-01).
    # Plain searches leave this False.
    from_exact: bool = False


class EmailSource(Protocol):
    """The single extension point. One file per source.

    All methods must be safe to call concurrently with the underlying mail
    client running — implementations open the index read-only.
    """

    def search(self, q: SearchQuery) -> list[EmailRef]: ...

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
