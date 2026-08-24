"""Executable dependency rules for the hexagonal application boundary."""
from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from email_mcp.application.service import (
    ApplicationDependencies,
    EmailApplication,
)
from email_mcp.domain.events import DomainEvent
from email_mcp.domain.mail import EmailRef
from email_mcp.domain.models import ScheduledEntry, SendResult

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "email_mcp"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


def test_domain_never_reaches_outward():
    """The center cannot know about application, adapters, or protocols."""
    for path in (PACKAGE / "domain").glob("*.py"):
        for level, module in _imports(path):
            assert level < 2, f"{path.name} reaches outside domain: {module}"
            assert not module.startswith("email_mcp"), (
                f"{path.name} has an absolute package dependency: {module}"
            )


def test_application_imports_only_domain_and_its_own_ports():
    """Use cases must remain runnable without MCP, macOS, storage or network."""
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


def test_outbound_adapters_never_depend_on_inbound_adapters():
    """Concrete edges may point inward, never sideways into a front end."""
    forbidden = {"bootstrap", "cli", "mcp_api", "server"}
    for path in (PACKAGE / "adapters").glob("*.py"):
        for level, module in _imports(path):
            root = module.split(".", 1)[0]
            if level >= 2:
                assert root not in forbidden, (
                    f"{path.name} depends on inbound/composition code: {module}"
                )
            if level == 0 and module.startswith("email_mcp."):
                root = module.split(".", 2)[1]
                assert root not in forbidden, (
                    f"{path.name} depends on inbound/composition code: {module}"
                )


def test_mcp_api_is_a_thin_inbound_adapter():
    allowed = {"application", "bootstrap", "domain", "envelope"}
    path = PACKAGE / "mcp_api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            roots = (
                [node.module.split(".", 1)[0]] if node.module
                else [alias.name.split(".", 1)[0] for alias in node.names]
            )
            if node.level == 1:
                assert set(roots) <= allowed, (
                    f"mcp_api imports a concrete integration: {roots}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("email_mcp."), (
                    f"mcp_api imports a concrete integration: {alias.name}"
                )


def test_compatibility_facades_do_not_regrow_workflows():
    """Keep legacy import surfaces as facades, not renewed monoliths."""
    limits = {
        "dispatcher.py": 300,
        "sender.py": 550,
        "graph.py": 675,
        "triage.py": 850,
    }
    for name, maximum in limits.items():
        lines = (PACKAGE / name).read_text(encoding="utf-8").splitlines()
        assert len(lines) <= maximum, f"{name} regrew to {len(lines)} lines"


def test_production_adapter_classes_cover_their_port_contracts():
    from email_mcp.adapters.background import DefaultBackgroundGateway
    from email_mcp.adapters.delivery import DefaultDeliveryGateway
    from email_mcp.adapters.events import AuditEventPublisher
    from email_mcp.adapters.operations import DefaultOperationsGateway
    from email_mcp.adapters.refresh import AppleMailRefreshGateway
    from email_mcp.adapters.scheduling import (
        FileScheduleStore,
        GraphDeferredScheduler,
    )
    from email_mcp.adapters.source import LazySourceProvider
    from email_mcp.adapters.triage import AppleMailTriageGateway
    from email_mcp.application import ports
    from email_mcp.domain.mail import EmailSource
    from email_mcp.sources.apple_mail import AppleMailSource

    pairs = (
        (DefaultBackgroundGateway, ports.BackgroundGateway),
        (DefaultDeliveryGateway, ports.DeliveryGateway),
        (AuditEventPublisher, ports.EventPublisher),
        (DefaultOperationsGateway, ports.OperationsGateway),
        (AppleMailRefreshGateway, ports.RefreshGateway),
        (FileScheduleStore, ports.ScheduleStore),
        (GraphDeferredScheduler, ports.DeferredScheduler),
        (LazySourceProvider, ports.SourceProvider),
        (AppleMailTriageGateway, ports.TriageGateway),
        (AppleMailSource, EmailSource),
    )
    for implementation, protocol in pairs:
        required = {
            name for name, member in inspect.getmembers(protocol)
            if callable(member) and not name.startswith("_")
        }
        missing = {
            name for name in required
            if not callable(getattr(implementation, name, None))
        }
        assert not missing, f"{implementation.__name__} misses {sorted(missing)}"
        for name in required:
            expected = inspect.signature(getattr(protocol, name))
            actual = inspect.signature(getattr(implementation, name))
            expected_kinds = [
                parameter.kind for parameter in expected.parameters.values()
            ]
            actual_kinds = [
                parameter.kind for parameter in actual.parameters.values()
            ]
            assert actual_kinds == expected_kinds, (
                f"{implementation.__name__}.{name}{actual} is incompatible "
                f"with {protocol.__name__}.{name}{expected}"
            )
    assert isinstance(FileScheduleStore.states, tuple)


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
    def send(self, **values):
        return SendResult(
            ok=True, message_id="message-1", to=[values["to"]],
            subject=values["subject"],
        )


