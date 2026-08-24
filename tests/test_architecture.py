"""Executable dependency rules for the hexagonal application boundary."""
from __future__ import annotations

import ast
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
from email_mcp.domain.models import SendResult

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
    for path in (PACKAGE / "application").glob("*.py"):
        for level, module in _imports(path):
            if level == 0:
                assert not module.startswith("email_mcp"), (
                    f"{path.name} imports an outer package: {module}"
                )
            elif level == 1:
                assert module.split(".", 1)[0] in {
                    "models", "ports", "service",
                }, f"{path.name} imports outside application: {module}"
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


def test_legacy_source_contract_is_a_compatibility_export():
    from email_mcp.sources.base import EmailRef as LegacyEmailRef

    assert LegacyEmailRef is EmailRef
