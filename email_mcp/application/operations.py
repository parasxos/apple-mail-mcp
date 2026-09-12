"""Diagnostics and audit-query use cases."""
from __future__ import annotations

from ..domain.errors import InvalidInput
from ..domain.ids import bound_interval
from .models import AuditPage, AuditQuery, DoctorReport, TransportReport
from .ports import OperationsGateway


class OperationsUseCases:
    def __init__(self, *, operations: OperationsGateway) -> None:
        self._operations = operations

    def doctor(self) -> DoctorReport:
        return self._operations.doctor()

    def transport_check(self) -> TransportReport:
        return self._operations.transport_check()

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
            if value is None:
                continue
            try:
                bound_interval(str(value))
            except ValueError as exc:
                raise InvalidInput(f"`{name}`: {exc}") from None
        return self._operations.audit(AuditQuery(
            since=since, until=until, tool=tool, event=event,
            plan_id=plan_id, operation_id=operation_id, limit=limit,
        ))
