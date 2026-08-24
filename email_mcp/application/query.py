"""Provider-neutral query construction, bounds, and result shaping."""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from ..domain.errors import InvalidInput
from ..domain.mail import Email, EmailSource, SearchQuery
from .models import EmailMetadata, EmailMinimal

VIEWS = ("minimal", "metadata", "full")
BATCH_MAX_IDS = 50
PAGE_MAX = 500


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"invalid ISO datetime: {value!r} ({error})"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def search_query(**values) -> SearchQuery:
    return SearchQuery(
        query=values.get("query", ""),
        from_addr=values.get("from_addr"),
        to_addr=values.get("to_addr"),
        mailbox=values.get("mailbox"),
        account=values.get("account"),
        before=parse_datetime(values.get("before")),
        after=parse_datetime(values.get("after")),
        has_attachment=values.get("has_attachment"),
        unread_only=values.get("unread_only", False),
        limit=values.get("limit", 50),
        offset=values.get("offset", 0),
    )


def check_page(limit: int) -> None:
    if not 0 < limit <= PAGE_MAX:
        raise InvalidInput(
            f"limit {limit} is outside 1..{PAGE_MAX} — lower it and paginate"
        )


def empty_scope_note(
    source: EmailSource,
    mailbox: str,
    account: str | None,
) -> str | None:
    tail = "/" + urllib.parse.quote(mailbox)
    scoped = [
        candidate for candidate in source.mailboxes()
        if tail in candidate.path
        and (not account or f"//{account}/" in candidate.path)
    ]
    server_side = sum(candidate.total for candidate in scoped)
    if (not scoped or not server_side
            or any(candidate.local_count for candidate in scoped)):
        return None
    return (
        f"mailbox {mailbox!r} holds {server_side} message(s) server-side "
        "but none in the local store — Gmail accounts keep local copies "
        "only under [Gmail]/All Mail; search there, or drop the mailbox "
        "filter."
    )


def check_view(view: str) -> None:
    if view not in VIEWS:
        raise InvalidInput(f"unknown view {view!r} (want one of {VIEWS})")


def shape_email(message: Email, view: str):
    if view == "full":
        return message
    if view == "metadata":
        return EmailMetadata(
            ref=message.ref,
            headers=message.headers,
            attachments=message.attachments,
            flags=message.flags,
        )
    ref = message.ref
    return EmailMinimal(
        id=ref.id,
        subject=ref.subject,
        from_addr=ref.from_addr,
        date=ref.date,
        mailbox=ref.mailbox,
        unread=ref.unread,
    )
