"""The 21 email use cases, independent of MCP and concrete integrations."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ..domain.errors import InvalidInput, NotFound, ToolError
from ..domain.events import DomainEvent, EventPublisher
from ..domain.mail import Email, EmailSource, SearchQuery
from ..domain.models import DraftResult, ScheduledEntry, SendResult
from .models import (
    AttachmentOut,
    AuditPage,
    BatchItemError,
    CancelReceipt,
    EmailBatch,
    EmailMetadata,
    EmailMinimal,
    MailboxList,
    OneEmail,
    PlanMessageOut,
    PlanReceipt,
    RecentPage,
    RefreshOutcome,
    SearchHit,
    SearchPage,
    Thread,
)
from .ports import (
    DeferredScheduler,
    DeliveryGateway,
    OperationsGateway,
    RefreshGateway,
    ScheduleStore,
    SourceProvider,
    TriageGateway,
)

_VIEWS = ("minimal", "metadata", "full")
_BATCH_MAX_IDS = 50
_PAGE_MAX = 500
_ISO_BOUND_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


@dataclass(frozen=True)
class ApplicationDependencies:
    source: SourceProvider
    delivery: DeliveryGateway
    schedules: ScheduleStore
    deferred: DeferredScheduler
    triage: TriageGateway
    refresh: RefreshGateway
    operations: OperationsGateway
    events: EventPublisher


class EmailApplication:
    """Application service used identically by MCP, CLI, and tests."""

    def __init__(self, dependencies: ApplicationDependencies):
        self._deps = dependencies

    @property
    def source(self) -> EmailSource:
        return self._deps.source.get()

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
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

    @classmethod
    def _search_query(cls, **values) -> SearchQuery:
        return SearchQuery(
            query=values.get("query", ""),
            from_addr=values.get("from_addr"),
            to_addr=values.get("to_addr"),
            mailbox=values.get("mailbox"),
            account=values.get("account"),
            before=cls._parse_dt(values.get("before")),
            after=cls._parse_dt(values.get("after")),
            has_attachment=values.get("has_attachment"),
            unread_only=values.get("unread_only", False),
            limit=values.get("limit", 50),
            offset=values.get("offset", 0),
        )

    @staticmethod
    def _check_page(limit: int) -> None:
        if not 0 < limit <= _PAGE_MAX:
            raise InvalidInput(
                f"limit {limit} is outside 1..{_PAGE_MAX} — lower it and paginate"
            )

    @staticmethod
    def _empty_scope_note(source: EmailSource, mailbox: str,
                          account: str | None) -> str | None:
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

    @staticmethod
    def _check_view(view: str) -> None:
        if view not in _VIEWS:
            raise InvalidInput(f"unknown view {view!r} (want one of {_VIEWS})")

    @staticmethod
    def _shape_email(message: Email, view: str):
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

    def search_emails(self, **values) -> SearchPage:
        limit = values.get("limit", 50)
        self._check_page(limit)
        query = self._search_query(**values)
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
        note = (self._empty_scope_note(source, mailbox, values.get("account"))
                if mailbox and not results else None)
        return SearchPage(fts=fts, results=results, note=note)

    def get_email(self, id: str, view: str = "full") -> OneEmail:
        self._check_view(view)
        return OneEmail(email=self._shape_email(self.source.get(id), view))

    def get_emails_batch(self, ids: list[str],
                         view: str = "full") -> EmailBatch:
        self._check_view(view)
        if len(ids) > _BATCH_MAX_IDS:
            raise InvalidInput(
                f"{len(ids)} ids exceeds the batch cap of {_BATCH_MAX_IDS} — "
                "split the request"
            )
        emails, errors = [], []
        source = self.source
        for message_id in ids:
            try:
                emails.append(self._shape_email(source.get(str(message_id)), view))
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

    def list_recent(self, mailbox: str | None = None,
                    account: str | None = None,
                    limit: int = 50) -> RecentPage:
        self._check_page(limit)
        source = self.source
        messages = list(source.recent(mailbox, account, limit))
        note = (self._empty_scope_note(source, mailbox, account)
                if mailbox and not messages else None)
        return RecentPage(messages=messages, note=note)

    def get_attachment(self, id: str, attachment_id: str) -> AttachmentOut:
        return AttachmentOut(attachment=self.source.attachment(id, attachment_id))

    def refresh_mail(self, wait_seconds: float = 5.0,
                     timeout_seconds: float = 30.0) -> RefreshOutcome:
        wait_seconds = max(0.0, min(60.0, float(wait_seconds)))
        timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))
        return self._deps.refresh.refresh(
            self.source, wait_seconds, timeout_seconds,
        )

    def _event(self, name: str, outcome: str, *,
               operation_id: str | None = None,
               tool: str | None = None, **attributes) -> None:
        self._deps.events.publish(DomainEvent(
            name=name,
            outcome=outcome,
            operation_id=operation_id,
            tool=tool,
            attributes=attributes,
        ))

    def _failed_delivery(self, name: str, tool: str, error: ToolError,
                         *, subject: str | None = None,
                         detail: dict | None = None) -> None:
        failure_detail = dict(detail or {})
        failure_detail["error"] = str(error)[:300]
        self._event(name, "failed", tool=tool, subject=subject,
                    detail=failure_detail)

    def _sent(self, name: str, tool: str, result: SendResult,
              identity: str | None, *, detail: dict | None = None) -> None:
        self._event(
            name, "sent", tool=tool,
            message_id=result.message_id,
            identity=identity,
            to=result.to,
            cc=result.cc or None,
            bcc=result.bcc or None,
            subject=result.subject,
            detail=detail,
        )

    def send_email(self, **values) -> SendResult:
        try:
            result = self._deps.delivery.send(**values)
        except ToolError as error:
            self._failed_delivery(
                "send", "send_email", error, subject=values.get("subject")
            )
            raise
        self._sent(
            "send", "send_email", result, values.get("from_identity"),
            detail=({"attachments": result.attachments}
                    if result.attachments else None),
        )
        return result

    def create_draft(self, **values) -> DraftResult:
        try:
            result = self._deps.delivery.create_draft(**values)
        except ToolError as error:
            self._failed_delivery(
                "draft", "create_draft", error, subject=values.get("subject")
            )
            raise
        self._event(
            "draft", "created", tool="create_draft",
            message_id=result.message_id,
            identity=values.get("from_identity"),
            to=result.to,
            cc=result.cc or None,
            subject=result.subject,
            detail={"draft_id": result.draft_id, "account": result.account},
        )
        return result

    def reply_email(self, **values) -> SendResult:
        original_id = values["id"]
        try:
            result = self._deps.delivery.reply(self.source, **values)
        except ToolError as error:
            self._failed_delivery(
                "reply", "reply_email", error,
                detail={"orig_id": original_id,
                        "reply_all": values.get("reply_all", False)},
            )
            raise
        self._sent(
            "reply", "reply_email", result, values.get("from_identity"),
            detail={"orig_id": original_id,
                    "reply_all": values.get("reply_all", False)},
        )
        return result

    def schedule_email(self, **values) -> ScheduledEntry:
        try:
            entry = self._deps.delivery.schedule(**values)
        except ToolError as error:
            self._failed_delivery(
                "schedule", "schedule_email", error,
                subject=values.get("subject"),
            )
            raise
        requested = (self._deps.delivery.requested_executor(entry.identity)
                     if entry.executor == "launchd" else None)
        self._event(
            "schedule", "scheduled", operation_id=entry.id,
            tool="schedule_email", spool_id=entry.id,
            message_id=entry.message_id, identity=entry.identity,
            to=entry.to, cc=entry.cc or None, bcc=entry.bcc or None,
            subject=entry.subject,
            detail={
                "executor": entry.executor,
                "send_at": entry.send_at,
                "draft_id": entry.graph_draft_id,
                "graph_fallback": requested == "graph",
            },
        )
        return entry

    def list_scheduled(self, state: str | None = None,
                       limit: int = 50) -> dict:
        if state and state not in self._deps.schedules.states:
            raise InvalidInput(
                f"unknown state {state!r} "
                f"(want one of {self._deps.schedules.states})"
            )
        self._check_page(limit)
        return self._deps.schedules.listing(state, limit)

    def dispatcher_installed(self) -> bool:
        return self._deps.schedules.dispatcher_installed()

    @classmethod
    def _plan_query(cls, *, cap: int, limit: int = 0, **values) -> SearchQuery:
        return cls._search_query(
            **values,
            limit=limit if 0 < limit <= cap else cap + 1,
            offset=0,
        )

    @staticmethod
    def _plan_receipt(plan) -> PlanReceipt:
        return PlanReceipt(
            plan_id=plan.id,
            count=len(plan.messages),
            expires_at=plan.expires_at,
            summary=plan.summary,
            actions=list(plan.actions),
            messages=[
                PlanMessageOut(
                    id=str(message.rowid),
                    subject=message.subject,
                    from_addr=message.from_addr,
                    date=message.date,
                    mailbox=message.mailbox,
                    unread=message.unread,
                )
                for message in plan.messages
            ],
        )

    def triage_plan(self, *, limit: int = 0,
                    actions: list[dict] | None = None, **values) -> PlanReceipt:
        query = self._plan_query(
            cap=self._deps.triage.plan_cap(), limit=limit, **values,
        )
        return self._plan_receipt(
            self._deps.triage.build(self.source, query, actions)
        )

    def triage_plan_delete(self, *, limit: int = 0,
                           **values) -> PlanReceipt:
        query = self._plan_query(
            cap=self._deps.triage.delete_cap(), limit=limit, **values,
        )
        return self._plan_receipt(
            self._deps.triage.build_delete(self.source, query)
        )

    def triage_apply(self, plan_id: str) -> dict:
        return self._deps.triage.apply(self.source, plan_id)

    def mailbox_create(self, account: str, path: str) -> dict:
        result = self._deps.triage.create_mailbox(self.source, account, path)
        if result.get("existed") is False:
            self._event(
                "mailbox_create", "created", tool="mailbox_create",
                account=account, mailbox=path,
            )
        return result

    def mailbox_delete(self, account: str, path: str) -> dict:
        result = self._deps.triage.delete_mailbox(self.source, account, path)
        if result.get("existed"):
            outcome = ("deleted" if result.get("deleted")
                       else result.get("code", "delete_failed"))
            self._event(
                "mailbox_delete", outcome, tool="mailbox_delete",
                account=account, mailbox=path,
                detail=({"method": result["method"]}
                        if result.get("method") else None),
            )
        return result

    def _cancel_event(self, operation_id: str, outcome: str, *,
                      reason: str | None = None,
                      subject: str | None = None, **extra) -> None:
        detail = {"reason": reason, **extra} if reason else None
        self._event(
            "cancel", outcome, operation_id=operation_id,
            tool="cancel_scheduled", spool_id=operation_id,
            subject=subject, detail=detail,
        )

    def cancel_scheduled(self, id: str) -> CancelReceipt:
        found = self._deps.schedules.find(id)
        if found is None:
            self._cancel_event(id, "failed", reason="not_found")
            raise NotFound(f"no scheduled message with id {id!r}")
        state, entry = found
        if state != "pending":
            self._cancel_event(
                id, "failed", reason="not_pending",
                subject=entry.subject, state=state,
            )
            raise InvalidInput(
                f"cannot cancel {id}: status is {state!r} "
                "(only pending messages can be cancelled)",
                operation_id=id,
            )

        if entry.executor == "graph":
            try:
                identity = self._deps.deferred.identity(entry.identity)
            except ToolError as error:
                self._cancel_event(
                    id, "failed", reason="identity_unavailable",
                    subject=entry.subject,
                )
                raise ToolError(
                    f"cannot cancel {id}: {error}", code=error.code,
                    operation_id=id,
                ) from error
            try:
                draft_id = entry.graph_draft_id
                if draft_id is None:
                    draft_id = self._deps.deferred.find_draft(
                        identity, entry.message_id,
                    )
                outcome = (self._deps.deferred.delete_draft(identity, draft_id)
                           if draft_id else "gone")
            except ToolError as error:
                self._cancel_event(
                    id, "failed", reason="revoke_failed",
                    subject=entry.subject,
                )
                raise ToolError(
                    f"cannot cancel {id}: Exchange still holds the deferred "
                    f"draft and the revoke failed ({error}). Retry, or discard "
                    "the draft in Outlook/OWA yourself, then cancel again.",
                    code=error.code, operation_id=id,
                ) from error
            if outcome == "gone":
                try:
                    sent = self._deps.deferred.was_sent(
                        identity, entry.message_id,
                    )
                except ToolError as error:
                    self._cancel_event(
                        id, "failed", reason="sent_check_failed",
                        subject=entry.subject,
                    )
                    raise ToolError(
                        f"cannot cancel {id}: the deferred draft is gone but "
                        f"Sent Items could not be checked ({error}) — outcome "
                        "ambiguous, retry.", code=error.code, operation_id=id,
                    ) from error
                if sent:
                    if not self._deps.schedules.claim(id, "pending", "sent"):
                        self._cancel_event(
                            id, "failed", reason="claim_lost",
                            subject=entry.subject,
                        )
                        raise InvalidInput(
                            f"cannot cancel {id}: a dispatcher just moved it — "
                            "re-check list_scheduled", operation_id=id,
                        )
                    self._deps.schedules.mark_delivered_now(entry)
                    self._deps.schedules.update("sent", entry)
                    self._cancel_event(id, "too_late_sent", subject=entry.subject)
                    raise InvalidInput(
                        f"cannot cancel {id}: Exchange already sent it (found "
                        "in Sent Items) — the entry has been moved to sent/.",
                        operation_id=id, data={"id": id, "status": "sent"},
                    )

        if not self._deps.schedules.claim(id, "pending", "cancelled"):
            self._cancel_event(
                id, "failed", reason="claim_lost", subject=entry.subject,
            )
            raise InvalidInput(
                f"cannot cancel {id}: a dispatcher just claimed it",
                operation_id=id,
            )
        entry.status = "cancelled"
        self._deps.schedules.update("cancelled", entry)
        self._cancel_event(id, "cancelled", subject=entry.subject)
        return CancelReceipt(
            id=id, status="cancelled", subject=entry.subject,
            was_due=entry.send_at,
        )

    def doctor(self) -> dict:
        return self._deps.operations.doctor()

    def transport_check(self) -> dict:
        return self._deps.operations.transport_check()

    @staticmethod
    def _valid_iso_bound(value: str) -> bool:
        if _ISO_BOUND_RE.fullmatch(value):
            return True
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    def audit(self, *, since: str | None = None,
              until: str | None = None, tool: str | None = None,
              event: str | None = None, plan_id: str | None = None,
              operation_id: str | None = None,
              limit: int = 50) -> AuditPage:
        for name, value in (("since", since), ("until", until)):
            if value is not None and not self._valid_iso_bound(str(value)):
                raise InvalidInput(
                    f"invalid ISO datetime for `{name}`: {value!r} "
                    "(want ISO-8601; prefixes allowed, e.g. "
                    "2026-07 or 2026-07-29)"
                )
        return AuditPage(**self._deps.operations.audit(
            since=since, until=until, tool=tool, event=event,
            plan_id=plan_id, operation_id=operation_id, limit=limit,
        ))
