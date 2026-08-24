"""Behavioral contracts shared by replaceable production adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from email_mcp import graph, identities, sender
from email_mcp.adapters import background as background_adapter
from email_mcp.adapters.background import DefaultBackgroundGateway
from email_mcp.adapters.source import FixedSourceProvider, LazySourceProvider
from email_mcp.application.ports import (
    BackgroundDeliveryError,
    BackgroundIdentityError,
    BackgroundProviderError,
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


def test_background_adapter_normalizes_identity_errors(monkeypatch):
    def fail(_name):
        raise identities.IdentityError("identity disappeared")

    monkeypatch.setattr(identities, "get", fail)
    with pytest.raises(BackgroundIdentityError, match="disappeared"):
        DefaultBackgroundGateway().identity("missing")


@pytest.mark.parametrize(
    ("method", "provider"),
    [
        ("find_deferred_draft", "find_draft_by_message_id"),
        ("deferred_was_sent", "sent_by_message_id"),
        ("deferred_status", "draft_status"),
        ("delete_deferred_draft", "delete_draft"),
    ],
)
def test_background_adapter_normalizes_provider_errors(
    monkeypatch, method, provider,
):
    def fail(*_args):
        raise graph.GraphError("provider unavailable")

    monkeypatch.setattr(graph, provider, fail)
    arguments = {
        "find_deferred_draft": (object(), "<message@example.test>"),
        "deferred_was_sent": (object(), "<message@example.test>"),
        "deferred_status": (object(), "draft-1", "<message@example.test>"),
        "delete_deferred_draft": (object(), "draft-1"),
    }
    with pytest.raises(BackgroundProviderError, match="unavailable"):
        getattr(DefaultBackgroundGateway(), method)(*arguments[method])


def test_background_adapter_normalizes_delivery_errors(monkeypatch):
    def fail(*_args, **_kwargs):
        raise sender.SendError("transport unavailable")

    monkeypatch.setattr(sender, "deliver_for", fail)
    with pytest.raises(BackgroundDeliveryError, match="unavailable"):
        DefaultBackgroundGateway().deliver(
            object(), b"message", ["person@example.test"],
        )


def test_notification_content_is_passed_as_data_not_applescript(monkeypatch):
    calls = []
    monkeypatch.setattr(
        background_adapter.subprocess, "run",
        lambda arguments, **options: calls.append((arguments, options)),
    )
    hostile = '" & do shell script "touch /tmp/never" & "'

    background_adapter._macos_notification(hostile, hostile)

    arguments, options = calls[0]
    assert arguments[-2:] == [hostile, hostile]
    assert hostile not in arguments[2]
    assert arguments[3] == "--"
    assert options == {"capture_output": True, "timeout": 10}
