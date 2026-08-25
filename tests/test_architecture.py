"""Executable dependency and composition rules for the application core."""
from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_type_hints

from email_mcp.application.background import BackgroundUseCases
from email_mcp.application.delivery import DeliveryUseCases
from email_mcp.application.models import QueueIntegrity
from email_mcp.application.reads import ReadUseCases
from email_mcp.domain.events import DomainEvent
from email_mcp.domain.mail import EmailRef
from email_mcp.domain.models import ScheduledEntry, SendRequest, SendResult

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "email_mcp"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.level, node.module
            else:
                # `from . import x, y` names modules, not attributes.
                for alias in node.names:
                    yield node.level, alias.name


def _public_methods(protocol) -> set[str]:
    return {
        name for name, member in inspect.getmembers(protocol)
        if callable(member) and not name.startswith("_")
    }


def test_domain_never_reaches_outward():
    for path in (PACKAGE / "domain").glob("*.py"):
        for level, module in _imports(path):
            assert level < 2, f"{path.name} reaches outside domain: {module}"
            assert not module.startswith("email_mcp"), (
                f"{path.name} has an absolute package dependency: {module}"
            )


def test_application_imports_only_domain_and_its_own_modules():
    application_modules = {
        path.stem for path in (PACKAGE / "application").glob("*.py")
    }
    for path in (PACKAGE / "application").glob("*.py"):
        for level, module in _imports(path):
            if level == 0:
                assert not module.startswith("email_mcp"), (
                    f"{path.name} imports an outer package: {module}"
                )
            elif level == 1:
                assert module.split(".", 1)[0] in application_modules, (
                    f"{path.name} imports outside application: {module}"
                )
            else:
                assert level == 2 and module.split(".", 1)[0] == "domain", (
                    f"{path.name} imports an outer implementation: {module}"
                )


