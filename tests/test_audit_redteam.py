"""v0.10 S3 red team — the binding failure matrix (F1-F12) plus the
adversarial hunts beyond it, run against the REAL config.audit_dir resolver.

This module shadows conftest's autouse ``audit_dir_guard`` on purpose: the
conftest guard monkeypatches config.audit_dir to a pre-made lambda, which
would hide resolver-level behaviour (mkdir, chmod healing, hostile parents,
symlinks, dir-is-a-file). Here the guard pins ONLY the env var, so every
test exercises the true production path end to end.

F11 (the mutation mandate) is executed by a scratchpad runner outside
pytest — each mutation is applied to the source, its named killing test is
run and must FAIL, then the source is restored. The killing tests
themselves live here and in tests/test_audit.py / tests/test_audit_hooks.py.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from email_mcp import audit, config, ids, plans, sender, server, spool, triage
from email_mcp.plans import PlanAction
from email_mcp.sources.base import SearchQuery

SENTINEL = "XSENTINELX-BODY-73c1-never-in-the-ledger"


# --------------------------------------------------------------------- #
# fixtures + helpers                                                     #
# --------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def state_root_guard(tmp_path, monkeypatch):
    """Shadow conftest's guard: conftest patches config.state_root, which
    would defeat the real env-driven resolution these tests exercise."""
    root = tmp_path / "state"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def audit_dir_guard(state_root_guard):
    """Derived from the pinned ROOT — one root is the whole point. Not
    pre-created: emit's own mkdir path is part of the attack surface."""
    yield state_root_guard / "audit"
    audit.set_process("server")

@pytest.fixture
def send_env(monkeypatch, tmp_path, audit_dir_guard):
    """Documented defaults + isolated spool/plans/fts, audit re-pinned
    after the EMAIL_MCP_* wipe (mirrors tests/test_audit_hooks.py)."""
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(audit_dir_guard.parent))
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris@example.org")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Paris")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES",
                       str(tmp_path / "no-identities.toml"))


@pytest.fixture
def delivered(monkeypatch):
    """Mock the byte-level transport; record what would have been sent."""
    sent: list[bytes] = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", sent.append)
    return sent


class _WarnCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):  # noqa: A003 - logging API name
        self.records.append(record)


@pytest.fixture
def email_mcp_warnings():
    """The email_mcp logger has propagate=False (stdout is the wire), so
    caplog never sees it — attach a collector directly."""
    handler = _WarnCollector()
    logging.getLogger("email_mcp").addHandler(handler)
    yield handler.records
    logging.getLogger("email_mcp").removeHandler(handler)


def _lines(d: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(d.glob("*.jsonl")):
        out += path.read_text(encoding="utf-8").splitlines()
    return out


def _events(d: Path) -> list[dict]:
    return [json.loads(line) for line in _lines(d) if line.strip()]


def _seed(d: Path, month: str, records: list) -> Path:
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


def _iso_in(minutes: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes)).isoformat()


def _mail_source(mail_fixture):
    from email_mcp.sources.apple_mail import AppleMailSource
    return AppleMailSource(mail_base=mail_fixture)


def _mini_plan(summary: str = "mark_read: 2 msg(s)") -> plans.Plan:
    now = plans.utcnow()
    return plans.Plan(
        id=plans.new_id(now), created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(minutes=10)), status="draft",
        query={}, actions=[PlanAction(action="mark_read")], target=None,
        messages=[], summary=summary,
    )


# --------------------------------------------------------------------- #
# multiprocessing workers (module level: spawn/fork picklable)           #
# --------------------------------------------------------------------- #


def _hammer(dir_str: str, src: str, count: int) -> None:
    os.environ["EMAIL_MCP_STATE_DIR"] = str(Path(dir_str).parent)
    from email_mcp import audit as aud
    aud.set_process(src)
    for i in range(count):
        if aud.emit("send", outcome="sent",
                    detail={"src": src, "i": i}) is None:
            raise SystemExit(1)


