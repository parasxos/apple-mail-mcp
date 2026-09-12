"""Scheduled-delivery use cases, independent of launchd and providers.

The worker is deliberately a one-pass application service.  The outer
launchd adapter decides when it runs; this layer owns retry, recovery and
the safety rules that prevent local and Exchange delivery from racing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ..domain import codes
from ..domain.errors import SpoolBusy
from ..domain.events import EventPublisher
from ..domain.models import DeliveryReport, ScheduledEntry
from .base import ApplicationService
from .models import DispatchSummary
from .ports import (
    BackgroundDeliveryError,
    BackgroundIdentityError,
    BackgroundProviderError,
    Clock,
    DeferredDelivery,
    DispatchQueue,
    IdentityResolver,
    LocalDelivery,
    UserNotifier,
)

BACKOFF_MINUTES = (2, 5, 15, 45, 120)
STALE_SENDING_MINUTES = 10
GRAPH_GRACE_MINUTES = 10
DISPATCHER = "dispatcher"  # the ownership name of the whole run
SPOOL_HELD = "another dispatcher holds the spool"
RECORD_HELD = (
    "graph: record held elsewhere (schedule or cancel in flight) — skipped"
)
SUPERSEDED = "graph: record moved before this pass owned it — skipped"


def parse_timestamp(stamp: str | None) -> datetime | None:
    """Parse stored timestamps defensively; naive legacy values mean UTC."""
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else value.replace(
        tzinfo=timezone.utc,
    )


def is_due(entry: ScheduledEntry, now: datetime) -> bool:
    send_at = parse_timestamp(entry.send_at)
    if send_at is not None and send_at > now:
        return False
    next_attempt = parse_timestamp(entry.next_attempt_at)
    return next_attempt is None or next_attempt <= now


def is_stale(entry: ScheduledEntry, now: datetime) -> bool:
    """A sending/ claim is stranded once its lease is STALE_SENDING_MINUTES
    old. The lease is the stamp the claim itself wrote, never the schedule
    time: an overdue message that was just claimed is active, not
    abandoned. A manifest without a stamp predates the lease and the run
    that took it is long over."""
    claimed = parse_timestamp(entry.claimed_at)
    return claimed is None or now - claimed >= timedelta(
        minutes=STALE_SENDING_MINUTES,
    )


class BackgroundUseCases(ApplicationService):
    """Recover and dispatch scheduled messages through injected ports."""

    def __init__(
        self,
        *,
        queue: DispatchQueue,
        clock: Clock,
        identities: IdentityResolver,
        delivery: LocalDelivery,
        deferred: DeferredDelivery,
        notifier: UserNotifier,
        events: EventPublisher,
        max_retries: int,
    ) -> None:
        super().__init__(events)
        self._queue = queue
        self._clock = clock
        self._identities = identities
        self._delivery = delivery
        self._deferred = deferred
        self._notifier = notifier
        self._max_retries = max(1, int(max_retries))

    def _fail_or_retry(
        self,
        entry: ScheduledEntry,
        error: str,
        now: datetime,
        source: str = "sending",
    ) -> str:
        entry.attempts += 1
        entry.last_error = error
        if entry.attempts >= self._max_retries:
            self._queue.move(entry, source, "failed")
            self._notifier.notify(
                "email-mcp: send FAILED",
                f"{entry.subject!r} to {', '.join(entry.to)} — {error[:120]}",
            )
            outcome, note = "failed", "failed"
        else:
            delay = BACKOFF_MINUTES[min(
                entry.attempts - 1, len(BACKOFF_MINUTES) - 1,
            )]
            entry.next_attempt_at = self._clock.format(
                now + timedelta(minutes=delay),
            )
            self._queue.move(entry, source, "pending")
            outcome, note = "retry", f"retry in {delay}m"
        self._event(
            "deliver", outcome, operation_id=entry.id,
            spool_id=entry.id, identity=entry.identity,
            subject=entry.subject,
            detail={"attempts": entry.attempts, "error": error[:300]},
        )
        return note

    def _park_partial(
        self, entry: ScheduledEntry, report: DeliveryReport,
    ) -> str:
        """A partial refusal is terminal, never a retry: the accepted
        recipients already hold the message, so a whole resend would
        duplicate it. The manifest keeps who got it and who did not."""
        entry.attempts += 1
        entry.next_attempt_at = None
        entry.code = codes.PARTIAL_DELIVERY
        entry.last_error = report.verdict
        self._queue.move(entry, "sending", "failed")
        self._notifier.notify(
            "email-mcp: send PARTIAL",
            f"{entry.subject!r} — refused: {', '.join(report.refused)}",
        )
        self._event(
            "deliver", "partial", operation_id=entry.id,
            spool_id=entry.id, identity=entry.identity,
            message_id=entry.message_id, to=entry.to,
            subject=entry.subject,
            detail={"code": codes.PARTIAL_DELIVERY,
                    "accepted": report.accepted, "refused": report.refused},
        )
        return "partial delivery — parked in failed/"

    def recover_stranded(self, now: datetime) -> list[str]:
        recovered: list[str] = []
        for entry in self._queue.entries("sending"):
            if not is_stale(entry, now):
                continue
            entry.attempts += 1
            entry.last_error = entry.last_error or (
                "dispatcher died mid-delivery (recovered from sending/)"
            )
            if entry.attempts >= self._max_retries:
                self._queue.move(entry, "sending", "failed")
                self._event(
                    "recover", "failed", operation_id=entry.id,
                    spool_id=entry.id, subject=entry.subject,
                    detail={"attempts": entry.attempts},
                )
            else:
                entry.next_attempt_at = self._clock.format(now)
                self._queue.move(entry, "sending", "pending")
                self._event(
                    "recover", "requeued", operation_id=entry.id,
                    spool_id=entry.id, subject=entry.subject,
                    detail={"attempts": entry.attempts},
                )
                recovered.append(entry.id)
        return recovered

    def graph_mark_sent(self, entry: ScheduledEntry, now: datetime) -> str:
        entry.delivered_at = self._clock.format(now)
        entry.next_attempt_at = None
        entry.last_error = None
        self._queue.move(entry, "pending", "sent")
        self._event(
            "graph_sent", "sent", operation_id=entry.id,
            spool_id=entry.id, identity=entry.identity,
            message_id=entry.message_id, subject=entry.subject,
        )
        return "sent (delivered by Exchange)"

    def graph_adopt(self, entry: ScheduledEntry, draft_id: str) -> str:
        entry.graph_draft_id = draft_id
        entry.last_error = None
        self._queue.update("pending", entry)
        self._event(
            "graph_adopt", "adopted", operation_id=entry.id,
            spool_id=entry.id, draft_id=draft_id,
            message_id=entry.message_id,
        )
        return "graph: adopted existing draft"

    def graph_flip_to_local(
        self,
        entry: ScheduledEntry,
        now: datetime,
        reason: str,
        clear_draft: bool,
    ) -> str:
        entry.executor = "launchd"
        if clear_draft:
            entry.graph_draft_id = None
        entry.next_attempt_at = self._clock.format(now)
        entry.last_error = None
        self._queue.update("pending", entry)
        self._event(
            "graph_flip", "flipped", operation_id=entry.id,
            spool_id=entry.id, message_id=entry.message_id,
            detail={"reason": reason},
        )
        return f"graph: {reason} — local delivery next pass"

    def graph_leave(
        self,
        entry: ScheduledEntry,
        error: str,
        note: str,
    ) -> str:
        entry.last_error = error
        self._queue.update("pending", entry)
        return note

    def graph_apply_status(
        self,
        entry: ScheduledEntry,
        status: str,
        now: datetime,
    ) -> str:
        if status == "sent":
            return self.graph_mark_sent(entry, now)
        if status == "cancelled_externally":
            entry.next_attempt_at = None
            entry.last_error = (
                "deferred draft was discarded outside the spool (e.g. in "
                "Outlook/OWA Drafts) — not sent, not sendable locally"
            )
            self._queue.move(entry, "pending", "cancelled")
            self._event(
                "graph_cancelled_external", "cancelled",
                operation_id=entry.id, spool_id=entry.id,
                message_id=entry.message_id, subject=entry.subject,
            )
            return "cancelled externally (draft discarded in Outlook/OWA)"
        return f"graph: status {status} — left for next pass"

    def reconcile_deferred(self, now: datetime) -> dict[str, str]:
        results: dict[str, str] = {}
        grace = timedelta(minutes=GRAPH_GRACE_MINUTES)
        for entry in self._queue.entries("pending"):
            if entry.executor != "graph":
                continue
            send_at = parse_timestamp(entry.send_at) or now - grace
            if now < send_at + grace:
                continue
            next_attempt = parse_timestamp(entry.next_attempt_at)
            if next_attempt is not None and next_attempt > now:
                continue
            # Ownership spans probe → rewrite, so a cancel cannot move the
            # record under this pass and this pass cannot overwrite a
            # cancel; a record someone else holds waits for the next pass.
            # The listing was taken before ownership, so the record is
            # re-read under it: a cancel that landed in between has moved
            # it, and a stale copy must never be written back.
            try:
                with self._queue.own(entry.id):
                    current = self._queue.load("pending", entry.id)
                    results[entry.id] = (
                        self._reconcile_one(current, now)
                        if current is not None and current.executor == "graph"
                        else SUPERSEDED
                    )
            except SpoolBusy:
                results[entry.id] = RECORD_HELD
        return results

    def _reconcile_one(self, entry: ScheduledEntry, now: datetime) -> str:
        try:
            identity = self._identities.resolve(entry.identity)
        except BackgroundIdentityError as error:
            return self._fail_or_retry(
                entry, str(error), now, source="pending",
            )

        if not entry.graph_draft_id:
            try:
                draft_id = self._deferred.find_draft(
                    identity, entry.message_id,
                )
            except BackgroundProviderError as error:
                return self.graph_leave(
                    entry, str(error),
                    "graph: drafts lookup failed — retrying",
                )
            if draft_id is not None:
                return self.graph_adopt(entry, draft_id)
            try:
                sent = self._deferred.was_sent(identity, entry.message_id)
            except BackgroundProviderError as error:
                return self.graph_leave(
                    entry, str(error),
                    "graph: sent-items lookup failed — retrying",
                )
            if sent:
                return self.graph_mark_sent(entry, now)
            return self.graph_flip_to_local(
                entry, now, "no draft found", clear_draft=False,
            )

        try:
            status = self._deferred.status(
                identity, entry.graph_draft_id, entry.message_id,
            )
        except BackgroundProviderError as error:
            return self.graph_leave(
                entry, str(error), "graph: unreachable — retrying next pass",
            )
        if status != "held":
            return self.graph_apply_status(entry, status, now)

        try:
            outcome = self._deferred.delete_draft(
                identity, entry.graph_draft_id,
            )
        except BackgroundProviderError as error:
            return self.graph_leave(
                entry, str(error), "graph: draft revoke failed — retrying",
            )
        if outcome == "deleted":
            return self.graph_flip_to_local(
                entry, now, "draft revoked", clear_draft=True,
            )
        try:
            status = self._deferred.status(
                identity, entry.graph_draft_id, entry.message_id,
            )
        except BackgroundProviderError as error:
            return self.graph_leave(
                entry, str(error),
                "graph: draft gone, outcome ambiguous — retrying",
            )
        return self.graph_apply_status(entry, status, now)

    def dispatch_scheduled(
        self, now: datetime | None = None,
    ) -> DispatchSummary:
        now = now or self._clock.now()
        # One pass per spool: recovery, reconcile and dispatch run under
        # one ownership, so an overlapping pass can never judge this
        # pass's fresh claims stranded — it is refused, not interleaved.
        try:
            with self._queue.own(DISPATCHER):
                return self._dispatch(now)
        except SpoolBusy:
            return DispatchSummary(
                checked_at=self._clock.format(now), due=0, results={},
                skipped=SPOOL_HELD,
            )

    def _dispatch(self, now: datetime) -> DispatchSummary:
        self.recover_stranded(now)
        due = [
            entry for entry in self._queue.entries("pending")
            if entry.executor != "graph" and is_due(entry, now)
        ]
        results = self.reconcile_deferred(now)
        if not due:
            integrity = self._queue.integrity()
            return DispatchSummary(
                checked_at=self._clock.format(now),
                due=0,
                results=results,
                integrity=None if integrity.ok else integrity,
            )

        ready: dict[str, tuple[bool, str | None, object | None]] = {}

        def transport_ready(
            name: str,
        ) -> tuple[bool, str | None, object | None]:
            if name not in ready:
                try:
                    identity = self._identities.resolve(name)
                except BackgroundIdentityError as error:
                    ready[name] = (False, str(error), None)
                else:
                    ok, error = self._delivery.preflight(identity)
                    ready[name] = (ok, error, identity)
            return ready[name]

        for entry in due:
            if not self._queue.claim(entry.id):
                results[entry.id] = "claimed elsewhere"
                continue
            entry.status = "sending"
            transport_ok, transport_error, identity = transport_ready(
                entry.identity,
            )
            if not transport_ok:
                results[entry.id] = self._fail_or_retry(
                    entry, transport_error or "transport unavailable", now,
                )
                continue
            try:
                raw = self._queue.read_message("sending", entry.id)
            except OSError as error:
                entry.last_error = (
                    "spool .eml missing" if isinstance(error, FileNotFoundError)
                    else f"spool .eml unreadable: {error}"
                )
                self._queue.move(entry, "sending", "failed")
                self._event(
                    "deliver", "failed", operation_id=entry.id,
                    spool_id=entry.id, identity=entry.identity,
                    subject=entry.subject,
                    detail={"code": codes.SPOOL_EML_MISSING},
                )
                results[entry.id] = "failed"
                continue
            try:
                report = self._delivery.deliver(
                    identity, raw, entry.to + entry.cc + entry.bcc,
                )
            except BackgroundDeliveryError as error:
                results[entry.id] = self._fail_or_retry(entry, str(error), now)
                continue
            entry.accepted, entry.refused = report.accepted, report.refused
            if report.partial:
                results[entry.id] = self._park_partial(entry, report)
                continue
            entry.delivered_at = self._clock.format(self._clock.now())
            entry.next_attempt_at = None
            entry.last_error = None
            self._queue.move(entry, "sending", "sent")
            self._event(
                "deliver", "sent", operation_id=entry.id,
                spool_id=entry.id, identity=entry.identity,
                message_id=entry.message_id, to=entry.to,
                subject=entry.subject,
            )
            results[entry.id] = "sent"

        integrity = self._queue.integrity()
        return DispatchSummary(
            checked_at=self._clock.format(now),
            due=len(due),
            results=results,
            integrity=None if integrity.ok else integrity,
        )
