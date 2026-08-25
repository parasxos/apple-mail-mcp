"""Behavioral contracts for replaceable production adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from email_mcp import config, doctor, graph, identities, sender, spool, triage
from email_mcp.adapters import background as background_adapter
from email_mcp.adapters.background import (
    DefaultLocalDelivery,
    MacOSNotifier,
    SpoolDispatchQueue,
)
from email_mcp.adapters.delivery import DefaultDeliveryGateway
from email_mcp.adapters.operations import DefaultOperationsGateway
from email_mcp.adapters.scheduling import (
    DefaultIdentityResolver,
    FileScheduleStore,
    GraphDeferredDelivery,
)
from email_mcp.adapters.source import FixedSourceProvider, LazySourceProvider
from email_mcp.adapters.triage import AppleMailTriageGateway
from email_mcp.application.models import AuditQuery, TriageApplyResult
from email_mcp.application.ports import (
    BackgroundDeliveryError,
    BackgroundIdentityError,
    BackgroundProviderError,
)
from email_mcp.domain.models import (
    ScheduledEntry,
    SendRequest,
    SendResult,
)


def _entry(operation_id: str = "contract-1") -> ScheduledEntry:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat()
    return ScheduledEntry(
        id=operation_id,
        send_at=now,
        created_at=now,
        to=["person@example.test"],
        cc=[],
        bcc=[],
        subject="Adapter contract",
        attachments=[],
        message_id=f"<{operation_id}@example.test>",
    )


def test_fixed_source_provider_preserves_the_supplied_adapter():
    source = object()
    assert FixedSourceProvider(source).get() is source


def test_lazy_source_provider_builds_exactly_once_under_concurrency():
    source = object()
    calls: list[int] = []

    def factory():
        calls.append(1)
        return source

    provider = LazySourceProvider(factory)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: provider.get(), range(48)))

    assert calls == [1]
    assert all(result is source for result in results)


def test_delivery_adapter_maps_typed_command_without_losing_fields(monkeypatch):
    seen = {}

    def send_email(**values):
        seen.update(values)
        return SendResult(
            ok=True, message_id="message-1", to=[values["to"]],
            subject=values["subject"],
        )

    monkeypatch.setattr(sender, "send_email", send_email)
    request = SendRequest(
        to="person@example.test",
        subject="Typed",
        body="Boundary",
        cc="copy@example.test",
        bcc="blind@example.test",
        attachments=("one.pdf", "two.txt"),
        from_identity="work",
    )

    result = DefaultDeliveryGateway().send(request)

    assert result.message_id == "message-1"
    assert seen == {
        "to": "person@example.test",
        "subject": "Typed",
        "body": "Boundary",
        "cc": "copy@example.test",
        "bcc": "blind@example.test",
        "attachments": ["one.pdf", "two.txt"],
        "from_identity": "work",
    }


def test_identity_adapter_normalizes_domain_error_and_code(monkeypatch):
    def fail(_name):
        raise identities.IdentityError("identity disappeared", code="missing")

    monkeypatch.setattr(identities, "get", fail)
    with pytest.raises(BackgroundIdentityError, match="disappeared") as caught:
        DefaultIdentityResolver().resolve("missing")
    assert caught.value.code == "missing"


@pytest.mark.parametrize(
    ("method", "provider", "arguments"),
    [
        ("find_draft", "find_draft_by_message_id",
         (object(), "<message@example.test>")),
        ("was_sent", "sent_by_message_id",
         (object(), "<message@example.test>")),
        ("status", "draft_status",
         (object(), "draft-1", "<message@example.test>")),
        ("delete_draft", "delete_draft", (object(), "draft-1")),
    ],
)
def test_deferred_adapter_normalizes_provider_errors(
    monkeypatch, method, provider, arguments,
):
    def fail(*_args):
        raise graph.GraphError("provider unavailable", code="provider_down")

    monkeypatch.setattr(graph, provider, fail)
    with pytest.raises(BackgroundProviderError, match="unavailable") as caught:
        getattr(GraphDeferredDelivery(), method)(*arguments)
    assert caught.value.code == "provider_down"


def test_local_delivery_adapter_normalizes_transport_errors(monkeypatch):
    def fail(*_args, **_kwargs):
        raise sender.SendError("transport unavailable", code="transport_down")

    monkeypatch.setattr(sender, "deliver_for", fail)
    with pytest.raises(BackgroundDeliveryError, match="unavailable") as caught:
        DefaultLocalDelivery().deliver(
            object(), b"message", ["person@example.test"],
        )
    assert caught.value.code == "transport_down"


def test_notification_content_is_passed_as_data_not_applescript(monkeypatch):
    calls = []
    monkeypatch.setattr(
        background_adapter.subprocess, "run",
        lambda arguments, **options: calls.append((arguments, options)),
    )
    hostile = '" & do shell script "touch /tmp/never" & "'

    MacOSNotifier().notify(hostile, hostile)

    arguments, options = calls[0]
    assert arguments[-2:] == [hostile, hostile]
    assert hostile not in arguments[2]
    assert arguments[3] == "--"
    assert options == {"capture_output": True, "timeout": 10}


def test_spool_queue_contract_preserves_message_across_state_changes(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    entry = _entry()
    spool.save(b"frozen-message", entry)
    queue = SpoolDispatchQueue()

    assert [item.id for item in queue.entries("pending")] == [entry.id]
    assert queue.claim(entry.id) is True
    assert queue.read_message("sending", entry.id) == b"frozen-message"
    claimed = queue.load("sending", entry.id)
    assert claimed is not None
    queue.move(claimed, "sending", "sent")

    assert queue.load("pending", entry.id) is None
    assert queue.load("sent", entry.id).id == entry.id
    assert queue.integrity().ok is True


def test_schedule_listing_translates_storage_to_stable_wire_shape(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "dispatcher_plist", lambda: tmp_path / "agent")
    spool.save(b"message", _entry())

    listing = FileScheduleStore().listing(None, 50)
    wire = listing.to_wire()

    assert listing.integrity.ok is True
    assert wire["dispatcher_installed"] is False
    assert wire["dispatcher_label"] == config.LAUNCHD_LABEL
    assert [entry["id"] for entry in wire["pending"]] == ["contract-1"]
    assert set(spool.STATES) <= wire.keys()
    assert "integrity" not in wire


def test_operations_adapter_returns_typed_reports_and_audit(monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda: {
        "ok": True, "read_only": True, "checks": {}, "audit": {},
    })
    monkeypatch.setattr(doctor, "check_transports", lambda: {
        "ok": True, "detail": "ready", "identities": {},
        "default": "work",
    })
    from email_mcp import audit
    monkeypatch.setattr(audit, "query", lambda **_values: {
        "events": [], "files_scanned": 1, "skipped_lines": 0,
    })
    adapter = DefaultOperationsGateway()

    assert adapter.doctor().to_wire()["read_only"] is True
    assert adapter.transport_check().to_wire()["default"] == "work"
    assert adapter.audit(AuditQuery(limit=3)).files_scanned == 1


def test_triage_adapter_converts_legacy_dictionary_at_the_edge(monkeypatch):
    monkeypatch.setattr(triage, "apply_plan", lambda source, plan_id: {
        "ok": True,
        "plan_id": plan_id,
        "status": "applied",
        "planned": 1,
        "acted": 1,
        "failures": [],
        "verified": 1,
        "pending": [],
        "osascript_ms": 4,
        "verify_polls": 1,
        "duration_ms": 9,
    })

    result = AppleMailTriageGateway().apply(object(), "plan-1")

    assert isinstance(result, TriageApplyResult)
    assert result.to_wire() == {
        "ok": True,
        "plan_id": "plan-1",
        "status": "applied",
        "planned": 1,
        "acted": 1,
        "failures": [],
        "verified": 1,
        "pending": [],
        "osascript_ms": 4,
        "verify_polls": 1,
        "duration_ms": 9,
        "note": None,
    }