def _frozen_hammer(dir_str: str, frozen_iso: str, tag: str,
                   count: int) -> None:
    os.environ["EMAIL_MCP_STATE_DIR"] = str(Path(dir_str).parent)
    from email_mcp import audit as aud
    from email_mcp import ids as idm
    frozen = datetime.fromisoformat(frozen_iso)
    idm.utcnow = lambda: frozen  # freeze the clock THIS process reads
    for i in range(count):
        if aud.emit("send", outcome="sent",
                    detail={"tag": tag, "i": i}) is None:
            raise SystemExit(1)


def _mint_ids(out_path: str, count: int) -> None:
    from email_mcp import ids as idm
    with open(out_path, "w", encoding="utf-8") as f:
        for _ in range(count):
            f.write(idm.new_id() + "\n")


def _f6_schedule_then_die(audit_dir: str, spool_dir: str,
                          identities: str, send_at: str) -> None:
    """Schedule (mutation lands durably in the spool), then die at the
    exact emit boundary — the documented at-most-once loss window."""
    os.environ["EMAIL_MCP_STATE_DIR"] = str(Path(audit_dir).parent)
    os.environ["EMAIL_MCP_IDENTITIES"] = identities
    os.environ["EMAIL_MCP_FROM_ADDR"] = "paris@example.org"
    from email_mcp import audit as aud
    from email_mcp import server as srv
    aud.emit = lambda *a, **k: os._exit(137)  # SIGKILL stand-in
    srv.tool_schedule_email(to="paris@example.org", subject="f6",
                            body="body-f6", send_at=send_at)
    os._exit(1)  # emit was never reached — the kill site did not fire


# --------------------------------------------------------------------- #
# F1 — two-process 1000-event append                                     #
# --------------------------------------------------------------------- #


