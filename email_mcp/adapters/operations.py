"""Diagnostic and audit-query adapters."""
from __future__ import annotations

from .. import audit, doctor, envelope
from ..application.models import (
    AuditPage,
    AuditQuery,
    DoctorReport,
    TransportReport,
)


class DefaultOperationsGateway:
    def doctor(self) -> DoctorReport:
        value = doctor.run()
        return DoctorReport(
            ok=value["ok"],
            read_only=value["read_only"],
            checks=value["checks"],
            audit=value["audit"],
        )

    def transport_check(self) -> TransportReport:
        value = doctor.check_transports()
        return TransportReport(
            ok=value["ok"],
            detail=value["detail"],
            identities=value["identities"],
            default=value.get("default"),
            fix=value.get("fix"),
            advisory=value.get("advisory"),
        )

    def audit(self, query: AuditQuery) -> AuditPage:
        value = audit.query(
            since=query.since,
            until=query.until,
            tool=query.tool,
            event=query.event,
            plan_id=query.plan_id,
            operation_id=query.operation_id,
            limit=query.limit,
        )
        return AuditPage(**value)

    def classify(self, error: BaseException) -> str:
        return envelope.classify(error)
