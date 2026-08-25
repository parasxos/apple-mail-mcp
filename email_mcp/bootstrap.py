"""The sole production composition root for application capabilities."""
from __future__ import annotations

from threading import Lock

from . import config
from .adapters.background import (
    DefaultLocalDelivery,
    MacOSNotifier,
    SpoolDispatchQueue,
    SystemClock,
)
from .adapters.delivery import DefaultDeliveryGateway
from .adapters.events import AuditEventPublisher
from .adapters.operations import DefaultOperationsGateway
from .adapters.refresh import AppleMailRefreshGateway
from .adapters.scheduling import (
    DefaultIdentityResolver,
    FileScheduleStore,
    GraphDeferredDelivery,
)
from .adapters.source import FixedSourceProvider, LazySourceProvider
from .adapters.triage import AppleMailTriageGateway
from .application.background import BackgroundUseCases
from .application.delivery import DeliveryUseCases
from .application.operations import OperationsUseCases
from .application.reads import ReadUseCases
from .application.scheduling import SchedulingUseCases
from .application.service import EmailApplication
from .application.triage import TriageUseCases

_application: EmailApplication | None = None
_lock = Lock()


def build_application(
    *,
    source=None,
    source_provider=None,
    delivery=None,
    schedules=None,
    identities=None,
    deferred=None,
    triage=None,
    refresh=None,
    operations=None,
    classifier=None,
    events=None,
    clock=None,
    dispatch_queue=None,
    local_delivery=None,
    notifier=None,
    max_retries: int | None = None,
) -> EmailApplication:
    """Wire production defaults while allowing explicit boundary fakes."""
    if source is not None and source_provider is not None:
        raise ValueError("pass source or source_provider, not both")

    source_port = source_provider if source_provider is not None else (
        FixedSourceProvider(source) if source is not None
        else LazySourceProvider()
    )
    event_port = events if events is not None else AuditEventPublisher()
    operations_port = (
        operations if operations is not None else DefaultOperationsGateway()
    )
    classifier_port = (
        classifier if classifier is not None else operations_port
    )
    identity_port = (
        identities if identities is not None else DefaultIdentityResolver()
    )
    deferred_port = (
        deferred if deferred is not None else GraphDeferredDelivery()
    )

    reads = ReadUseCases(
        source=source_port,
        refresh=(refresh if refresh is not None else AppleMailRefreshGateway()),
        classifier=classifier_port,
    )
    delivery_use_cases = DeliveryUseCases(
        source=source_port,
        delivery=(delivery if delivery is not None
                  else DefaultDeliveryGateway()),
        events=event_port,
    )
    scheduling = SchedulingUseCases(
        schedules=(schedules if schedules is not None
                   else FileScheduleStore()),
        identities=identity_port,
        deferred=deferred_port,
        events=event_port,
    )
    triage_use_cases = TriageUseCases(
        source=source_port,
        triage=(triage if triage is not None else AppleMailTriageGateway()),
        events=event_port,
    )
    operations_use_cases = OperationsUseCases(operations=operations_port)
    background = BackgroundUseCases(
        queue=(dispatch_queue if dispatch_queue is not None
               else SpoolDispatchQueue()),
        clock=(clock if clock is not None else SystemClock()),
        identities=identity_port,
        delivery=(local_delivery if local_delivery is not None
                  else DefaultLocalDelivery()),
        deferred=deferred_port,
        notifier=(notifier if notifier is not None else MacOSNotifier()),
        events=event_port,
        max_retries=(config.send_max_retries()
                     if max_retries is None else max_retries),
    )
    return EmailApplication(
        reads=reads,
        delivery=delivery_use_cases,
        scheduling=scheduling,
        triage=triage_use_cases,
        operations=operations_use_cases,
        background=background,
    )


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
