"""Audit ledger core (email_mcp.audit + email_mcp.ids) — v0.10 S1.

The suite-wide EMAIL_MCP_AUDIT_DIR guard moves to conftest.py at S2 (when
server/dispatcher hooks start emitting from other test files); until then
the autouse fixture below isolates every test in THIS file.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import random
import re
import stat
from pathlib import Path

import pytest

from email_mcp import audit


@pytest.fixture(autouse=True)
def audit_dir_guard(tmp_path, monkeypatch):
    """Point the ledger at a per-test tmp dir — nothing here may ever
    touch ~/.email-mcp/audit — and reset the process tag afterwards."""
    d = tmp_path / "audit"
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(d))
    yield d
    audit.set_process("server")


def _lines(d: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(d.glob("*.jsonl")):
        out += path.read_text(encoding="utf-8").splitlines()
    return out


def _seed(d: Path, month: str, records: list) -> Path:
    """Write a monthly file from dicts (serialized) and/or raw lines."""
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{month}.jsonl"
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _event(ts: str, op: str, **extra) -> dict:
    rec = {"v": 1, "ts": ts, "op": op, "src": "server",
           "event": "send", "outcome": "sent"}
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------- #
# emit                                                                    #
# ---------------------------------------------------------------------- #


def test_emit_writes_single_parseable_envelope_line(audit_dir_guard):
    op = audit.emit("send", outcome="sent", tool="send_email",
                    message_id="<m1@x>", to=["a@b.c"])
    lines = _lines(audit_dir_guard)
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["v"] == audit.SCHEMA_VERSION == 1
    assert rec["op"] == op
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", op)  # ids.new_id shape
    assert rec["src"] == "server"
    assert rec["event"] == "send"
    assert rec["outcome"] == "sent"
    assert rec["tool"] == "send_email"
    assert rec["message_id"] == "<m1@x>"
    assert rec["to"] == ["a@b.c"]
    # Monthly file is named from the event's own ts.
    names = [p.name for p in audit_dir_guard.glob("*.jsonl")]
    assert names == [rec["ts"][:7] + ".jsonl"]


def test_emit_threads_operation_id_and_omits_null_fields(audit_dir_guard):
    spool_id = "20260730T000000Z-abc123"
    op = audit.emit("deliver", outcome="retry", operation_id=spool_id,
                    spool_id=spool_id, identity=None, subject=None,
                    detail=None)
    assert op == spool_id
    rec = json.loads(_lines(audit_dir_guard)[0])
    assert rec["op"] == spool_id
    assert rec["spool_id"] == spool_id
    for absent in ("identity", "subject", "detail", "tool", "account"):
        assert absent not in rec


def test_emit_truncates_subject_to_200_chars(audit_dir_guard):
    audit.emit("send", outcome="sent", subject="s" * 500)
    rec = json.loads(_lines(audit_dir_guard)[0])
    assert rec["subject"] == "s" * 200


def test_emit_truncation_collapses_failures_and_pending(audit_dir_guard):
    failures = [{"id": f"<msg-{i:04d}@example.org>", "code": "applescript"}
                for i in range(400)]
    pending = [f"<pending-{i:04d}@example.org>" for i in range(400)]
    audit.emit("plan_finish", outcome="failed", plan_id="p1",
               summary="move 200 to Archive",
               detail={"failures": failures, "pending": pending, "moved": 3})
    line = _lines(audit_dir_guard)[0]
    assert len(line.encode("utf-8")) <= audit.MAX_EVENT_BYTES
    rec = json.loads(line)
    assert rec["detail"] == {"failures": 400, "pending": 400, "moved": 3,
                             "truncated": True}
    assert rec["summary"] == "move 200 to Archive"  # summary survives


def test_emit_truncation_drops_detail_wholesale(audit_dir_guard):
    audit.emit("schedule", outcome="scheduled", summary="the summary",
               detail={"blob": "x" * (2 * audit.MAX_EVENT_BYTES)})
    line = _lines(audit_dir_guard)[0]
    assert len(line.encode("utf-8")) <= audit.MAX_EVENT_BYTES
    rec = json.loads(line)
    assert rec["detail"] == {"truncated": True}
    assert rec["summary"] == "the summary"
    for key in ("v", "ts", "op", "src", "event", "outcome"):
        assert key in rec  # envelope always survives


def test_emit_never_raises_and_returns_none_when_unwritable(
    tmp_path, monkeypatch
):
    parent = tmp_path / "ro"
    parent.mkdir()
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(parent / "audit"))
    parent.chmod(0o500)  # audit dir cannot be created underneath
    try:
        assert audit.emit("send", outcome="sent") is None  # no raise
    finally:
        parent.chmod(0o700)
    assert not (parent / "audit").exists()


def test_env_overridden_dir_never_chmods_parent(audit_dir_guard):
    """Gate regression: with EMAIL_MCP_AUDIT_DIR set, the parent is not
    ours — chmod'ing it can raise (root-owned temp roots) and would cost
    the event. Only the default ~/.email-mcp parent gets locked down."""
    parent = audit_dir_guard.parent
    parent.chmod(0o755)
    assert audit.emit("send", outcome="sent") is not None
    assert len(_lines(audit_dir_guard)) == 1  # the event landed
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755  # parent untouched


def test_emit_file_and_dir_permissions(audit_dir_guard):
    audit.emit("send", outcome="sent")
    files = list(audit_dir_guard.glob("*.jsonl"))
    assert len(files) == 1
    assert stat.S_IMODE(audit_dir_guard.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


def test_set_process_tags_src(audit_dir_guard):
    audit.set_process("dispatcher")
    audit.emit("deliver", outcome="sent")
    rec = json.loads(_lines(audit_dir_guard)[0])
    assert rec["src"] == "dispatcher"


def _hammer(dir_str: str, src: str, count: int) -> None:
    """Spawned-process worker: emit `count` events into the given ledger."""
    os.environ["EMAIL_MCP_AUDIT_DIR"] = dir_str
    from email_mcp import audit as aud
    aud.set_process(src)
    for i in range(count):
        if aud.emit("send", outcome="sent",
                    detail={"src": src, "i": i}) is None:
            raise SystemExit(1)


def test_two_process_concurrent_append(audit_dir_guard):
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_hammer, args=(str(audit_dir_guard), src, 500))
        for src in ("server", "dispatcher")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    lines = _lines(audit_dir_guard)
    assert len(lines) == 1000
    seen = set()
    for line in lines:
        rec = json.loads(line)  # every line parses — no interleaving
        seen.add((rec["detail"]["src"], rec["detail"]["i"]))
    assert len(seen) == 1000  # nothing lost, nothing doubled


# ---------------------------------------------------------------------- #
# query / tail                                                            #
# ---------------------------------------------------------------------- #


def test_query_newest_first_and_limit_clamped(audit_dir_guard):
    recs = [
        _event(f"2026-01-01T{h:02d}:{m:02d}:00+00:00", f"o{h * 60 + m}")
        for h in range(9) for m in range(60)
    ]  # 540 events
    random.Random(7).shuffle(recs)  # non-monotonic on disk (clock skew)
    _seed(audit_dir_guard, "2026-01", recs)

    out = audit.query(limit=10_000)
    assert len(out["events"]) == 500  # clamped high
    tss = [e["ts"] for e in out["events"]]
    assert tss == sorted(tss, reverse=True)  # newest first, stable
    assert out["events"][0]["op"] == "o539"
    assert len(audit.query()["events"]) == 50  # default
    assert len(audit.query(limit=0)["events"]) == 1  # clamped low
    assert len(audit.query(limit=-3)["events"]) == 1


def test_query_tolerates_corrupt_and_torn_lines(audit_dir_guard):
    _seed(audit_dir_guard, "2026-02", [
        _event("2026-02-01T00:00:00+00:00", "g1"),
        '{"v":1,"ts":"2026-02-01T00:00:01+00:00","op":"torn',  # torn write
        "not json at all",
        "42",  # parses but is not an event
    ])
    out = audit.query()
    assert [e["op"] for e in out["events"]] == ["g1"]
    assert out["skipped_lines"] == 3
    assert out["files_scanned"] == 1


def test_query_prunes_months_and_filters_fields(audit_dir_guard):
    _seed(audit_dir_guard, "2026-06", [
        _event("2026-06-15T12:00:00+00:00", "june1", tool="send_email"),
    ])
    _seed(audit_dir_guard, "2026-07", [
        _event("2026-07-01T08:00:00+00:00", "july1", src="dispatcher",
               event="deliver", outcome="retry", spool_id="s1"),
        _event("2026-07-02T09:00:00+00:00", "july2", event="plan_finish",
               outcome="applied", plan_id="p1"),
    ])

    since_july = audit.query(since="2026-07")
    assert since_july["files_scanned"] == 1  # June pruned by filename
    assert {e["op"] for e in since_july["events"]} == {"july1", "july2"}

    until_june = audit.query(until="2026-06-30")
    assert until_june["files_scanned"] == 1
    assert [e["op"] for e in until_june["events"]] == ["june1"]

    assert [e["op"] for e in audit.query(tool="send_email")["events"]] \
        == ["june1"]
    assert [e["op"] for e in audit.query(event="deliver")["events"]] \
        == ["july1"]
    assert [e["op"] for e in audit.query(plan_id="p1")["events"]] \
        == ["july2"]
    assert [e["op"] for e in audit.query(operation_id="july1")["events"]] \
        == ["july1"]
    # ts-level bound inside a non-pruned month
    assert [e["op"] for e in audit.query(since="2026-07-02")["events"]] \
        == ["july2"]


def test_query_absent_dir_returns_empty(tmp_path, monkeypatch):
    missing = tmp_path / "never-created"
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(missing))
    assert audit.query() == {
        "events": [], "files_scanned": 0, "skipped_lines": 0,
    }
    assert not missing.exists()  # create=False never mkdirs


def test_cli_tail_and_status(audit_dir_guard, capsys):
    for i in range(7):
        audit.emit("send", outcome="sent", operation_id=f"op{i}")

    assert audit.main(["--tail", "5"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["op"] for line in out] \
        == ["op2", "op3", "op4", "op5", "op6"]  # last 5, oldest first

    assert audit.main(["--event", "send", "--limit", "2"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert json.loads(out[0])["op"] == "op6"  # query mode: newest first

    assert audit.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert str(audit_dir_guard) in out
    assert "files: 1" in out
    assert "last event:" in out and "send/sent" in out

    assert audit.main([]) == 2  # bare invocation prints help
    assert "usage:" in capsys.readouterr().out