def test_f1_two_process_thousand_event_append(audit_dir_guard):
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
    assert len(seen) == 1000  # exact count: nothing lost, nothing doubled
    # 0600/0700 hold through the concurrent hammering (kills 0600→0644).
    files = list(audit_dir_guard.glob("*.jsonl"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(audit_dir_guard.stat().st_mode) == 0o700


# --------------------------------------------------------------------- #
# F2 — torn tail line: skipped + counted, neighbours AND next emit live  #
# --------------------------------------------------------------------- #


def test_f2_torn_tail_skipped_and_next_emit_survives(audit_dir_guard):
    ops = [audit.emit("send", outcome="sent", detail={"i": i})
           for i in range(3)]
    assert all(ops)
    path, = audit_dir_guard.glob("*.jsonl")
    keep = path.read_bytes().splitlines(keepends=True)
    torn = b"".join(keep[:2]) + keep[2][: len(keep[2]) // 2].rstrip(b"\n")
    path.write_bytes(torn)  # crash mid-write: no trailing newline

    op4 = audit.emit("send", outcome="sent", detail={"i": 3})
    assert op4 is not None
    out = audit.query(limit=500)
    got = {e["op"] for e in out["events"]}
    assert got == {ops[0], ops[1], op4}  # others intact, NEW event intact
    assert out["skipped_lines"] == 1     # the torn fragment, counted once
    # The healed file ends cleanly: another emit appends a parseable line.
    op5 = audit.emit("send", outcome="sent", detail={"i": 4})
    assert op5 in {e["op"] for e in audit.query(limit=500)["events"]}


# --------------------------------------------------------------------- #
# F3 — corrupt binary line tolerated                                     #
# --------------------------------------------------------------------- #


def test_f3_corrupt_binary_line_tolerated(audit_dir_guard):
    op = audit.emit("send", outcome="sent")
    path, = audit_dir_guard.glob("*.jsonl")
    with open(path, "ab") as f:
        f.write(b"\x00\xfe\xff\x80 binary garbage \x9c\n")
        f.write(b"\xc3(broken utf-8 continuation\n")
        f.write(b"[1,2,3]\n")  # parses, but is not an event dict
    op2 = audit.emit("send", outcome="sent")
    out = audit.query(limit=500)
    assert {e["op"] for e in out["events"]} == {op, op2}
    assert out["skipped_lines"] == 3
    assert out["files_scanned"] == 1


# --------------------------------------------------------------------- #
# F4 — unwritable ledger never blocks mail                               #
# --------------------------------------------------------------------- #


def test_f4_unwritable_ledger_never_blocks_mail(
    send_env, delivered, mail_fixture, tmp_path, monkeypatch,
    email_mcp_warnings,
):
    """Hostile parent (0500, ledger dir absent — the un-mkdir-able case):
    send/schedule/triage/mailbox flows all return their normal results,
    every emit returns None, and each dropped event logs ONE warning."""
    ro = tmp_path / "ro"
    ro.mkdir()
    dead = ro / "audit"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(dead.parent))
    ro.chmod(0o500)
    try:
        ok = server.tool_send_email(to="paris@example.org",
                                    subject="f4-send", body="b")
        assert ok["ok"] is True and ok["message_id"]

        sch = server.tool_schedule_email(to="paris@example.org",
                                         subject="f4-sched", body="b",
                                         send_at=_iso_in(60))
        assert sch["ok"] is True and sch["id"]
        assert spool.load("pending", sch["id"]) is not None

        plan = triage.build_plan(_mail_source(mail_fixture),
                                 SearchQuery(unread_only=True),
                                 [{"action": "mark_read"}])
        assert plan.id and plans.load(plan.id) is not None

        monkeypatch.setattr(server, "_SOURCE", object())
        monkeypatch.setattr(
            server, "create_mailbox",
            lambda src, account, path: {
                "ok": True, "account": account, "path": path,
                "existed": False, "applescript": "OK",
                "index_verified": True, "mail_verified": True,
                "warning": None,
            })
        mb = server.tool_mailbox_create(account="ACC", path="Archive/F4")
        assert mb["ok"] is True

        assert audit.emit("send", outcome="sent") is None  # direct probe
    finally:
        ro.chmod(0o700)

    assert not dead.exists()  # nothing was ever written
    dropped = [r for r in email_mcp_warnings
               if "audit: emit" in r.getMessage()]
    # exactly ONE warning per dropped event: send, schedule, plan_create,
    # mailbox_create, direct probe
    assert len(dropped) == 5


def test_f4_unchmodable_0500_dir_drops_events_not_mail(
    send_env, delivered, tmp_path, monkeypatch,
):
    """A 0500 dir the process cannot heal (stand-in for an other-owner
    dir): resolver pinned past the chmod, emit -> None, mail flows on."""
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    frozen.chmod(0o500)
    monkeypatch.setattr(config, "audit_dir", lambda create=True: frozen)
    try:
        out = server.tool_send_email(to="paris@example.org",
                                     subject="f4b", body="b")
        assert out["ok"] is True
        assert audit.emit("send", outcome="sent") is None
        assert list(frozen.iterdir()) == []
    finally:
        frozen.chmod(0o700)


def test_f4_owned_0500_dir_self_heals(audit_dir_guard):
    """A pre-existing 0500 ledger dir is NOT silently re-chmodded — since
    v0.11 only directories this tool created are its to mode. The event is
    dropped (receipts lost, mail unaffected) and `doctor --fix` repairs the
    mode on request. The old self-heal was the same surprise-chmod that
    three release gates kept finding ways to abuse."""
    audit_dir_guard.mkdir(parents=True)
    audit_dir_guard.chmod(0o500)
    try:
        assert audit.emit("send", outcome="sent") is None  # dropped, no raise
        assert stat.S_IMODE(audit_dir_guard.stat().st_mode) == 0o500
    finally:
        audit_dir_guard.chmod(0o700)


# --------------------------------------------------------------------- #
# F5 — body sentinel never reaches the ledger                            #
# --------------------------------------------------------------------- #


def test_f5_body_sentinel_grep_is_empty(
    send_env, delivered, mail_fixture, audit_dir_guard, monkeypatch,
):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    assert server.tool_send_email(to="paris@example.org", subject="f5-send",
                                  body=f"top {SENTINEL} secret")["ok"]
    sch = server.tool_schedule_email(to="paris@example.org",
                                     subject="f5-sched",
                                     body=f"later {SENTINEL}",
                                     send_at=_iso_in(60))
    assert sch["ok"] is True
    monkeypatch.setattr(server, "_SOURCE", _mail_source(mail_fixture))
    assert server.tool_reply_email(id="100",
                                   body=f"re: {SENTINEL}")["ok"] is True
    # triage: the fixture message 100's BODY says "should be retracted";
    # its subject (allowed in the ledger) does not.
    plan = triage.build_plan(_mail_source(mail_fixture),
                             SearchQuery(unread_only=True),
                             [{"action": "mark_read"}])
    plans.finish(plans.claim(plan.id), "applied",
                 {"planned": 1, "acted": 1, "verified": 1})
    assert server.tool_cancel_scheduled(sch["id"])["ok"] is True

    blob = b"".join(p.read_bytes()
                    for p in audit_dir_guard.rglob("*") if p.is_file())
    text = blob.decode("utf-8", errors="replace")
    assert SENTINEL not in text
    assert "should be retracted" not in text  # fixture body text
    # the grep is not vacuous: all six mutations left their events
    assert text.count('"event":"') >= 6
    for name in ("send", "schedule", "reply", "plan_create",
                 "plan_finish", "cancel"):
        assert f'"event":"{name}"' in text


# --------------------------------------------------------------------- #
# F6 — kill between mutation and emit (the documented loss window)       #
# --------------------------------------------------------------------- #


def test_f6_kill_between_mutation_and_emit(send_env, tmp_path,
                                           audit_dir_guard):
    from email_mcp import doctor

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_f6_schedule_then_die, args=(
        str(audit_dir_guard), str(tmp_path / "spool"),
        str(tmp_path / "no-identities.toml"), _iso_in(60)))
    p.start()
    p.join(timeout=120)
    assert p.exitcode == 137  # died AT the emit boundary, not before/after

    # Mutation durable: the spool entry exists and is deliverable.
    pending = spool.entries("pending")
    assert len(pending) == 1 and pending[0].subject == "f6"
    # Event absent — this IS the documented at-most-once window.
    if audit_dir_guard.is_dir():
        assert _lines(audit_dir_guard) == []
    else:
        assert not audit_dir_guard.exists()
    # Neither doctor nor the audit tool crash over the gap.
    check = doctor.check_audit()
    assert isinstance(check, dict) and "detail" in check
    out = server.tool_audit()
    assert out["ok"] is True and out["events"] == []


# --------------------------------------------------------------------- #
# F7 — month-boundary two-process rollover (frozen clocks)               #
# --------------------------------------------------------------------- #


def test_f7_month_boundary_two_process_rollover(audit_dir_guard):
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_frozen_hammer, args=(
            str(audit_dir_guard), "2026-07-31T23:59:59+00:00", "july", 150)),
        ctx.Process(target=_frozen_hammer, args=(
            str(audit_dir_guard), "2026-08-01T00:00:00+00:00", "aug", 150)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    july = (audit_dir_guard / "2026-07.jsonl").read_text().splitlines()
    aug = (audit_dir_guard / "2026-08.jsonl").read_text().splitlines()
    assert len(july) == 150 and len(aug) == 150
    for line in july:
        rec = json.loads(line)
        assert rec["ts"].startswith("2026-07-31T23:59:59")
        assert rec["detail"]["tag"] == "july"
    for line in aug:
        rec = json.loads(line)
        assert rec["ts"].startswith("2026-08-01T00:00:00")
        assert rec["detail"]["tag"] == "aug"

    merged = audit.query(limit=500)
    assert len(merged["events"]) == 300
    assert merged["files_scanned"] == 2
    tss = [e["ts"] for e in merged["events"]]
    assert tss == sorted(tss, reverse=True)  # Aug strictly before July
    only_aug = audit.query(since="2026-08", limit=500)
    assert len(only_aug["events"]) == 150
    assert only_aug["files_scanned"] == 1  # July pruned by filename


# --------------------------------------------------------------------- #
# F8 — clock skew: stable sort + filters                                 #
# --------------------------------------------------------------------- #


def test_f8_backdated_and_forward_dated_ts_sort_and_filter(audit_dir_guard):
    same = "2026-07-15T12:00:00+00:00"
    _seed(audit_dir_guard, "2026-07", [
        _event("2020-01-01T00:00:00+00:00", "past"),    # backdated
        _event(same, "a"), _event(same, "b"), _event(same, "c"),
        _event("2030-01-01T00:00:00+00:00", "future"),  # forward-dated
        _event("2026-07-10T08:00:00+00:00", "normal"),
    ])
    out = audit.query(limit=500)
    ops = [e["op"] for e in out["events"]]
    assert ops[0] == "future" and ops[-1] == "past"
    # stable among ts ties: later-appended first (newest-first semantics)
    assert ops[1:4] == ["c", "b", "a"]
    tss = [e["ts"] for e in out["events"]]
    assert tss == sorted(tss, reverse=True)

    assert [e["op"] for e in audit.query(since="2026-01")["events"]] \
        == ["future", "c", "b", "a", "normal"]  # 2020 filtered by ts
    assert [e["op"] for e in audit.query(until="2026-07-31")["events"]] \
        == ["c", "b", "a", "normal", "past"]    # 2030 filtered by ts
    assert [e["op"] for e in
            audit.query(since="2026-07-11", until="2026-07-31")["events"]] \
        == ["c", "b", "a"]


def test_f8_same_second_same_op_ordering(audit_dir_guard, monkeypatch):
    frozen = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ids, "utcnow", lambda: frozen)
    for i in range(3):
        assert audit.emit("deliver", outcome="retry", operation_id="OPX",
                          detail={"i": i}) == "OPX"
    out = audit.query(operation_id="OPX")
    assert [e["detail"]["i"] for e in out["events"]] == [2, 1, 0]