def test_application_import_does_not_load_concrete_integrations():
    script = """
import sys
import email_mcp.application.service
forbidden = {
    'email_mcp.adapters', 'email_mcp.audit', 'email_mcp.bootstrap',
    'email_mcp.graph', 'email_mcp.mcp_api', 'email_mcp.sender',
    'email_mcp.server', 'email_mcp.spool', 'email_mcp.triage',
}
loaded = forbidden.intersection(sys.modules)
if loaded:
    raise SystemExit(','.join(sorted(loaded)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_bootstrap_is_the_only_module_that_imports_concrete_adapters():
    """Every production entry point reaches concrete edges via bootstrap."""
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path == PACKAGE / "bootstrap.py" or "adapters" in path.parts:
            continue
        for level, module in _imports(path):
            absolute = module.startswith("email_mcp.adapters")
            relative = level == 1 and module.split(".", 1)[0] == "adapters"
            if absolute or relative:
                offenders.append(str(path.relative_to(PACKAGE)))
    assert offenders == []


def test_outbound_adapters_never_depend_on_inbound_or_process_adapters():
    forbidden = {
        "bootstrap", "cli", "dispatcher", "dispatcher_runtime",
        "mcp_api", "server",
    }
    for path in (PACKAGE / "adapters").glob("*.py"):
        for level, module in _imports(path):
            root = module.split(".", 1)[0]
            if level >= 2:
                assert root not in forbidden, (
                    f"{path.name} depends on an inbound edge: {module}"
                )
            if level == 0 and module.startswith("email_mcp."):
                root = module.split(".", 2)[1]
                assert root not in forbidden, (
                    f"{path.name} depends on an inbound edge: {module}"
                )


def test_inbound_adapters_do_not_import_concrete_integrations():
    allowed = {
        "application", "audit", "bootstrap", "dispatcher_runtime",
        "domain", "envelope",
    }
    for name in ("mcp_api.py", "dispatcher.py"):
        path = PACKAGE / name
        for level, module in _imports(path):
            if level != 1:
                continue
            root = module.split(".", 1)[0]
            assert root in allowed, f"{name} imports concrete edge {module}"


def test_capabilities_receive_only_the_roles_they_use():
    from email_mcp.application.operations import OperationsUseCases
    from email_mcp.application.scheduling import SchedulingUseCases
    from email_mcp.application.triage import TriageUseCases

    expected = {
        ReadUseCases: {"source", "refresh", "classifier"},
        DeliveryUseCases: {"source", "delivery", "events"},
        SchedulingUseCases: {
            "schedules", "identities", "deferred", "events",
        },
        TriageUseCases: {"source", "triage", "events"},
        OperationsUseCases: {"operations"},
        BackgroundUseCases: {
            "queue", "clock", "identities", "delivery", "deferred",
            "notifier", "events", "max_retries",
        },
    }
    for capability, roles in expected.items():
        parameters = inspect.signature(capability.__init__).parameters
        assert set(parameters) - {"self"} == roles
        assert all(
            parameter.kind is not inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )


def test_ports_are_role_sized_and_have_typed_top_level_returns():
    from email_mcp.application import ports

    role_ports = (
        ports.SourceProvider,
        ports.DeliveryGateway,
        ports.ScheduleStore,
        ports.IdentityResolver,
        ports.DeferredDelivery,
        ports.TriageGateway,
        ports.RefreshGateway,
        ports.ErrorClassifier,
        ports.OperationsGateway,
        ports.Clock,
        ports.DispatchQueue,
        ports.LocalDelivery,
        ports.UserNotifier,
    )
    for protocol in role_ports:
        methods = _public_methods(protocol)
        assert len(methods) <= 7, f"{protocol.__name__} is a fat port"
        for method in methods:
            hints = get_type_hints(getattr(protocol, method))
            assert hints.get("return") not in (dict, Any), (
                f"{protocol.__name__}.{method} has an opaque return contract"
            )

    send = inspect.signature(ports.DeliveryGateway.send)
    assert list(send.parameters) == ["self", "request"]
    assert get_type_hints(ports.DeliveryGateway.send)["request"] is SendRequest


def test_production_adapter_classes_cover_their_port_contracts():
    from email_mcp.adapters.background import (
        DefaultLocalDelivery,
        MacOSNotifier,
        SpoolDispatchQueue,
        SystemClock,
    )
    from email_mcp.adapters.delivery import DefaultDeliveryGateway
    from email_mcp.adapters.events import AuditEventPublisher
    from email_mcp.adapters.operations import DefaultOperationsGateway
    from email_mcp.adapters.refresh import AppleMailRefreshGateway
    from email_mcp.adapters.scheduling import (
        DefaultIdentityResolver,
        FileScheduleStore,
        GraphDeferredDelivery,
    )
    from email_mcp.adapters.source import LazySourceProvider
    from email_mcp.adapters.triage import AppleMailTriageGateway
    from email_mcp.application import ports
    from email_mcp.domain.events import EventPublisher
    from email_mcp.domain.mail import EmailSource
    from email_mcp.sources.apple_mail import AppleMailSource

    pairs = (
        (DefaultDeliveryGateway, ports.DeliveryGateway),
        (AuditEventPublisher, EventPublisher),
        (DefaultOperationsGateway, ports.OperationsGateway),
        (DefaultOperationsGateway, ports.ErrorClassifier),
        (AppleMailRefreshGateway, ports.RefreshGateway),
        (FileScheduleStore, ports.ScheduleStore),
        (DefaultIdentityResolver, ports.IdentityResolver),
        (GraphDeferredDelivery, ports.DeferredDelivery),
        (LazySourceProvider, ports.SourceProvider),
        (AppleMailTriageGateway, ports.TriageGateway),
        (SystemClock, ports.Clock),
        (SpoolDispatchQueue, ports.DispatchQueue),
        (DefaultLocalDelivery, ports.LocalDelivery),
        (MacOSNotifier, ports.UserNotifier),
        (AppleMailSource, EmailSource),
    )
    for implementation, protocol in pairs:
        required = _public_methods(protocol)
        missing = {
            name for name in required
            if not callable(getattr(implementation, name, None))
        }
        assert not missing, f"{implementation.__name__} misses {sorted(missing)}"
        for name in required:
            expected = inspect.signature(getattr(protocol, name))
            actual = inspect.signature(getattr(implementation, name))
            assert [p.kind for p in actual.parameters.values()] == [
                p.kind for p in expected.parameters.values()
            ], (
                f"{implementation.__name__}.{name}{actual} is incompatible "
                f"with {protocol.__name__}.{name}{expected}"
            )


def test_compatibility_facades_do_not_regrow_workflows():
    limits = {
        "dispatcher.py": 180,
        "sender.py": 550,
        "graph.py": 675,
        "triage.py": 850,
    }
    for name, maximum in limits.items():
        lines = (PACKAGE / name).read_text(encoding="utf-8").splitlines()
        assert len(lines) <= maximum, f"{name} regrew to {len(lines)} lines"


class _Source:
    def __init__(self):
        self.last_query = None

    def search(self, query):
        self.last_query = query
        return [EmailRef(
            id="42", subject="Architecture", from_addr="a@example.test",
            to=["b@example.test"], cc=[],
            date=datetime(2026, 8, 24, tzinfo=timezone.utc),
            mailbox="Inbox", account="test", snippet="ports and adapters",
            unread=True, has_attachment=False, thread_id="thread-1",
        )]

    def fts_status(self):
        return {"state": "ready", "hits": 0, "hits_capped": False}


class _Provider:
    def __init__(self, source):
        self.source = source

    def get(self):
        return self.source


class _Delivery:
    def send(self, request):
        return SendResult(
            ok=True, message_id="message-1", to=[request.to],
            subject=request.subject,
        )


class _Classifier:
    @staticmethod
    def classify(error):
        return type(error).__name__.lower()


class _Events:
    def __init__(self):
        self.published: list[DomainEvent] = []

    def publish(self, event):
        self.published.append(event)


def test_read_and_delivery_capabilities_run_with_in_memory_ports_only():
    source, events = _Source(), _Events()
    provider = _Provider(source)
    reads = ReadUseCases(
        source=provider, refresh=object(), classifier=_Classifier(),
    )
    delivery = DeliveryUseCases(
        source=provider, delivery=_Delivery(), events=events,
    )

    page = reads.search_emails(query="architecture", limit=10)
    sent = delivery.send_email(
        to="b@example.test", subject="Boundary", body="Stable",
    )

    assert [hit.id for hit in page.results] == ["42"]
    assert source.last_query.query == "architecture"
    assert sent.message_id == "message-1"
    assert [(event.name, event.outcome) for event in events.published] == [
        ("send", "sent"),
    ]


class _Clock:
    value = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value

    @staticmethod
    def format(value):
        return value.isoformat()


class _Queue:
    def __init__(self):
        entry = ScheduledEntry(
            id="scheduled-1", send_at="2026-08-24T11:59:00+00:00",
            created_at="2026-08-24T11:00:00+00:00",
            to=["b@example.test"], cc=[], bcc=[], subject="Contract",
            attachments=[], message_id="<scheduled-1@example.test>",
        )
        self.records = {"pending": {entry.id: entry}}

    def entries(self, state):
        return list(self.records.get(state, {}).values())

    def load(self, state, operation_id):
        return self.records.get(state, {}).get(operation_id)

    def claim(self, operation_id):
        entry = self.records["pending"].pop(operation_id, None)
        if entry is None:
            return False
        self.records.setdefault("sending", {})[operation_id] = entry
        return True

    def move(self, entry, source, target):
        self.records.setdefault(source, {}).pop(entry.id, None)
        entry.status = target
        self.records.setdefault(target, {})[entry.id] = entry

    def update(self, state, entry):
        self.records.setdefault(state, {})[entry.id] = entry

    @staticmethod
    def read_message(state, operation_id):
        assert (state, operation_id) == ("sending", "scheduled-1")
        return b"frozen-message"

    @staticmethod
    def integrity():
        return QueueIntegrity(True, {}, {}, {}, [])


class _Identities:
    @staticmethod
    def resolve(name):
        return name


class _LocalDelivery:
    def __init__(self):
        self.delivered = []

    @staticmethod
    def preflight(identity):
        return True, None

    def deliver(self, identity, raw, recipients):
        self.delivered.append((raw, recipients))


class _Deferred:
    def __getattr__(self, name):
        raise AssertionError(f"local entry must not use deferred role: {name}")


class _Notifier:
    @staticmethod
    def notify(title, text):
        raise AssertionError("successful delivery must not notify")


def test_background_state_machine_runs_with_role_fakes_only():
    queue, delivery, events = _Queue(), _LocalDelivery(), _Events()
    worker = BackgroundUseCases(
        queue=queue, clock=_Clock(), identities=_Identities(),
        delivery=delivery, deferred=_Deferred(), notifier=_Notifier(),
        events=events, max_retries=5,
    )

    summary = worker.dispatch_scheduled()

    assert summary.to_wire() == {
        "checked_at": "2026-08-24T12:00:00+00:00",
        "due": 1,
        "results": {"scheduled-1": "sent"},
    }
    assert delivery.delivered == [
        (b"frozen-message", ["b@example.test"]),
    ]
    assert list(queue.records["sent"]) == ["scheduled-1"]
    assert [(event.name, event.outcome) for event in events.published] == [
        ("deliver", "sent"),
    ]


def test_legacy_source_contract_is_a_compatibility_export():
    from email_mcp.sources.base import EmailRef as LegacyEmailRef

    assert LegacyEmailRef is EmailRef
