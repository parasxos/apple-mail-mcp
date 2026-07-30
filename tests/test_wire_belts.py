"""v0.10 wire-safety belts (docs/v1-contract.md §3.5/§7): previously-
crashing paths return coded envelopes; success shapes stay byte-untouched;
the FULL traceback goes to the file log, never the wire."""
from __future__ import annotations

import logging

from email_mcp import server


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def test_bad_iso_dates_return_invalid_input():
    # The _parse_dt ValueError leak (search / plan / plan_delete in Q5).
    out = server.tool_search_emails(before="not-a-date")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["fix"] == "run doctor"
    assert "invalid ISO" in out["error"]

    out2 = server.tool_triage_plan(after="nope",
                                   actions=[{"action": "mark_read"}])
    assert out2["code"] == "invalid_input"
    out3 = server.tool_triage_plan_delete(before="garbage")
    assert out3["code"] == "invalid_input"

    # The audit tool rejects bad bounds with the same code — but as its
    # own designed validation, not a belt catch.
    out4 = server.tool_audit(since="last tuesday")
    assert out4["ok"] is False and out4["code"] == "invalid_input"
    assert "since" in out4["error"]


def test_unknown_or_bad_ids_return_not_found_and_invalid_input(
    mail_fixture, monkeypatch
):
    from email_mcp.sources.apple_mail import AppleMailSource

    monkeypatch.setattr(server, "_SOURCE",
                        AppleMailSource(mail_base=mail_fixture))
    out = server.tool_get_email("424242")  # LookupError leak in Q5
    assert out["ok"] is False and out["code"] == "not_found"
    assert out["fix"] == "run doctor"

    assert server.tool_get_attachment("424242", "1.2")["code"] == "not_found"
    assert server.tool_get_email("not-a-rowid")["code"] == "invalid_input"

    # Success shapes byte-untouched: the bare v0.7-compat dict gains no
    # envelope keys from the belt.
    ok = server.tool_get_email("100")
    assert "code" not in ok and "fix" not in ok
    assert ok["ref"]["id"] == "100"


def test_missing_mail_store_returns_mail_unavailable(monkeypatch):
    class GoneSource:
        def search(self, q):
            raise FileNotFoundError(
                "~/Library/Mail does not exist — grant Full Disk Access")

    monkeypatch.setattr(server, "_SOURCE", GoneSource())
    out = server.tool_search_emails(query="x")
    assert out["ok"] is False and out["code"] == "mail_unavailable"
    assert out["fix"] == "run doctor"
    assert "Full Disk Access" in out["error"]


def test_unexpected_exception_returns_internal_error_and_logs_traceback(
    monkeypatch,
):
    class Boom:
        def search(self, q):
            raise RuntimeError("kaput")

    monkeypatch.setattr(server, "_SOURCE", Boom())
    recorder = _Recorder()
    logger = logging.getLogger("email_mcp")  # propagate=False: hook directly
    logger.addHandler(recorder)
    try:
        out = server.tool_search_emails(query="x")
    finally:
        logger.removeHandler(recorder)

    assert out["ok"] is False and out["code"] == "internal_error"
    assert out["fix"] == "run doctor"
    assert "RuntimeError: kaput" in out["error"]
    # FULL traceback in the file log, never on the wire.
    assert any(r.exc_info and r.exc_info[0] is RuntimeError
               for r in recorder.records)
    assert "Traceback" not in str(out)


def test_triage_apply_belt_carries_plan_id_operation_id(monkeypatch):
    monkeypatch.setattr(server, "_SOURCE", object())

    def _boom(src, plan_id):
        raise RuntimeError("exploded mid-apply")

    monkeypatch.setattr(server, "apply_plan", _boom)
    out = server.tool_triage_apply(plan_id="P-123")
    assert out["ok"] is False and out["code"] == "internal_error"
    assert out["operation_id"] == "P-123"  # threads to the ledger's op

    out2 = server.tool_triage_apply("P-9")  # positional binding too
    assert out2["operation_id"] == "P-9"
