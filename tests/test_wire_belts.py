"""Wire-safety belts (docs/v1-contract.md §3.5/§7): previously-crashing
paths return coded envelopes; the FULL traceback goes to the file log,
never the wire. v0.11 additions: the three formerly array-shaped tools
(get_thread / list_mailboxes / list_recent) are belted too — §7's carve-out
is closed and "no exception escapes" is absolute — the belt's class map is
F6-precise (PermissionError / sqlite3.OperationalError → mail_unavailable;
UnicodeDecodeError and KeyError/IndexError → internal_error, never blamed
on the caller), and the send/cancel failure envelopes carry §3.4/§3 codes.
"""
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

    # Success envelope ({ok, email} since v0.11) gains no failure keys
    # from the belt.
    ok = server.tool_get_email("100")
    assert ok["ok"] is True
    assert "code" not in ok and "fix" not in ok
    assert ok["email"]["ref"]["id"] == "100"


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


# --------------------------------------------------------------------- #
# v0.11: the three formerly array-shaped tools are belted (§7 absolute)  #
# --------------------------------------------------------------------- #


def test_list_recent_bad_limit_returns_invalid_input(
    mail_fixture, monkeypatch
):
    """The proved live leak: tool_list_recent(limit="abc") used to raise
    ValueError onto the wire — now a coded envelope (acceptance probe)."""
    from email_mcp.sources.apple_mail import AppleMailSource

    monkeypatch.setattr(server, "_SOURCE",
                        AppleMailSource(mail_base=mail_fixture))
    out = server.tool_list_recent(limit="abc")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["fix"] == "run doctor"


def test_thread_and_mailbox_tools_belt_every_leak(monkeypatch):
    class Gone:
        def thread(self, thread_id):
            raise FileNotFoundError("~/Library/Mail does not exist")

        def mailboxes(self):
            raise RuntimeError("kaput")

        def recent(self, mailbox, account, limit):
            raise LookupError("nothing here")

    monkeypatch.setattr(server, "_SOURCE", Gone())
    assert server.tool_get_thread("7001")["code"] == "mail_unavailable"
    assert server.tool_list_mailboxes()["code"] == "internal_error"
    assert server.tool_list_recent()["code"] == "not_found"


# --------------------------------------------------------------------- #
# F6: belt class map precision (§3.5)                                    #
# --------------------------------------------------------------------- #


def test_permission_and_sqlite_errors_map_to_mail_unavailable(monkeypatch):
    import sqlite3

    class Denied:
        def search(self, q):
            raise PermissionError("Operation not permitted: Envelope Index")

    class Locked:
        def search(self, q):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server, "_SOURCE", Denied())
    out = server.tool_search_emails(query="x")
    assert out["ok"] is False and out["code"] == "mail_unavailable"

    monkeypatch.setattr(server, "_SOURCE", Locked())
    out = server.tool_search_emails(query="x")
    assert out["ok"] is False and out["code"] == "mail_unavailable"


def test_unicode_and_container_errors_are_internal_never_callers_fault(
    monkeypatch,
):
    """UnicodeDecodeError (a ValueError) and KeyError/IndexError (both
    LookupErrors) come from the store or a bug — never from JSON-supplied
    arguments. The belt must not blame the caller (invalid_input) or claim
    a missing object (not_found)."""
    class Mojibake:
        def search(self, q):
            b"\xff\xfe".decode("utf-8")

    class BrokenDict:
        def search(self, q):
            return {}["missing-internal-key"]

    class BrokenList:
        def search(self, q):
            return [][5]

    for src in (Mojibake(), BrokenDict(), BrokenList()):
        monkeypatch.setattr(server, "_SOURCE", src)
        out = server.tool_search_emails(query="x")
        assert out["ok"] is False and out["code"] == "internal_error"
        assert out["fix"] == "run doctor"


# --------------------------------------------------------------------- #
# v0.11: SEND_CODES_V011 wired (§3.4) + cancel_scheduled codes (§3/§4)   #
# --------------------------------------------------------------------- #


