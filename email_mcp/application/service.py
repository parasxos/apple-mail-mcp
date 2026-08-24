"""Composite application facade over cohesive email capabilities."""
from __future__ import annotations

from .base import ApplicationDependencies, ApplicationService
from .background import BackgroundUseCases
from .delivery import DeliveryUseCases
from .operations import OperationsUseCases
from .reads import ReadUseCases
from .scheduling import SchedulingUseCases
from .triage import TriageUseCases


class EmailApplication(
    BackgroundUseCases,
    ReadUseCases,
    DeliveryUseCases,
    SchedulingUseCases,
    TriageUseCases,
    OperationsUseCases,
):
    """Stable facade used identically by MCP, CLI, jobs, and tests."""


__all__ = [
    "ApplicationDependencies",
    "ApplicationService",
    "EmailApplication",
]
