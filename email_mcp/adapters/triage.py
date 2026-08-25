"""Apple Mail triage adapter."""
from __future__ import annotations

from .. import config, triage
from ..application.models import (
    MailboxCreateResult,
    MailboxDeleteResult,
    TriageApplyResult,
    TriageFailure,
)


class AppleMailTriageGateway:
    def plan_cap(self) -> int:
        return config.triage_max_messages()

    def delete_cap(self) -> int:
        return triage.delete_max()

    def build(self, source, query, actions):
        return triage.build_plan(source, query, actions)

    def build_delete(self, source, query):
        return triage.build_delete_plan(source, query)

    def apply(self, source, plan_id: str) -> TriageApplyResult:
        value = triage.apply_plan(source, plan_id)
        return TriageApplyResult(
            plan_id=value["plan_id"],
            status=value["status"],
            planned=value["planned"],
            acted=value["acted"],
            failures=[TriageFailure(**item) for item in value["failures"]],
            verified=value["verified"],
            pending=value["pending"],
            osascript_ms=value["osascript_ms"],
            verify_polls=value["verify_polls"],
            duration_ms=value["duration_ms"],
            note=value.get("note"),
        )

    def create_mailbox(self, source, account: str,
                       path: str) -> MailboxCreateResult:
        value = triage.create_mailbox(source, account, path)
        return MailboxCreateResult(
            account=value["account"],
            path=value["path"],
            existed=value["existed"],
            applescript=value.get("applescript"),
            index_verified=value["index_verified"],
            mail_verified=value["mail_verified"],
            warning=value.get("warning"),
        )

    def delete_mailbox(self, source, account: str,
                       path: str) -> MailboxDeleteResult:
        value = triage.delete_mailbox(source, account, path)
        return MailboxDeleteResult(
            ok=value["ok"],
            account=value["account"],
            path=value["path"],
            existed=value["existed"],
            deleted=value["deleted"],
            mail_verified=value["mail_verified"],
            warning=value.get("warning"),
            method=value.get("method"),
            code=value.get("code"),
            error=value.get("error"),
        )
