"""Apple Mail triage adapter."""
from __future__ import annotations

from .. import config, triage


class AppleMailTriageGateway:
    def plan_cap(self) -> int:
        return config.triage_max_messages()

    def delete_cap(self) -> int:
        return triage.delete_max()

    def build(self, source, query, actions):
        return triage.build_plan(source, query, actions)

    def build_delete(self, source, query):
        return triage.build_delete_plan(source, query)

    def apply(self, source, plan_id: str) -> dict:
        return triage.apply_plan(source, plan_id)

    def create_mailbox(self, source, account: str, path: str) -> dict:
        return triage.create_mailbox(source, account, path)

    def delete_mailbox(self, source, account: str, path: str) -> dict:
        return triage.delete_mailbox(source, account, path)
