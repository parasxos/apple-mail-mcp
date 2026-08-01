"""envelope.py — the one wire boundary (contract §2/§3/§7): typed values
gain ok:true, typed errors render {ok:false, code, error, fix?,
operation_id?}, unknown exceptions are classified by the one map with the
FULL traceback in the file log only. Byte-bounding of failure prose and
the minted-id gate live here and nowhere else."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import pytest

from email_mcp import codes, envelope
from email_mcp.envelope import (
    InvalidInput, MailUnavailable, NotFound, ToolError,
)


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@dataclass(frozen=True)
class _Receipt:
    id: str
    n: int


# --------------------------------------------------------------------- #
# the exception-classification map                                      #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("exc,code", [
    # most-specific-first: the interesting rows are subclasses of broad ones
    (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "internal_error"),
    (ValueError("nope"), "invalid_input"),
    (KeyError("k"), "internal_error"),
    (IndexError("i"), "internal_error"),
    (LookupError("missing"), "not_found"),
    (sqlite3.ProgrammingError("closed db"), "internal_error"),
    (FileNotFoundError("gone"), "mail_unavailable"),
    (PermissionError("denied"), "mail_unavailable"),
    (sqlite3.DatabaseError("corrupt"), "mail_unavailable"),
    (sqlite3.OperationalError("disk I/O"), "mail_unavailable"),
    (RuntimeError("kaput"), "internal_error"),  # the floor
])
def test_classification_map(exc, code):
    assert envelope.classify(exc) == code


def test_hierarchy_is_keyed_to_the_codes_namespace():
    from email_mcp.graph import GraphError
    from email_mcp.identities import IdentityError
    from email_mcp.transports import SendError
    from email_mcp.triage import TriageError

    assert NotFound("x").code == codes.NOT_FOUND
    assert InvalidInput("x").code == codes.INVALID_INPUT
    assert MailUnavailable("x").code == codes.MAIL_UNAVAILABLE
    # the send/triage families are members of the one hierarchy
    for cls in (SendError, IdentityError, GraphError, TriageError):
        assert issubclass(cls, ToolError)
    assert SendError("x").code == codes.DELIVERY_FAILED           # default
    assert SendError("x", code=codes.AUTH_FAILED).code == "auth_failed"
    assert IdentityError("x").code == codes.IDENTITY_MISCONFIGURED
    assert GraphError("x").code == codes.TRANSPORT_UNAVAILABLE
    assert TriageError("plan_expired", "gone").code == "plan_expired"


# --------------------------------------------------------------------- #
# the boundary                                                          #
# --------------------------------------------------------------------- #


def test_success_wraps_typed_value_with_ok():
    @envelope.tool
    def t() -> _Receipt:
        return _Receipt(id="X", n=3)

    assert t() == {"ok": True, "id": "X", "n": 3}


def test_value_with_its_own_ok_keeps_it():
    """doctor / refresh_mail report health through their own ok (the §2
    documented exceptions) — the boundary must not overwrite it."""
    @envelope.tool
    def t() -> dict:
        return {"ok": False, "error": "nudge failed"}

    out = t()
    assert out["ok"] is False and out["error"] == "nudge failed"


def test_typed_error_renders_code_without_fix_or_operation_id():
    @envelope.tool
    def t() -> dict:
        raise NotFound("no message 9")

    assert t() == {"ok": False, "code": "not_found", "error": "no message 9"}


def test_typed_error_carries_minted_id_and_data():
    @envelope.tool
    def t() -> dict:
        raise InvalidInput("cannot cancel S-1: already sent",
                           operation_id="S-1", data={"status": "sent"})

    out = t()
    assert out["operation_id"] == "S-1"
    assert out["status"] == "sent"


def test_op_from_threads_belt_failures_only():
    """The minted-id gate: the belt threads op_from (a crash after the
    artifact existed — §1 row 18); a typed reject decides for itself."""
    @envelope.tool(op_from="plan_id")
    def crash(plan_id: str) -> dict:
        raise RuntimeError("mid-apply")

    assert crash("P-1")["operation_id"] == "P-1"       # positional
    assert crash(plan_id="P-2")["operation_id"] == "P-2"

    @envelope.tool(op_from="plan_id")
    def reject(plan_id: str) -> dict:
        raise NotFound("no plan")

    assert "operation_id" not in reject("P-9")


def test_belt_classifies_logs_traceback_and_says_run_doctor():
    @envelope.tool
    def t() -> dict:
        raise RuntimeError("kaput")

    recorder = _Recorder()
    logger = logging.getLogger("email_mcp")  # propagate=False: hook directly
    logger.addHandler(recorder)
    try:
        out = t()
    finally:
        logger.removeHandler(recorder)

    assert out["ok"] is False and out["code"] == "internal_error"
    assert out["fix"] == "run doctor"
    assert out["error"] == "RuntimeError: kaput"
    assert any(r.exc_info and r.exc_info[0] is RuntimeError
               for r in recorder.records)
    assert "Traceback" not in str(out)


def test_belt_keeps_plain_prose_for_caller_fixable_codes():
    @envelope.tool
    def t() -> dict:
        raise ValueError("invalid ISO datetime: 'x'")

    out = t()
    assert out["code"] == "invalid_input"
    assert out["error"] == "invalid ISO datetime: 'x'"  # no type-name prefix


@pytest.mark.parametrize("exc", [
    NotFound("é" * 3000),       # typed path (multibyte at the cut)
    RuntimeError("x" * 5000),   # belt path
])
def test_failure_prose_is_byte_bounded_at_utf8_boundaries(exc):
    @envelope.tool
    def t() -> dict:
        raise exc

    err = t()["error"]
    raw = err.encode("utf-8")           # decodes back: valid UTF-8
    assert len(raw) <= envelope.MAX_ERROR_BYTES + len("…".encode("utf-8"))
    assert err.endswith("…")
