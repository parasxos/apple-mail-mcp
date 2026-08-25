"""Filesystem spool, launchd status, and Exchange scheduling adapters."""
from __future__ import annotations

from .. import config, graph, identities, spool
from ..application.models import ScheduleListing
from ..application.ports import (
    BackgroundIdentityError,
    BackgroundProviderError,
)
from ..domain.models import ScheduledEntry
from .queue import queue_integrity


class FileScheduleStore:
    states = tuple(spool.STATES)

    def dispatcher_installed(self) -> bool:
        return config.dispatcher_plist().exists()

    def listing(self, state: str | None, limit: int) -> ScheduleListing:
        states = [state] if state else list(spool.STATES)
        scans = spool.scan_all(states)
        return ScheduleListing(
            dispatcher_installed=self.dispatcher_installed(),
            dispatcher_label=config.LAUNCHD_LABEL,
            entries={result.state: result.entries[-limit:] for result in scans},
            integrity=queue_integrity(scans),
        )

    def find(self, operation_id: str):
        return spool.find(operation_id)

    def claim(self, operation_id: str, old: str, new: str) -> bool:
        return spool.claim(operation_id, old, new)

    def update(self, state: str, entry: ScheduledEntry) -> None:
        spool.update(state, entry)

    def mark_delivered_now(self, entry: ScheduledEntry) -> None:
        entry.delivered_at = spool.iso(spool.utcnow())
        entry.next_attempt_at = None
        entry.last_error = None
        entry.status = "sent"


class DefaultIdentityResolver:
    def resolve(self, name: str) -> object:
        try:
            return identities.get(name)
        except identities.IdentityError as error:
            raise BackgroundIdentityError(
                str(error), code=error.code,
            ) from error


class GraphDeferredDelivery:

    def find_draft(self, identity: object, message_id: str) -> str | None:
        try:
            return graph.find_draft_by_message_id(identity, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(
                str(error), code=error.code,
            ) from error

    def delete_draft(self, identity: object, draft_id: str) -> str:
        try:
            return graph.delete_draft(identity, draft_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(
                str(error), code=error.code,
            ) from error

    def was_sent(self, identity: object, message_id: str) -> bool:
        try:
            return graph.sent_by_message_id(identity, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(
                str(error), code=error.code,
            ) from error

    def status(self, identity: object, draft_id: str,
               message_id: str) -> str:
        try:
            return graph.draft_status(identity, draft_id, message_id)
        except graph.GraphError as error:
            raise BackgroundProviderError(
                str(error), code=error.code,
            ) from error


# Compatibility name for the first ports-and-adapters branch revision.
GraphDeferredScheduler = GraphDeferredDelivery
