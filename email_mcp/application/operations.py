"""Diagnostics and audit-query use cases."""
from __future__ import annotations

import re
from datetime import datetime

from ..domain.errors import InvalidInput
from .base import ApplicationService
from .models import AuditPage

_ISO_BOUND_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


class OperationsUseCases(ApplicationService):
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

    def audit(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        tool: str | None = None,
        event: str | None = None,
        plan_id: str | None = None,
        operation_id: str | None = None,
        limit: int = 50,
    ) -> AuditPage:
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