class _Operations:
    @staticmethod
    def classify(error):
        return type(error).__name__.lower()


class _Events:
    def __init__(self):
        self.published: list[DomainEvent] = []

    def publish(self, event):
        self.published.append(event)


class _Background:
    def __init__(self):
        self.clock = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        self.entry = ScheduledEntry(
            id="scheduled-1", send_at="2026-08-24T11:59:00+00:00",
            created_at="2026-08-24T11:00:00+00:00",
            to=["b@example.test"], cc=[], bcc=[], subject="Contract",
            attachments=[], message_id="<scheduled-1@example.test>",
        )
        self.records = {"pending": {self.entry.id: self.entry}}
        self.delivered: list[tuple[bytes, list[str]]] = []

    def now(self):
        return self.clock

    @staticmethod
    def iso(value):
        return value.isoformat()

    def entries(self, state):
        return list(self.records.get(state, {}).values())

    def load(self, state, operation_id):
        return self.records.get(state, {}).get(operation_id)

    def claim(self, operation_id):
        entry = self.records.get("pending", {}).pop(operation_id, None)
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
        assert state == "sending" and operation_id == "scheduled-1"
        return b"frozen-message"

    def integrity(self):
        return {"ok": True, "counts": {}}

    @staticmethod
    def max_retries():
        return 5

    @staticmethod
    def identity(name):
        return name

    @staticmethod
    def preflight(identity):
        return True, None

    def deliver(self, identity, raw, recipients):
        self.delivered.append((raw, recipients))

    def find_deferred_draft(self, identity, message_id):
        raise AssertionError("local entry must not use Exchange")

    def deferred_was_sent(self, identity, message_id):
        raise AssertionError("local entry must not use Exchange")

    def deferred_status(self, identity, draft_id, message_id):
        raise AssertionError("local entry must not use Exchange")

    def delete_deferred_draft(self, identity, draft_id):
        raise AssertionError("local entry must not use Exchange")

    @staticmethod
    def notify(title, text):
        raise AssertionError("successful delivery must not notify")


def test_use_cases_run_with_in_memory_ports_only():
    source, events = _Source(), _Events()
    application = EmailApplication(ApplicationDependencies(
        source=_Provider(source),
        delivery=_Delivery(),
        schedules=object(),
        deferred=object(),
        triage=object(),
        refresh=object(),
        operations=_Operations(),
        events=events,
    ))

    page = application.search_emails(query="architecture", limit=10)
    sent = application.send_email(
        to="b@example.test", subject="Boundary", body="Stable",
    )

    assert [hit.id for hit in page.results] == ["42"]
    assert source.last_query.query == "architecture"
    assert sent.message_id == "message-1"
    assert [(event.name, event.outcome) for event in events.published] == [
        ("send", "sent"),
    ]


def test_background_worker_runs_with_in_memory_ports_only():
    background, events = _Background(), _Events()
    application = EmailApplication(ApplicationDependencies(
        source=object(), delivery=object(), schedules=object(),
        deferred=object(), triage=object(), refresh=object(),
        operations=object(), events=events, background=background,
    ))

    summary = application.dispatch_scheduled()

    assert summary == {
        "checked_at": "2026-08-24T12:00:00+00:00",
        "due": 1,
        "results": {"scheduled-1": "sent"},
    }
    assert background.delivered == [
        (b"frozen-message", ["b@example.test"]),
    ]
    assert list(background.records["sent"]) == ["scheduled-1"]
    assert [(event.name, event.outcome) for event in events.published] == [
        ("deliver", "sent"),
    ]


def test_legacy_source_contract_is_a_compatibility_export():
    from email_mcp.sources.base import EmailRef as LegacyEmailRef

    assert LegacyEmailRef is EmailRef
