"""Application composition root.

Only this module knows both the application ports and their production
adapters.  Front ends request the composed application; tests can build one
with explicit fakes without mutating module-level business dependencies.
"""
from __future__ import annotations

from threading import Lock

from .adapters.delivery import DefaultDeliveryGateway
from .adapters.events import AuditEventPublisher
from .adapters.operations import DefaultOperationsGateway
from .adapters.refresh import AppleMailRefreshGateway
from .adapters.scheduling import FileScheduleStore, GraphDeferredScheduler
from .adapters.source import FixedSourceProvider, LazySourceProvider
from .adapters.triage import AppleMailTriageGateway
from .application.service import ApplicationDependencies, EmailApplication

_application: EmailApplication | None = None
_lock = Lock()


def build_application(
    *, source=None, source_provider=None, delivery=None, schedules=None,
    deferred=None, triage=None, refresh=None, operations=None, events=None,
) -> EmailApplication:
    if source is not None and source_provider is not None:
        raise ValueError("pass source or source_provider, not both")
    provider = source_provider if source_provider is not None else (
        FixedSourceProvider(source) if source is not None else LazySourceProvider()
    )
    return EmailApplication(ApplicationDependencies(
        source=provider,
        delivery=(delivery if delivery is not None
                  else DefaultDeliveryGateway()),
        schedules=(schedules if schedules is not None
                   else FileScheduleStore()),
        deferred=(deferred if deferred is not None
                  else GraphDeferredScheduler()),
        triage=(triage if triage is not None else AppleMailTriageGateway()),
        refresh=(refresh if refresh is not None else AppleMailRefreshGateway()),
        operations=(operations if operations is not None
                    else DefaultOperationsGateway()),
        events=(events if events is not None else AuditEventPublisher()),
    ))


def get_application() -> EmailApplication:
    global _application
    if _application is None:
        with _lock:
            if _application is None:
                _application = build_application()
    return _application


def set_application(application: EmailApplication | None) -> None:
    """Explicit process-level override for an alternate front end or test."""
    global _application
    with _lock:
        _application = application