# --------------------------------------------------------------------- #
# F9 — worst-case 200-message plan_finish stays within the cap           #
# --------------------------------------------------------------------- #


def test_f9_worst_case_plan_finish_truncates_in_order(send_env,
                                                      audit_dir_guard):
    long_summary = ("move 200 huge messages to Archive — " + "s" * 160)[:200]

    # (a) failures/pending are the bulk: they collapse to counts FIRST and
    # the rest of detail (counts, short error) survives.
    plan_a = _mini_plan(long_summary)
    plans.finish(plan_a, "failed", {
        "planned": 200, "acted": 190, "verified": 180,
        "failures": [{"id": f"<{'x' * 120}-{i:03d}@example.org>",
                      "code": "applescript",
                      "detail": "long prose " * 40} for i in range(200)],
        "pending": [{"id": f"<{'p' * 120}-{i:03d}@example.org>"}
                    for i in range(200)],
        "error": "short error",
    })
    # (b) a pathological non-list detail field: detail drops WHOLESALE,
    # envelope + summary still survive.
    plan_b = _mini_plan(long_summary)
    plans.finish(plan_b, "failed", {"error": "y" * (3 * audit.MAX_EVENT_BYTES)})

    lines = _lines(audit_dir_guard)
    assert len(lines) == 2
    for line in lines:
        assert len(line.encode("utf-8")) <= audit.MAX_EVENT_BYTES
    ev_a, ev_b = (json.loads(line) for line in lines)
    assert ev_a["detail"] == {"planned": 200, "acted": 190, "verified": 180,
                              "failures": 200, "pending": 200,
                              "error": "short error", "truncated": True}
    assert ev_b["detail"] == {"truncated": True}
    for ev, plan in ((ev_a, plan_a), (ev_b, plan_b)):
        assert ev["op"] == plan.id and ev["summary"] == long_summary
        for key in ("v", "ts", "op", "src", "event", "outcome"):
            assert key in ev  # the envelope always survives


