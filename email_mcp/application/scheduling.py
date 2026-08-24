"""Scheduled-mail listing and cancellation use cases."""
from __future__ import annotations

from ..domain.errors import InvalidInput, NotFound, ToolError
from .base import ApplicationService
from .models import CancelReceipt
from .query import check_page


class SchedulingUseCases(ApplicationService):
    def list_scheduled(
        self,
        state: str | None = None,
        limit: int = 50,
    ) -> dict:
        if state and state not in self._deps.schedules.states:
            raise InvalidInput(
                f"unknown state {state!r} "
                f"(want one of {self._deps.schedules.states})"
            )
        check_page(limit)
        return self._deps.schedules.listing(state, limit)

    def dispatcher_installed(self) -> bool:
        return self._deps.schedules.dispatcher_installed()

    def _cancel_event(
        self,
        operation_id: str,
        outcome: str,
        *,
        reason: str | None = None,
        subject: str | None = None,
        **extra,
    ) -> None:
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
                outcome = (
                    self._deps.deferred.delete_draft(identity, draft_id)
                    if draft_id else "gone"
                )
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
                    self._cancel_event(
                        id, "too_late_sent", subject=entry.subject,
                    )
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
