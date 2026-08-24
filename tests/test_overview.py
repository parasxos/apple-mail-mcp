"""User-facing status overview: concise state plus concrete recovery."""
from __future__ import annotations

from email_mcp import checks, overview


def _doctor_report(*, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "read_only": False,
        "checks": {
            "mail_store": {"ok": True, "detail": "42 messages available"},
            "automation": {"ok": True, "detail": "Mail.app reachable"},
            "accessibility": {
                "ok": False, "advisory": True,
                "detail": "optional mailbox deletion fallback unavailable",
                "fix": "grant Accessibility if you need mailbox deletion",
            },
            "identities": {"ok": True, "detail": "one identity: main"},
            "transports": {"ok": True, "detail": "1/1 healthy"},
            "dispatcher": {"ok": True, "detail": "installed; 0 pending"},
            "spool_plans": {
                "ok": True,
                "detail": "pending 0, sending 0, sent 2, failed 0, cancelled 0",
                "counts": {"pending": 0, "sending": 0, "sent": 2,
                           "failed": 0, "cancelled": 0},
            },
            "fts": {"ok": True, "detail": "ready: 42 indexed"},
            "graph": {"ok": True, "detail": "no Graph identities"},
        },
        "audit": {"ok": True, "detail": "3 events recorded"},
    }


def _scheduling(*, pending=None, failed=None, ok: bool = True) -> dict:
    pending = pending or []
    failed = failed or []
    return {
        "ok": ok,
        "launchd_installed": True,
        "counts": {"pending": len(pending), "sending": 0, "sent": 2,
                   "failed": len(failed), "cancelled": 0},
        "pending": pending,
        "sending": [],
        "failed": failed,
    }


def test_ready_overview_is_grouped_and_names_the_queue():
    snap = overview.build(_doctor_report(), [], _scheduling())
    text = "\n".join(overview.render(snap))

    assert snap["ready"] is True
    assert "Status: READY" in text
    assert "Mail access" in text
    assert "Scheduled mail" in text
    assert "Queue: 0 pending, 0 sending, 0 failed" in text
    assert "No action needed" in text


def test_failed_schedule_is_visible_and_has_a_recovery_path():
    failed = [{
        "id": "20260824T120000Z-deadbeef",
        "send_at": "2026-08-24T12:00:00+00:00",
        "to": ["person@example.org"],
        "subject": "Quarterly update",
        "attempts": 5,
        "next_attempt_at": None,
        "last_error": "SMTP authentication failed",
        "executor": "launchd",
    }]
    snap = overview.build(_doctor_report(), [], _scheduling(failed=failed))
    text = "\n".join(overview.render(snap))

    assert snap["ready"] is False
    assert "Status: NEEDS ATTENTION" in text
    assert "Quarterly update" in text
    assert "SMTP authentication failed" in text
    assert "20260824T120000Z-deadbeef" in text
    assert "email-mcp dispatcher --status" in text
    assert "reschedule" in text.lower()


def test_repairable_install_findings_lead_with_doctor_fix():
    finding = checks.Finding(checks.TREE_MODES, "two private folders are open")
    snap = overview.build(_doctor_report(), [finding], _scheduling())

    assert snap["ready"] is False
    assert snap["next_steps"][0] == "email-mcp doctor --fix"
    assert snap["findings"] == [{
        "check": checks.TREE_MODES,
        "detail": "two private folders are open",
        "repairable": True,
    }]


def test_next_scheduled_message_is_shown_without_dumping_the_whole_queue():
    pending = [
        {"id": "later", "send_at": "2026-08-25T12:00:00+00:00",
         "to": ["b@example.org"], "subject": "Later", "attempts": 0,
         "next_attempt_at": None, "last_error": None, "executor": "graph"},
        {"id": "first", "send_at": "2026-08-24T12:00:00+00:00",
         "to": ["a@example.org"], "subject": "First", "attempts": 0,
         "next_attempt_at": None, "last_error": None,
         "executor": "launchd"},
    ]
    snap = overview.build(_doctor_report(), [], _scheduling(pending=pending))
    text = "\n".join(overview.render(snap))

    assert snap["next_scheduled"]["id"] == "first"
    assert "Next: 2026-08-24T12:00:00+00:00 — First" in text
    assert "Later" not in text
