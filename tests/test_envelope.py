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

from email_mcp import codes, envelope, ids
from email_mcp.envelope import (
    InvalidInput, MailUnavailable, NotFound, ToolError,
)
from email_mcp.state import RefusedError


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
    (RefusedError("EMAIL_MCP_SPOOL_DIR is retired"), "invalid_input"),
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

    p1, p2 = ids.new_id(), ids.new_id()
    assert crash(p1)["operation_id"] == p1             # positional
    assert crash(plan_id=p2)["operation_id"] == p2

    @envelope.tool(op_from="plan_id")
    def reject(plan_id: str) -> dict:
        raise NotFound("no plan")

    assert "operation_id" not in reject(ids.new_id())


def test_op_from_gates_on_the_minted_vocabulary():
    """The caller's argument is a claim, not proof of a mint (§2): only a
    string in the minted vocabulary threads as operation_id."""
    @envelope.tool(op_from="plan_id")
    def crash(plan_id: str) -> dict:
        raise RuntimeError("mid-apply")

    for claim in ("P-1", "Z" * 60000, "", None, 42):
        assert "operation_id" not in crash(claim)


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


def test_policy_refusal_is_invalid_input_with_the_bare_reason():
    """A refused state root (§3.5 is the belt, this is not): the one
    policy string, verbatim — no 'RefusedError: ' prefix."""
    @envelope.tool
    def t() -> dict:
        raise RefusedError("EMAIL_MCP_SPOOL_DIR is retired — "
                           "use EMAIL_MCP_STATE_DIR.")

    out = t()
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert out["error"].startswith("EMAIL_MCP_SPOOL_DIR is retired")


class _EvilStr(Exception):
    def __str__(self):
        raise RuntimeError("unrenderable")


class _EvilTyped(ToolError):
    def __str__(self):
        raise RuntimeError("unrenderable")


@pytest.mark.parametrize("exc,code", [
    (_EvilStr(), "internal_error"),   # belt arm
    (_EvilTyped("x"), "internal_error"),  # typed arm
])
def test_exception_whose_str_raises_never_escapes(exc, code):
    """§7 'no exception escapes to the wire' includes the exception
    thrown while RENDERING the first one."""
    @envelope.tool
    def t() -> dict:
        raise exc

    out = t()
    assert out["ok"] is False and out["code"] == code
    assert type(exc).__name__ in out["error"]


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


_CAP = envelope.MAX_ERROR_BYTES + len("…".encode("utf-8"))


def test_value_carried_failure_is_bounded_wholesale():
    """refresh_mail / mailbox_delete report failure as a VALUE (§2
    documented exceptions) — every string of that ok:false envelope is
    bounded, including echoed caller arguments."""
    @envelope.tool
    def t() -> dict:
        return {"ok": False, "error": "x" * 60000, "path": "y" * 60000,
                "duration_ms": 7}

    out = t()
    assert out["ok"] is False and out["duration_ms"] == 7
    assert len(out["error"].encode("utf-8")) <= _CAP
    assert len(out["path"].encode("utf-8")) <= _CAP


def test_partial_failure_channels_are_bounded_inside_ok_true():
    """errors[]/failures[] are failure records riding in ok:true (§2) —
    bounded like any failure prose, while success payloads are untouched."""
    @envelope.tool
    def t() -> dict:
        return {"emails": [{"body_text": "b" * 5000}],
                "errors": [{"id": "Z" * 60000,
                            "error": "no message " + "Z" * 60000,
                            "code": "invalid_input"}]}

    out = t()
    assert len(out["emails"][0]["body_text"]) == 5000   # payload untouched
    entry = out["errors"][0]
    assert entry["code"] == "invalid_input"
    assert len(entry["id"].encode("utf-8")) <= _CAP
    assert len(entry["error"].encode("utf-8")) <= _CAP


def test_minted_id_vocabulary_is_ascii():
    """\\d matches Unicode digits, but strftime mints only ASCII — an id
    claim written in another script is not ours and never threads onto
    the wire as operation_id."""
    from email_mcp import ids

    assert ids.is_minted_id("20260801T123456Z-abcdefabcdef")
    assert not ids.is_minted_id("\u0662\u0660\u0662\u0666\u0660\u0668"
                                "\u0660\u0661T\u0661\u0662\u0663\u0664"
                                "\u0665\u0666Z-abcdefabcdef")