# --------------------------------------------------------------------- #
# F10 — 10k id mints across 4 forked processes, zero collisions          #
# --------------------------------------------------------------------- #


def test_f10_ten_thousand_id_mints_across_four_forks(tmp_path):
    ctx = multiprocessing.get_context("fork")
    outs = [tmp_path / f"ids-{i}.txt" for i in range(4)]
    procs = [ctx.Process(target=_mint_ids, args=(str(o), 2500))
             for o in outs]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    minted: list[str] = []
    for o in outs:
        minted += o.read_text().splitlines()
    assert len(minted) == 10_000
    assert len(set(minted)) == 10_000  # zero collisions, cross-process too


# --------------------------------------------------------------------- #
# F11 — deterministic killer for the split-write mutation                #
# --------------------------------------------------------------------- #


def test_f11_emit_is_exactly_one_write_syscall(audit_dir_guard,
                                               monkeypatch):
    """A line MUST land in one os.write on the O_APPEND fd — two writers'
    lines can then never interleave. Counting the syscall kills the
    'split the single write' mutation deterministically (the interleave
    itself is a race F1 only catches probabilistically)."""
    audit.emit("send", outcome="sent")  # healthy, newline-terminated file
    calls: list[bytes] = []
    real_write = os.write

    def counting_write(fd, data):
        calls.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", counting_write)
    op = audit.emit("send", outcome="sent", detail={"n": 2})
    assert op is not None
    assert len(calls) == 1              # ONE write per event
    assert calls[0].endswith(b"\n")     # newline travels IN that write
    json.loads(calls[0].decode("utf-8"))


