"""Filesystem spool, launchd status, and Exchange scheduling adapters."""
from __future__ import annotations

from .. import codes, graph, identities, spool
from ..dispatcher import LAUNCHD_LABEL, _plist_path
from ..domain.models import ScheduledEntry


class FileScheduleStore:
    states = tuple(spool.STATES)

    def dispatcher_installed(self) -> bool:
        return _plist_path().exists()

    def listing(self, state: str | None, limit: int) -> dict:
        states = [state] if state else list(spool.STATES)
        scans = spool.scan_all(states)
        integrity = spool.integrity(scans)
        out = {
            "dispatcher_installed": self.dispatcher_installed(),
            "dispatcher_label": LAUNCHD_LABEL,
            **{result.state: result.entries[-limit:] for result in scans},
        }
        if not integrity["ok"]:
            out.update({
                "ok": False,
                "code": codes.SPOOL_INTEGRITY,
                "error": (
                    f"scheduled-mail storage has {len(integrity['issues'])} "
                    "integrity issue(s); readable records are included below "
                    "and damaged records were left untouched"
                ),
                "fix": ("run `email-mcp dispatcher --status`, then reconcile "
                        "every path in integrity.issues before deleting or "
                        "rescheduling mail"),
                "integrity": integrity,
            })
        return out

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


class GraphDeferredScheduler:
    def identity(self, name: str):
        return identities.get(name)

    def find_draft(self, identity, message_id: str) -> str | None:
        return graph.find_draft_by_message_id(identity, message_id)

    def delete_draft(self, identity, draft_id: str) -> str:
        return graph.delete_draft(identity, draft_id)

    def was_sent(self, identity, message_id: str) -> bool:
        return graph.sent_by_message_id(identity, message_id)
