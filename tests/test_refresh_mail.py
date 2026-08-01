"""Tests for refresh_mail() — mock the AppleScript subprocess so the suite
runs on any host, then verify the tool reports the right deltas."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from email_mcp import server as srv


@pytest.fixture
def apple_source(mail_fixture, monkeypatch):
    """Wire the lazy singleton to an AppleMailSource on the test fixture."""
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    monkeypatch.setattr(srv, "_SOURCE", src, raising=False)
    yield src
    monkeypatch.setattr(srv, "_SOURCE", None, raising=False)


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["osascript", "-e", "…"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_refresh_mail_success_reports_no_delta_when_writer_idle(apple_source):
    """Successful AppleScript call but Mail.app didn't fetch anything new —
    new_messages should be 0 (not None) and before/after snapshots present."""
    with patch.object(srv.subprocess, "run", return_value=_fake_completed(0)):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=5)

    assert result["ok"] is True
    assert result["error"] is None
    assert result["error_code"] is None
    assert result["before"]["total"] == 4
    assert result["after"]["total"] == 4
    assert result["new_messages"] == 0
    assert isinstance(result["applescript_duration_ms"], int)


def test_refresh_mail_success_reports_delta_when_writer_adds_rows(apple_source, mail_fixture):
    """If Mail.app commits new rows between the AppleScript call and the
    after-snapshot, new_messages must reflect the delta."""
    import sqlite3

    def fake_run(*args, **kwargs):
        # Mid-call: another connection inserts a new message, just like
        # Mail.app would after fetching from IMAP.
        db = mail_fixture / "MailData" / "Envelope Index"
        writer = sqlite3.connect(db)
        writer.execute(
            "INSERT INTO subjects(ROWID, subject) VALUES (?,?)",
            (777, "Inbound from IMAP"),
        )
        writer.execute(
            "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, "
            "date_received, mailbox, read, flagged, deleted, conversation_id, "
            "global_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (1234, 777, 1, 1, 1900000000, 1900000100, 1, 0, 0, 0, 9001, 7777),
        )
        writer.commit()
        writer.close()
        return _fake_completed(0)

    with patch.object(srv.subprocess, "run", side_effect=fake_run):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=5)

    assert result["ok"] is True
    assert result["new_messages"] == 1
    assert result["after"]["newest_subject"] == "Inbound from IMAP"


def test_refresh_mail_maps_permission_denied(apple_source):
    stderr = (
        "execution error: Not authorized to send Apple events to Mail. "
        "(-1743)"
    )
    with patch.object(srv.subprocess, "run", return_value=_fake_completed(1, stderr=stderr)):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=5)

    assert result["ok"] is False
    assert result["error_code"] == -1743
    assert "Privacy & Security" in result["error"]
    # Snapshots still populated — caller can still see the current state.
    assert result["before"]["total"] == 4
    assert result["after"]["total"] == 4
    assert result["new_messages"] == 0


def test_refresh_mail_maps_mail_app_missing(apple_source):
    stderr = (
        "execution error: Can't get application \"Mail\". "
        "(-1728)"
    )
    with patch.object(srv.subprocess, "run", return_value=_fake_completed(1, stderr=stderr)):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=5)

    assert result["ok"] is False
    assert result["error_code"] == -1728
    assert "not installed" in result["error"]


def test_refresh_mail_handles_osascript_missing(apple_source):
    with patch.object(srv.subprocess, "run", side_effect=FileNotFoundError("osascript")):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=5)

    assert result["ok"] is False
    assert "macOS" in result["error"]
    assert result["error_code"] is None


def test_refresh_mail_handles_timeout(apple_source):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))

    with patch.object(srv.subprocess, "run", side_effect=boom):
        result = srv.tool_refresh_mail(wait_seconds=0, timeout_seconds=1)

    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_refresh_mail_clamps_wait_and_timeout(apple_source):
    """Out-of-range inputs should clamp, not pin the server forever."""
    captured = {}
    sleeps: list[float] = []

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _fake_completed(0)

    with patch.object(srv.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        with patch.object(srv.subprocess, "run", side_effect=fake_run):
            result = srv.tool_refresh_mail(wait_seconds=9999, timeout_seconds=9999)

    # Timeout clamp: 120s max.
    assert captured["timeout"] == 120.0
    # Wait clamp: 60s max — and the tool actually slept the clamped amount.
    assert result["waited_seconds"] == 60.0
    assert sleeps == [60.0]


def test_refresh_mail_skips_wait_on_failure(apple_source):
    """If AppleScript fails, we must not sleep — the user is already in a
    debugging loop and wait_seconds adds latency for nothing."""
    import time as time_mod

    sleeps: list[float] = []
    real_sleep = time_mod.sleep
    with patch.object(srv.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        with patch.object(srv.subprocess, "run", return_value=_fake_completed(1, stderr="(-1743)")):
            srv.tool_refresh_mail(wait_seconds=5, timeout_seconds=5)

    # No sleep should have happened on the failure path.
    assert sleeps == []
    _ = real_sleep  # keep import used for clarity