# --------------------------------------------------------------------- #
# F12 — audit tool over a corrupted ledger                               #
# --------------------------------------------------------------------- #


def test_f12_audit_tool_over_corrupted_ledger(audit_dir_guard):
    _seed(audit_dir_guard, "2026-06",
          [_event("2026-06-15T12:00:00+00:00", "ok-june")])
    with open(audit_dir_guard / "2026-07.jsonl", "wb") as f:
        f.write(json.dumps(
            _event("2026-07-01T00:00:00+00:00", "ok-july")).encode() + b"\n")
        f.write(b'{"v":1,"ts":"2026-07-01T00:00:01+00:00","op":"torn')
        f.write(b"\n\x00\xff\x80 binary\n")
        f.write(b'"just-a-string"\n')
    (audit_dir_guard / "2026-05.jsonl").mkdir()   # month-NAMED directory
    (audit_dir_guard / "README.txt").write_text("not a ledger file")

    out = server.tool_audit()
    assert out["ok"] is True
    assert out.get("code") is None                # never internal_error
    assert {e["op"] for e in out["events"]} == {"ok-june", "ok-july"}
    assert out["skipped_lines"] == 3
    assert out["files_scanned"] == 2              # dir + txt never counted
    json.dumps(out)                               # wire-serializable

    # invalid bounds are coded envelopes, not crashes
    bad = server.tool_audit(since="not-a-date")
    assert bad["ok"] is False and bad["code"] == "invalid_input"
    # semantically inverted bounds are simply empty
    assert server.tool_audit(since="2026-08",
                             until="2026-07")["events"] == []


# --------------------------------------------------------------------- #
# beyond the matrix — adversarial hunts                                  #
# --------------------------------------------------------------------- #


def test_emit_coerces_nonserializable_detail(audit_dir_guard):
    """A hook passing Path/datetime/bytes/set inside detail must cost a
    coerced value, never the event (regression: json TypeError used to
    drop the whole receipt)."""
    op = audit.emit("deliver", outcome="sent", detail={
        "path": Path("/tmp/x.eml"),
        "when": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "blob": b"bytes",   # key renamed from "raw": that key is now FENCED
        "codes": {1, 2},    # (F2 detail denylist) — coercion intent unchanged
    })
    assert op is not None
    rec, = _events(audit_dir_guard)
    assert rec["detail"]["path"] == "/tmp/x.eml"
    assert rec["detail"]["when"].startswith("2026-07-30")
    assert isinstance(rec["detail"]["blob"], str)
    assert isinstance(rec["detail"]["codes"], str)


