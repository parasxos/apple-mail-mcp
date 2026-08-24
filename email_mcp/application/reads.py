"""Read, search, attachment, and refresh use cases."""
from __future__ import annotations

from dataclasses import asdict

from ..domain.errors import InvalidInput
from .base import ApplicationService
from .models import (
    AttachmentOut,
    BatchItemError,
    EmailBatch,
    MailboxList,
    OneEmail,
    RecentPage,
    RefreshOutcome,
    SearchHit,
    SearchPage,
    Thread,
)
from .query import (
    BATCH_MAX_IDS,
    check_page,
    check_view,
    empty_scope_note,
    search_query,
    shape_email,
)


class ReadUseCases(ApplicationService):
    def search_emails(self, **values) -> SearchPage:
        limit = values.get("limit", 50)
        check_page(limit)
        query = search_query(**values)
        source = self.source
        hits = source.search(query)
        fts = dict(getattr(source, "fts_status", lambda: None)() or {
            "state": "unavailable", "hits": 0, "hits_capped": False,
        })
        hit_ids = {str(rowid) for rowid in fts.pop("rowids", [])}
        query_lower = values.get("query", "").lower()
        results = []
        for hit in hits:
            visible = bool(query_lower) and (
                query_lower in hit.subject.lower()
                or query_lower in hit.from_addr.lower()
                or query_lower in hit.snippet.lower()
            )
            results.append(SearchHit(
                **asdict(hit),
                body_match=hit.id in hit_ids and not visible,
            ))
        mailbox = values.get("mailbox")
        note = (empty_scope_note(source, mailbox, values.get("account"))
                if mailbox and not results else None)
        return SearchPage(fts=fts, results=results, note=note)

    def get_email(self, id: str, view: str = "full") -> OneEmail:
        check_view(view)
        return OneEmail(email=shape_email(self.source.get(id), view))

    def get_emails_batch(
        self,
        ids: list[str],
        view: str = "full",
    ) -> EmailBatch:
        check_view(view)
        if len(ids) > BATCH_MAX_IDS:
            raise InvalidInput(
                f"{len(ids)} ids exceeds the batch cap of {BATCH_MAX_IDS} — "
                "split the request"
            )
        emails, errors = [], []
        source = self.source
        for message_id in ids:
            try:
                emails.append(shape_email(source.get(str(message_id)), view))
            except (ValueError, LookupError) as error:
                errors.append(BatchItemError(
                    id=str(message_id),
                    error=str(error),
                    code=self._deps.operations.classify(error),
                ))
        return EmailBatch(view=view, emails=emails, errors=errors)

    def get_thread(self, thread_id: str) -> Thread:
        return Thread(thread=list(self.source.thread(thread_id)))

    def list_mailboxes(self) -> MailboxList:
        return MailboxList(mailboxes=list(self.source.mailboxes()))

    def list_recent(
        self,
        mailbox: str | None = None,
        account: str | None = None,
        limit: int = 50,
    ) -> RecentPage:
        check_page(limit)
        source = self.source
        messages = list(source.recent(mailbox, account, limit))
        note = (empty_scope_note(source, mailbox, account)
                if mailbox and not messages else None)
        return RecentPage(messages=messages, note=note)

    def get_attachment(self, id: str, attachment_id: str) -> AttachmentOut:
        return AttachmentOut(
            attachment=self.source.attachment(id, attachment_id)
        )

    def refresh_mail(
        self,
        wait_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
    ) -> RefreshOutcome:
        wait_seconds = max(0.0, min(60.0, float(wait_seconds)))
        timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))
        return self._deps.refresh.refresh(
            self.source, wait_seconds, timeout_seconds,
        )
