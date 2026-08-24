"""Diagnostic and audit-query adapters."""
from __future__ import annotations

from typing import Any

from .. import audit, doctor, envelope


class DefaultOperationsGateway:
    def doctor(self) -> dict:
        return doctor.run()

    def transport_check(self) -> dict:
        return doctor.check_transports()

    def audit(self, **filters: Any) -> dict:
        return audit.query(**filters)

    def classify(self, error: BaseException) -> str:
        return envelope.classify(error)
