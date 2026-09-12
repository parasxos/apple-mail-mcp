"""Diagnostics and audit-query use cases."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..domain.errors import InvalidInput
from .background import parse_timestamp
from .models import AuditPage, AuditQuery, DoctorReport, TransportReport
from .ports import OperationsGateway

_CALENDAR_PREFIX = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def bound_interval(value: str) -> tuple[datetime, datetime]:
    """The closed UTC interval an audit since/until bound denotes.

    A calendar prefix (2026, 2026-07, 2026-07-29) spans its whole period;
    a full timestamp names one instant (offset or Z honoured, naive means
    UTC), so equivalent spellings denote the same interval. ValueError
    for anything else. The one place the bound grammar lives: the use
    case, the ledger reader and the CLI all ask here.
    """
    match = _CALENDAR_PREFIX.fullmatch(value)
    if match is None:
        instant = parse_timestamp(value)
        if instant is None:
            raise ValueError(
                f"invalid ISO-8601 bound {value!r} "
                "(prefixes allowed, e.g. 2026-07 or 2026-07-29)"
            )
        return instant, instant
    year, month, day = (int(g) if g else 0 for g in match.groups())
    start = datetime(year, month or 1, day or 1, tzinfo=timezone.utc)
    if day:
        end = start + timedelta(days=1)
    elif month:
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return start, end - timedelta(microseconds=1)


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