def test_send_code_classifier_covers_the_frozen_table():
    from email_mcp.identities import IdentityError
    from email_mcp.transports import SendError

    cases = [
        ("header_injection: control character (CR/LF/NUL) in `subject`",
         "header_injection"),
        ("invalid_recipient: 'x' in `to` is not a usable address",
         "invalid_recipient"),
        ("Refusing to send as identity [work]: recipient(s) not on its "
         "allowlist — a@b.", "recipient_not_allowed"),
        ("attachment not found: /tmp/x.pdf", "attachment_not_found"),
        ("attachment is a directory: /tmp/d — zip it first",
         "attachment_unreadable"),
        ("cannot read attachment /tmp/x.pdf: EACCES",
         "attachment_unreadable"),
        ("attachments total 25.0 MB, over the 20 MB budget",
         "attachments_too_large"),
        ("invalid header content: embedded newline", "invalid_header"),
        ("`to` is required (no valid recipient address).", "invalid_input"),
        ("`subject` is required.", "invalid_input"),
        ("`body` is empty.", "invalid_input"),
        ("invalid send_at (want ISO-8601): 'tomorrow'", "invalid_send_at"),
        ("send_at is in the past (2020-01-01T00:00:00+00:00).",
         "send_at_in_past"),
        ("[work/ssh_sendmail] transport unavailable: session dead",
         "transport_unavailable"),
        ("[work/ssh_sendmail] ssh not found on PATH.",
         "transport_unavailable"),
        ("[home/pipe] command not found: /usr/sbin/sendmail",
         "transport_unavailable"),
        ("[work/ssh_sendmail] delivery pipe timed out after 60s",
         "delivery_failed"),
        ("[home/pipe] delivery failed (exit 75): no stderr",
         "delivery_failed"),
        ("[home/pipe] /usr/sbin/sendmail hung for 60s — is the local MTA "
         "configured?", "delivery_failed"),
        ("[gmail/smtp] SMTP delivery via smtp.gmail.com:587 failed: boom",
         "delivery_failed"),
        ("[gmail/smtp] SMTP auth failed for x@gmail.com at "
         "smtp.gmail.com:587", "auth_failed"),
        # smtp's UNPREFIXED secret-source errors (the §3.4 known gap)
        ("`security` CLI not found — the smtp driver needs macOS.",
         "credentials_unavailable"),
        ("`op` CLI not found — install the 1Password CLI",
         "credentials_unavailable"),
        ("Keychain read for 'item' timed out after 30s",
         "credentials_unavailable"),
        ("Keychain item 'item' not readable (security exit 44).",
         "credentials_unavailable"),
        ("1Password read for 'op://v/i/p' failed (op exit 1)",
         "credentials_unavailable"),
        # a preflight wrapper embedding credential prose stays transport
        ("[gmail/smtp] transport unavailable: Keychain read for 'item' "
         "timed out after 30s", "transport_unavailable"),
        ("[x/nope] unknown transport driver 'nope'. Available: [...]",
         "identity_misconfigured"),
        ("[x/smtp] bad transport params: unexpected keyword",
         "identity_misconfigured"),
        ("[x/smtp] needs a secret source: set `op` or `keychain`",
         "identity_misconfigured"),
        ("[x/pipe] `command` is empty.", "identity_misconfigured"),
    ]
    for prose, want in cases:
        assert server._send_code(SendError(prose)) == want, prose

    # exception TYPE wins over prose for the identities file
    assert server._send_code(
        IdentityError("unknown identity 'x'. Available: ['a']")
    ) == "unknown_identity"
    assert server._send_code(
        IdentityError("malformed TOML in identities.toml: line 3")
    ) == "identity_misconfigured"

    from email_mcp.graph import GraphError
    assert server._send_code(GraphError("429 throttled")) == \
        "transport_unavailable"


def test_send_and_schedule_failures_carry_codes(monkeypatch):
    from email_mcp.transports import SendError

    def _refuse(**kwargs):
        raise SendError(
            "Refusing to send as identity [default]: recipient(s) not on "
            "its allowlist — stranger@example.org.")

    monkeypatch.setattr(server, "send_email", _refuse)
    out = server.tool_send_email(to="stranger@example.org", subject="s",
                                 body="b")
    assert out["ok"] is False and out["code"] == "recipient_not_allowed"

    def _past(**kwargs):
        raise SendError("send_at is in the past (2020-01-01T00:00:00).")

    monkeypatch.setattr(server, "schedule_email", _past)
    out = server.tool_schedule_email(to="a@b", subject="s", body="b",
                                     send_at="2020-01-01T00:00:00")
    assert out["ok"] is False and out["code"] == "send_at_in_past"


def test_cancel_scheduled_failures_carry_codes(monkeypatch):
    from types import SimpleNamespace

    from email_mcp import spool

    monkeypatch.setattr(spool, "find", lambda id: None)
    out = server.tool_cancel_scheduled("nope-1")
    assert out["ok"] is False and out["code"] == "not_found"
    assert "operation_id" not in out  # §2: no durable artifact exists

    entry = SimpleNamespace(subject="s", executor="launchd",
                            send_at="2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(spool, "find", lambda id: ("sent", entry))
    out = server.tool_cancel_scheduled("S-1")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["operation_id"] == "S-1"  # artifact exists → threads to op

    monkeypatch.setattr(spool, "find", lambda id: ("pending", entry))
    monkeypatch.setattr(spool, "claim",
                        lambda id, src=None, dst=None: False)
    out = server.tool_cancel_scheduled("S-2")  # dispatcher won the race
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["operation_id"] == "S-2"


def test_belt_survives_an_exception_whose_message_cannot_be_rendered():
    """§7 says every tool is total — including when the belt's OWN message
    formatting is what raises.

    `f"{type(e).__name__}: {e}"` ran inside the except handler and outside
    any guard, so an exception with an exploding `__str__` (or a metaclass
    with an exploding `__name__`) replaced the envelope with a bare
    ToolError carrying no ok, no code and no fix.
    """
    class ExplodingStr(Exception):
        def __str__(self):
            raise RuntimeError("__str__ itself explodes")

    class ExplodingName(type):
        @property
        def __name__(cls):
            raise RuntimeError("type name explodes")

    class NastyType(Exception, metaclass=ExplodingName):
        pass

    for exc in (ExplodingStr(), NastyType("boom")):
        @server._belt()
        def poisoned() -> dict:
            raise exc

        out = poisoned()
        assert out["ok"] is False
        assert out["code"] == "internal_error"
        assert out["fix"] == "run doctor"
        assert isinstance(out["error"], str) and out["error"]

    # An ordinary exception must still report its real message verbatim.
    @server._belt()
    def ordinary() -> dict:
        raise ValueError("a perfectly ordinary problem")

    assert ordinary()["error"] == "a perfectly ordinary problem"
