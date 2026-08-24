"""Scheduled-delivery use cases, independent of launchd and providers.

The worker is deliberately a one-pass application service.  The outer
launchd adapter decides when it runs; this layer owns retry, recovery and
the safety rules that prevent local and Exchange delivery from racing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain import codes
from ..domain.models import ScheduledEntry
from .base import ApplicationService
from .ports import (
    BackgroundDeliveryError,
    BackgroundGateway,
    BackgroundIdentityError,
    BackgroundProviderError,
)

BACKOFF_MINUTES = (2, 5, 15, 45, 120)
STALE_SENDING_MINUTES = 10
GRAPH_GRACE_MINUTES = 10
SUPERSEDED = "graph: entry changed under us — skipped (another pass won)"


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


class BackgroundUseCases(ApplicationService):
    """Recover and dispatch scheduled messages through injected ports."""

    @property
    def _background(self) -> BackgroundGateway:
        gateway = self._deps.background
        if gateway is None:
            raise RuntimeError("background delivery is not configured")
        return gateway

    def _fail_or_retry(
        self,
        entry: ScheduledEntry,
        error: str,
        now: datetime,
        source: str = "sending",
    ) -> str:
        entry.attempts += 1
        entry.last_error = error
        if entry.attempts >= self._background.max_retries():
            self._background.move(entry, source, "failed")
            self._background.notify(
                "email-mcp: send FAILED",
                f"{entry.subject!r} to {', '.join(entry.to)} — {error[:120]}",
            )
            outcome, note = "failed", "failed"
        else:
            delay = BACKOFF_MINUTES[min(
                entry.attempts - 1, len(BACKOFF_MINUTES) - 1,
            )]
            entry.next_attempt_at = self._background.iso(
                now + timedelta(minutes=delay),
            )
            self._background.move(entry, source, "pending")
            outcome, note = "retry", f"retry in {delay}m"
        self._event(
            "deliver", outcome, operation_id=entry.id,
            spool_id=entry.id, identity=entry.identity,
            subject=entry.subject,
            detail={"attempts": entry.attempts, "error": error[:300]},
        )
        return note

    def recover_stranded(self, now: datetime) -> list[str]:
        recovered: list[str] = []
        for entry in self._background.entries("sending"):
            reference = (
                parse_timestamp(entry.next_attempt_at)
                or parse_timestamp(entry.send_at)
            )
            age = (
                (now - reference).total_seconds() / 60
                if reference else float("inf")
            )
            if age < STALE_SENDING_MINUTES:
                continue
            entry.attempts += 1
            entry.last_error = entry.last_error or (
                "dispatcher died mid-delivery (recovered from sending/)"
            )
            if entry.attempts >= self._background.max_retries():
                self._background.move(entry, "sending", "failed")
                self._event(
                    "recover", "failed", operation_id=entry.id,
                    spool_id=entry.id, subject=entry.subject,
                    detail={"attempts": entry.attempts},
                )
            else:
                entry.next_attempt_at = self._background.iso(now)
                self._background.move(entry, "sending", "pending")
                self._event(
                    "recover", "requeued", operation_id=entry.id,
                    spool_id=entry.id, subject=entry.subject,
                    detail={"attempts": entry.attempts},
                )
                recovered.append(entry.id)
        return recovered

    def graph_current(self, entry: ScheduledEntry) -> bool:
        try:
            current = self._background.load("pending", entry.id)
        except Exception:
            return False
        return (
            current is not None
            and current.executor == "graph"
            and current.graph_draft_id == entry.graph_draft_id
        )

    def graph_mark_sent(self, entry: ScheduledEntry, now: datetime) -> str:
        if not self.graph_current(entry):
            return SUPERSEDED
        entry.delivered_at = self._background.iso(now)
        entry.next_attempt_at = None
        entry.last_error = None
        self._background.move(entry, "pending", "sent")
        self._event(
            "graph_sent", "sent", operation_id=entry.id,
            spool_id=entry.id, identity=entry.identity,
            message_id=entry.message_id, subject=entry.subject,
        )
        return "sent (delivered by Exchange)"

    def graph_adopt(self, entry: ScheduledEntry, draft_id: str) -> str:
        if not self.graph_current(entry):
            return SUPERSEDED
        entry.graph_draft_id = draft_id
        entry.last_error = None
        self._background.update("pending", entry)
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
        if not self.graph_current(entry):
            return SUPERSEDED
        entry.executor = "launchd"
        if clear_draft:
            entry.graph_draft_id = None
        entry.next_attempt_at = self._background.iso(now)
        entry.last_error = None
        self._background.update("pending", entry)
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
        if not self.graph_current(entry):
            return SUPERSEDED
        entry.last_error = error
        self._background.update("pending", entry)
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
            if not self.graph_current(entry):
                return SUPERSEDED
            entry.next_attempt_at = None
            entry.last_error = (
                "deferred draft was discarded outside the spool (e.g. in "
                "Outlook/OWA Drafts) — not sent, not sendable locally"
            )
            self._background.move(entry, "pending", "cancelled")
            self._event(
                "graph_cancelled_external", "cancelled",
                operation_id=entry.id, spool_id=entry.id,
                message_id=entry.message_id, subject=entry.subject,
            )
            return "cancelled externally (draft discarded in Outlook/OWA)"
        return f"graph: status {status} — left for next pass"

    def reconcile_deferred(self, now: datetime) -> dict[str, str]:
        entries = [
            entry for entry in self._background.entries("pending")
            if entry.executor == "graph"
        ]
        results: dict[str, str] = {}
        grace = timedelta(minutes=GRAPH_GRACE_MINUTES)
        for entry in entries:
            send_at = parse_timestamp(entry.send_at) or now - grace
            if now < send_at + grace:
                continue
            next_attempt = parse_timestamp(entry.next_attempt_at)
            if next_attempt is not None and next_attempt > now:
                continue
            try:
                identity = self._background.identity(entry.identity)
            except BackgroundIdentityError as error:
                results[entry.id] = self._fail_or_retry(
                    entry, str(error), now, source="pending",
                )
                continue

            if not entry.graph_draft_id:
                try:
                    draft_id = self._background.find_deferred_draft(
                        identity, entry.message_id,
                    )
                except BackgroundProviderError as error:
                    results[entry.id] = self.graph_leave(
                        entry, str(error),
                        "graph: drafts lookup failed — retrying",
                    )
                    continue
                if draft_id is not None:
                    results[entry.id] = self.graph_adopt(entry, draft_id)
                    continue
                try:
                    sent = self._background.deferred_was_sent(
                        identity, entry.message_id,
                    )
                except BackgroundProviderError as error:
                    results[entry.id] = self.graph_leave(
                        entry, str(error),
                        "graph: sent-items lookup failed — retrying",
                    )
                    continue
                if sent:
                    results[entry.id] = self.graph_mark_sent(entry, now)
                    continue
                results[entry.id] = self.graph_flip_to_local(
                    entry, now, "no draft found", clear_draft=False,
                )
                continue

            try:
                status = self._background.deferred_status(
                    identity, entry.graph_draft_id, entry.message_id,
                )
            except BackgroundProviderError as error:
                results[entry.id] = self.graph_leave(
                    entry, str(error),
                    "graph: unreachable — retrying next pass",
                )
                continue
            if status != "held":
                results[entry.id] = self.graph_apply_status(entry, status, now)
                continue

            try:
                outcome = self._background.delete_deferred_draft(
                    identity, entry.graph_draft_id,
                )
            except BackgroundProviderError as error:
                results[entry.id] = self.graph_leave(
                    entry, str(error),
                    "graph: draft revoke failed — retrying",
                )
                continue
            if outcome == "deleted":
                results[entry.id] = self.graph_flip_to_local(
                    entry, now, "draft revoked", clear_draft=True,
                )
                continue
            try:
                status = self._background.deferred_status(
                    identity, entry.graph_draft_id, entry.message_id,
                )
            except BackgroundProviderError as error:
                results[entry.id] = self.graph_leave(
                    entry, str(error),
                    "graph: draft gone, outcome ambiguous — retrying",
                )
                continue
            results[entry.id] = self.graph_apply_status(entry, status, now)
        return results

    def dispatch_scheduled(self, now: datetime | None = None) -> dict:
        now = now or self._background.now()
        self.recover_stranded(now)
        due = [
            entry for entry in self._background.entries("pending")
            if entry.executor != "graph" and is_due(entry, now)
        ]
        results = self.reconcile_deferred(now)
        if not due:
            summary = {
                "checked_at": self._background.iso(now),
                "due": 0,
                "results": results,
            }
            integrity = self._background.integrity()
            if not integrity["ok"]:
                summary["integrity"] = integrity
            return summary

        ready: dict[str, tuple[bool, str | None, Any | None]] = {}

        def transport_ready(name: str) -> tuple[bool, str | None, Any | None]:
            if name not in ready:
                try:
                    identity = self._background.identity(name)
                except BackgroundIdentityError as error:
                    ready[name] = (False, str(error), None)
                else:
                    ok, error = self._background.preflight(identity)
                    ready[name] = (ok, error, identity)
            return ready[name]

        for entry in due:
            if not self._background.claim(entry.id):
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
                raw = self._background.read_message("sending", entry.id)
            except OSError as error:
                entry.last_error = (
                    "spool .eml missing" if isinstance(error, FileNotFoundError)
                    else f"spool .eml unreadable: {error}"
                )
                self._background.move(entry, "sending", "failed")
                self._event(
                    "deliver", "failed", operation_id=entry.id,
                    spool_id=entry.id, identity=entry.identity,
                    subject=entry.subject,
                    detail={"code": codes.SPOOL_EML_MISSING},
                )
                results[entry.id] = "failed"
                continue
            try:
                self._background.deliver(
                    identity, raw, entry.to + entry.cc + entry.bcc,
                )
            except BackgroundDeliveryError as error:
                results[entry.id] = self._fail_or_retry(entry, str(error), now)
                continue
            entry.delivered_at = self._background.iso(self._background.now())
            entry.next_attempt_at = None
            entry.last_error = None
            self._background.move(entry, "sending", "sent")
            self._event(
                "deliver", "sent", operation_id=entry.id,
                spool_id=entry.id, identity=entry.identity,
                message_id=entry.message_id, to=entry.to,
                subject=entry.subject,
            )
            results[entry.id] = "sent"

        summary = {
            "checked_at": self._background.iso(now),
            "due": len(due),
            "results": results,
        }
        integrity = self._background.integrity()
        if not integrity["ok"]:
            summary["integrity"] = integrity
        return summary