def test_emit_circular_detail_drops_event_without_raising(audit_dir_guard):
    loop: dict = {}
    loop["self"] = loop
    assert audit.emit("send", outcome="sent", detail=loop) is None
    assert audit.query()["events"] == []  # dropped, logged, never raised


def test_query_limit_and_bound_extremes(audit_dir_guard):
    _seed(audit_dir_guard, "2026-07", [
        _event(f"2026-07-01T{h:02d}:{m:02d}:00+00:00", f"o{h * 60 + m}")
        for h in range(10) for m in range(60)
    ])  # 600 events
    assert len(audit.query(limit=99_999)["events"]) == 500
    assert len(audit.query(limit=0)["events"]) == 1
    assert len(audit.query(limit=-7)["events"]) == 1
    assert len(audit.query(limit=None)["events"]) == 50
    assert len(audit.query(limit="25")["events"]) == 25
    assert audit.query(since="2026-08", until="2026-07")["events"] == []
    assert audit.query(since="2026-07-01T09:00:00+00:00",
                       until="2026-07-01T01:00:00+00:00")["events"] == []


def test_unicode_and_emoji_subjects_roundtrip(audit_dir_guard):
    subject = "🚀" * 150 + "重要" * 30 + "עברית"
    op = audit.emit("send", outcome="sent", subject=subject,
                    to=["ünï@例え.jp"])
    assert op is not None
    rec, = _events(audit_dir_guard)
    assert rec["subject"] == subject[:200]  # 200 CHARS, unicode-safe
    assert rec["to"] == ["ünï@例え.jp"]
    assert audit.query()["events"][0]["subject"] == subject[:200]


def test_megabyte_single_fields_stay_capped(audit_dir_guard):
    audit.emit("send", outcome="sent", summary="s" * 1_000_000)
    audit.emit("send", outcome="sent", summary="keep",
               message_id="m" * 1_000_000)
    lines = _lines(audit_dir_guard)
    assert len(lines) == 2
    for line in lines:
        assert len(line.encode("utf-8")) <= audit.MAX_EVENT_BYTES
    a, b = (json.loads(line) for line in lines)
    assert a["summary"] and set(a["summary"]) == {"s"}  # halved, survives
    assert b["summary"] == "keep" and "message_id" not in b
    for rec in (a, b):
        for key in ("v", "ts", "op", "src", "event", "outcome"):
            assert key in rec


def test_symlinked_state_root_works(tmp_path, monkeypatch):
    """Relocating the ROOT with a link is supported (a symlinked managed
    LEAF is the refused case — see test_config_state_dirs)."""
    target = tmp_path / "real-state"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(link))
    op = audit.emit("send", outcome="sent")
    assert op is not None
    assert len(list((target / "audit").glob("*.jsonl"))) == 1  # through the link
    assert [e["op"] for e in audit.query()["events"]] == [op]


def test_audit_dir_is_a_regular_file(tmp_path, monkeypatch):
    from email_mcp import doctor

    root = tmp_path / "state"
    root.mkdir()
    imposter = root / "audit"
    imposter.write_text("I am a file, not a directory")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))
    assert audit.emit("send", outcome="sent") is None  # dropped, no raise
    assert audit.query() == {"events": [], "files_scanned": 0,
                             "skipped_lines": 0}
    out = server.tool_audit()
    assert out["ok"] is True and out["events"] == []
    assert isinstance(doctor.check_audit(), dict)  # no crash
    assert imposter.read_text() == "I am a file, not a directory"


def test_concurrent_query_while_appending(audit_dir_guard):
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_hammer,
                    args=(str(audit_dir_guard), "server", 800))
    p.start()
    try:
        polls = 0
        while p.is_alive():
            out = audit.query(limit=500)
            assert out["skipped_lines"] == 0  # atomic lines: never torn
            for e in out["events"]:
                assert e["v"] == 1 and e["outcome"] == "sent"
            polls += 1
    finally:
        p.join(timeout=120)
    assert p.exitcode == 0
    final = audit.query(limit=500)
    assert final["skipped_lines"] == 0
    assert len(_lines(audit_dir_guard)) == 800
    assert polls >= 1  # the race was actually exercised
