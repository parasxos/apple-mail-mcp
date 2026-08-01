"""S2 hook tests: every mutation site records exactly ONE audit event, in
the process that decides the outcome, threaded by the durable artifact id
(spool id / plan id — the artifact-id rule).

Transport, Graph and AppleScript boundaries are all mocked; the ledger
lands in the conftest-pinned per-test tmp dir. 13 tests, one per hook
site (or tightly related family) from the plan's hook table, plus the
doctor ledger-check test (the `audit` section of the report)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

import pytest

from email_mcp import config, dispatcher, plans, sender, server, spool, triage
from email_mcp.plans import PlanAction
from email_mcp.triage import TriageError


def _events(d: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(d.glob("*.jsonl")):
        out += [json.loads(line)
                for line in p.read_text(encoding="utf-8").splitlines()]
    return out


def _iso_in(minutes: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def send_env(monkeypatch, tmp_path, state_dir_guard):
    """Documented defaults (mirrors the sender suites); the state root is
    re-pinned after the EMAIL_MCP_* wipe — spool/plans/audit all live
    under the conftest guard's root."""
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(state_dir_guard))
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris@example.org")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Paris")
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES",
                       str(tmp_path / "no-identities.toml"))


@pytest.fixture
def delivered(monkeypatch):
    """Mock the byte-level transport; record what would have been sent."""
    sent: list[bytes] = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", sent.append)
    monkeypatch.setattr(dispatcher, "_notify", lambda *a, **k: None)
    return sent


# --------------------------------------------------------------------- #
# transmission family — server tool layer, EVERY terminal outcome       #
# --------------------------------------------------------------------- #


def test_send_email_emits_send_on_both_terminals(
    send_env, delivered, audit_dir_guard
):
    ok = server.tool_send_email(to="paris@example.org", subject="hi",
                                body="b")
    assert ok["ok"] is True
    blocked = server.tool_send_email(to="stranger@example.org",
                                     subject="hi", body="b")
    assert blocked["ok"] is False  # allowlist guard fired

    evs = _events(audit_dir_guard)
    assert [(e["event"], e["outcome"]) for e in evs] == \
        [("send", "sent"), ("send", "failed")]
    sent_ev, fail_ev = evs
    assert sent_ev["src"] == "server" and sent_ev["tool"] == "send_email"
    assert sent_ev["message_id"] == ok["message_id"]
    assert sent_ev["to"] == ["paris@example.org"]
    assert "allowlist" in fail_ev["detail"]["error"]
    assert fail_ev["subject"] == "hi"
    # no artifact existed → each op is a fresh mint, never shared
    assert sent_ev["op"] != fail_ev["op"]


def test_reply_email_emits_reply_with_orig_id_and_reply_all(
    send_env, delivered, audit_dir_guard, mail_fixture, monkeypatch
):
    from email_mcp.sources.apple_mail import AppleMailSource

    monkeypatch.setattr(server, "_SOURCE",
                        AppleMailSource(mail_base=mail_fixture))
    blocked = server.tool_reply_email(id="100", body="x")  # allowlist fires
    assert blocked["ok"] is False
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    ok = server.tool_reply_email(id="100", body="x", reply_all=True)
    assert ok["ok"] is True

    evs = [e for e in _events(audit_dir_guard) if e["event"] == "reply"]
    assert [(e["outcome"], e["detail"]["orig_id"], e["detail"]["reply_all"])
            for e in evs] == [("failed", "100", False),
                              ("sent", "100", True)]
    assert evs[1]["message_id"] == ok["message_id"]
    assert evs[1]["tool"] == "reply_email"


def test_schedule_email_emits_schedule_threaded_by_spool_id(
    send_env, audit_dir_guard
):
    ok = server.tool_schedule_email(to="paris@example.org", subject="later",
                                    body="b", send_at=_iso_in(60))
    assert ok["ok"] is True
    failed = server.tool_schedule_email(
        to="paris@example.org", subject="later", body="b",
        send_at="2020-01-01T00:00:00+00:00")
    assert failed["ok"] is False

    evs = [e for e in _events(audit_dir_guard) if e["event"] == "schedule"]
    assert [e["outcome"] for e in evs] == ["scheduled", "failed"]
    sched = evs[0]
    assert sched["op"] == ok["id"] == sched["spool_id"]  # artifact-id rule
    assert sched["message_id"] == ok["message_id"]
    assert sched["detail"] == {"executor": "launchd",
                               "send_at": ok["send_at"],
                               "draft_id": None, "graph_fallback": False}
    assert "past" in evs[1]["detail"]["error"]


def test_schedule_email_records_graph_fallback(
    send_env, audit_dir_guard, monkeypatch
):
    from email_mcp import graph
    from email_mcp import identities as idmod

    ident = idmod.Identity(name="g", from_addr="paris@example.org",
                           driver="pipe", allow_all=True, bcc_self=False,
                           executor="graph")
    monkeypatch.setattr(idmod, "get", lambda name=None: ident)

    def _refuse(ident_, raw, when):
        raise graph.GraphError("Exchange refused the deferred draft")

    monkeypatch.setattr(graph, "create_deferred_draft", _refuse)
    out = server.tool_schedule_email(to="paris@example.org", subject="g",
                                     body="b", send_at=_iso_in(30))
    assert out["ok"] is True and out["executor"] == "launchd"

    ev, = [e for e in _events(audit_dir_guard) if e["event"] == "schedule"]
    # graph_fallback = identity asked for graph, entry landed on launchd
    assert ev["detail"]["graph_fallback"] is True
    assert ev["detail"]["executor"] == "launchd"
    assert ev["identity"] == "g"


def test_cancel_emits_cancelled_and_too_late_sent(
    send_env, audit_dir_guard, monkeypatch
):
    plain = sender.schedule_email(to="paris@example.org", subject="c1",
                                  body="b", send_at=_iso_in(30))
    assert server.tool_cancel_scheduled(plain.id)["ok"] is True

    # Graph entry whose draft is gone AND already in Sent Items (F10):
    # the moved-to-sent ok:false terminal.
    from email_mcp import graph
    from email_mcp import identities as idmod

    late = sender.schedule_email(to="paris@example.org", subject="c2",
                                 body="b", send_at=_iso_in(30))
    e = spool.load("pending", late.id)
    e.executor = "graph"
    spool.update("pending", e)
    monkeypatch.setattr(idmod, "get", lambda name=None: idmod.Identity(
        name="g", from_addr="paris@example.org"))
    monkeypatch.setattr(graph, "find_draft_by_message_id", lambda i, m: None)
    monkeypatch.setattr(graph, "sent_by_message_id", lambda i, m: True)
    out = server.tool_cancel_scheduled(late.id)
    assert out["ok"] is False and out["status"] == "sent"

    evs = [e for e in _events(audit_dir_guard) if e["event"] == "cancel"]
    assert [(e["op"], e["outcome"]) for e in evs] == \
        [(plain.id, "cancelled"), (late.id, "too_late_sent")]
    assert all(e["tool"] == "cancel_scheduled" for e in evs)
    assert "detail" not in evs[0]  # terminal outcomes carry no reason


def test_cancel_emits_failed_with_reason(send_env, audit_dir_guard):
    assert server.tool_cancel_scheduled("nope-123")["ok"] is False

    claimed = sender.schedule_email(to="paris@example.org", subject="c3",
                                    body="b", send_at=_iso_in(30))
    assert spool.claim(claimed.id)  # a dispatcher grabbed it → sending/
    assert server.tool_cancel_scheduled(claimed.id)["ok"] is False

    evs = [e for e in _events(audit_dir_guard) if e["event"] == "cancel"]
    assert [(e["outcome"], e["detail"]["reason"]) for e in evs] == \
        [("failed", "not_found"), ("failed", "not_pending")]
    assert evs[0]["op"] == "nope-123"  # op = the requested spool id
    assert evs[1]["detail"]["state"] == "sending"


# --------------------------------------------------------------------- #
# store family — emit only on actual change                             #
# --------------------------------------------------------------------- #


def test_mailbox_create_emits_only_when_created(monkeypatch, audit_dir_guard):
    monkeypatch.setattr(server, "_SOURCE", object())
    shapes = iter([
        {"ok": True, "account": "ACC", "path": "Archive/X", "existed": False,
         "applescript": "OK", "index_verified": True, "mail_verified": True,
         "warning": None},
        {"ok": True, "account": "ACC", "path": "Archive/X", "existed": True,
         "applescript": None, "index_verified": True, "mail_verified": True,
         "warning": None},
    ])
    monkeypatch.setattr(server, "create_mailbox",
                        lambda src, account, path: next(shapes))
    assert server.tool_mailbox_create(account="ACC", path="Archive/X")["ok"]
    assert server.tool_mailbox_create(account="ACC", path="Archive/X")["ok"]

    def _unknown(src, account, path):
        raise TriageError("unknown_account", "no such account")

    monkeypatch.setattr(server, "create_mailbox", _unknown)
    assert server.tool_mailbox_create(account="NOPE", path="X")["ok"] is False

    evs = [e for e in _events(audit_dir_guard)
           if e["event"] == "mailbox_create"]
    assert len(evs) == 1  # created once; idempotent + refused emit NOTHING
    assert evs[0]["outcome"] == "created"
    assert evs[0]["account"] == "ACC" and evs[0]["mailbox"] == "Archive/X"


def test_mailbox_delete_emits_only_when_issued(monkeypatch, audit_dir_guard):
    monkeypatch.setattr(server, "_SOURCE", object())
    base = {"account": "ACC", "path": "Old", "mail_verified": True}
    shapes = iter([
        {"ok": True, "existed": False, "deleted": False, "warning": None,
         **base},                                     # absent → no event
        {"ok": True, "existed": True, "deleted": True,
         "method": "applescript", "warning": None, **base},
        {"ok": False, "existed": True, "deleted": False,
         "code": "accessibility_denied", "error": "needs Accessibility",
         **base},
        {"ok": False, "existed": True, "deleted": False,
         "code": "delete_failed", "error": "still there", **base},
    ])
    monkeypatch.setattr(server, "delete_mailbox",
                        lambda src, account, path: next(shapes))
    for _ in range(4):
        server.tool_mailbox_delete(account="ACC", path="Old")

    evs = [e for e in _events(audit_dir_guard)
           if e["event"] == "mailbox_delete"]
    assert [e["outcome"] for e in evs] == \
        ["deleted", "accessibility_denied", "delete_failed"]
    assert evs[0]["detail"] == {"method": "applescript"}
    assert all(e["mailbox"] == "Old" for e in evs)


# --------------------------------------------------------------------- #
# plan lifecycle — library level                                        #
# --------------------------------------------------------------------- #


def test_build_plan_emits_plan_create(
    send_env, mail_fixture, audit_dir_guard
):
    from email_mcp.sources.apple_mail import AppleMailSource
    from email_mcp.sources.base import SearchQuery

    src = AppleMailSource(mail_base=mail_fixture)
    plan = triage.build_plan(src, SearchQuery(unread_only=True),
                             [{"action": "mark_read"}])

    ev, = [e for e in _events(audit_dir_guard)
           if e["event"] == "plan_create"]
    assert ev["outcome"] == "created"
    assert ev["op"] == plan.id == ev["plan_id"]  # artifact-id rule
    assert ev["summary"] == plan.summary
    assert ev["detail"]["count"] == len(plan.messages) == 1
    assert ev["detail"]["actions"] == [{"action": "mark_read"}]
    assert "target" not in ev["detail"]  # no move_to → no target


def _mini_plan(summary: str = "mark_read: 2 msg(s)") -> plans.Plan:
    now = plans.utcnow()
    return plans.Plan(
        id=plans.new_id(now), created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(minutes=10)), status="draft",
        query={}, actions=[PlanAction(action="mark_read")], target=None,
        messages=[], summary=summary,
    )


def test_plan_finish_emits_apply_outcomes_with_compact_detail(
    send_env, audit_dir_guard
):
    applied = _mini_plan()
    plans.finish(applied, "applied", {
        "ok": True, "plan_id": applied.id, "status": "applied",
        "planned": 2, "acted": 2, "verified": 1,
        "failures": [{"id": "101", "code": "applescript",
                      "detail": "boom (-1728) huge prose"}],
        "pending": [{"id": "100", "expected": {"read": 1},
                     "observed": {"read": 0}}],
        "osascript_ms": 160, "duration_ms": 1200,
    })
    failed = _mini_plan("the failing plan")
    plans.finish(failed, "failed",
                 {"error": "accounts not in Mail: ['X']"})

    evs = [e for e in _events(audit_dir_guard)
           if e["event"] == "plan_finish"]
    assert [(e["outcome"], e["op"], e["plan_id"]) for e in evs] == \
        [("applied", applied.id, applied.id),
         ("failed", failed.id, failed.id)]
    # compact extraction: counts + {id, code} + pending ids — no prose dumps
    assert evs[0]["detail"] == {
        "planned": 2, "acted": 2, "verified": 1,
        "failures": [{"id": "101", "code": "applescript"}],
        "pending": ["100"],
    }
    assert evs[0]["summary"] == applied.summary  # outlives plan GC
    assert evs[1]["detail"] == {"error": "accounts not in Mail: ['X']"}


def test_plan_finish_covers_expiry_and_gc_stale_claims(
    send_env, audit_dir_guard
):
    expired = _mini_plan("expired plan")
    plans.save(expired)
    plans.expire(plans.claim(expired.id))

    stale = _mini_plan("stale-claim plan")
    plans.save(stale)
    assert plans.claim(stale.id) is not None  # leaves <id>.json.applying
    claim_path = config.plans_dir() / f"{stale.id}.json.applying"
    old = time.time() - 3 * config.triage_ttl_seconds()  # > 2x TTL stale
    os.utime(claim_path, (old, old))
    plans.gc()

    evs = [e for e in _events(audit_dir_guard)
           if e["event"] == "plan_finish"]
    assert [(e["outcome"], e["plan_id"]) for e in evs] == \
        [("expired", expired.id), ("failed", stale.id)]
    assert "detail" not in evs[0]  # expiry carries no result
    assert evs[1]["detail"] == {
        "error": "stale claim: apply crashed mid-flight"}
    assert not claim_path.exists()


# --------------------------------------------------------------------- #
# dispatcher — library level                                            #
# --------------------------------------------------------------------- #


def test_dispatcher_emits_deliver_and_recover(
    send_env, delivered, audit_dir_guard, monkeypatch
):
    monkeypatch.setenv("EMAIL_MCP_SEND_RETRIES", "2")

    # c1: sent — and the deliver event THREADS with the server's schedule
    # event through the shared op (the spool id).
    ok = server.tool_schedule_email(to="paris@example.org", subject="ok",
                                    body="b", send_at=_iso_in(-1))
    # c2': the frozen .eml vanished — bypasses the retry funnel.
    gone = sender.schedule_email(to="paris@example.org", subject="gone",
                                 body="b", send_at=_iso_in(-1))
    (Path(config.spool_dir()) / "pending" / f"{gone.id}.eml").unlink()
    summary = dispatcher.run_once()
    assert summary["results"][ok["id"]] == "sent"
    assert summary["results"][gone.id] == "failed"

    # c2: the ONE _fail_or_retry funnel — retry, then park in failed/.
    retry_e = sender.schedule_email(to="paris@example.org", subject="retry",
                                    body="b", send_at=_iso_in(-1))

    def _down(raw):
        raise sender.SendError("transport down")

    monkeypatch.setattr(sender, "_deliver_bytes", _down)
    assert dispatcher.run_once()["results"][retry_e.id] == "retry in 2m"
    e = spool.load("pending", retry_e.id)
    e.next_attempt_at = spool.iso(spool.utcnow() - timedelta(minutes=1))
    spool.update("pending", e)
    assert dispatcher.run_once()["results"][retry_e.id] == "failed"

    # c3: stranded claims — requeued, then (attempts exhausted) failed.
    stranded = sender.schedule_email(to="paris@example.org",
                                     subject="stranded", body="b",
                                     send_at=_iso_in(-1))
    assert spool.claim(stranded.id)
    dispatcher._recover_stranded(spool.utcnow() + timedelta(minutes=20))
    assert spool.claim(stranded.id)
    dispatcher._recover_stranded(spool.utcnow() + timedelta(minutes=40))

    evs = _events(audit_dir_guard)
    ok_evs = [e for e in evs if e.get("spool_id") == ok["id"]]
    assert [(e["event"], e["outcome"]) for e in ok_evs] == \
        [("schedule", "scheduled"), ("deliver", "sent")]
    assert {e["op"] for e in ok_evs} == {ok["id"]}  # cross-process thread
    assert ok_evs[1]["message_id"] == ok["message_id"]

    gone_ev, = [e for e in evs if e.get("spool_id") == gone.id]
    assert (gone_ev["event"], gone_ev["outcome"]) == ("deliver", "failed")
    assert gone_ev["detail"] == {"code": "spool_eml_missing"}

    retry_evs = [e for e in evs if e.get("spool_id") == retry_e.id]
    assert [(e["event"], e["outcome"], e["detail"]["attempts"])
            for e in retry_evs] == [("deliver", "retry", 1),
                                    ("deliver", "failed", 2)]
    assert all(e["detail"]["error"] == "transport down" for e in retry_evs)

    rec_evs = [e for e in evs if e.get("spool_id") == stranded.id
               and e["event"] == "recover"]
    assert [(e["outcome"], e["detail"]["attempts"]) for e in rec_evs] == \
        [("requeued", 1), ("failed", 2)]


def _graph_entry(subject: str, draft_id: str | None = None) -> spool.Entry:
    now = spool.utcnow()
    e = spool.Entry(
        id=spool.new_id(now), send_at=spool.iso(now),
        created_at=spool.iso(now), to=["paris@example.org"], cc=[], bcc=[],
        subject=subject, attachments=[], message_id=f"<{subject}@x>",
        executor="graph", graph_draft_id=draft_id,
    )
    spool.save(b"raw", e)
    return e


def test_graph_reconcile_emits_lifecycle_events(send_env, audit_dir_guard):
    now = spool.utcnow()

    adopt = _graph_entry("adopt")
    assert dispatcher._graph_adopt(adopt, "D1") == \
        "graph: adopted existing draft"

    flip = _graph_entry("flip", draft_id="D2")
    assert dispatcher._graph_flip_to_launchd(
        flip, now, "draft revoked", clear_draft=True) == \
        "graph: draft revoked — local delivery next pass"
    after = spool.load("pending", flip.id)
    assert after.executor == "launchd" and after.graph_draft_id is None

    sent = _graph_entry("gsent")
    assert dispatcher._graph_mark_sent(sent, now) == \
        "sent (delivered by Exchange)"
    assert spool.load("sent", sent.id) is not None

    cxl = _graph_entry("gcxl")
    assert "OWA" in dispatcher._graph_apply_status(
        cxl, "cancelled_externally", now)
    assert spool.load("cancelled", cxl.id) is not None

    # frozen decision: _graph_leave emits NOTHING (per-tick spam guard)
    left = _graph_entry("left")
    dispatcher._graph_leave(left, "unreachable", "note")

    evs = _events(audit_dir_guard)
    by_spool = {e["spool_id"]: (e["event"], e["outcome"]) for e in evs}
    assert by_spool[adopt.id] == ("graph_adopt", "adopted")
    assert by_spool[flip.id] == ("graph_flip", "flipped")
    assert by_spool[sent.id] == ("graph_sent", "sent")
    assert by_spool[cxl.id] == ("graph_cancelled_external", "cancelled")
    assert left.id not in by_spool
    adopt_ev, = [e for e in evs if e["spool_id"] == adopt.id]
    assert adopt_ev["draft_id"] == "D1" and adopt_ev["op"] == adopt.id
    flip_ev, = [e for e in evs if e["spool_id"] == flip.id]
    assert flip_ev["detail"] == {"reason": "draft revoked"}


# --------------------------------------------------------------------- #
# doctor — the ledger check                                             #
# --------------------------------------------------------------------- #


def test_doctor_audit_check_reports_and_gates_top_level_ok(
    send_env, audit_dir_guard, monkeypatch
):
    from email_mcp import audit, doctor

    check = doctor.check_audit()
    assert check["ok"] is True
    assert "no events yet" in check["detail"]
    assert check["last_event"] is None

    assert audit.emit("send", outcome="sent", subject="s") is not None
    check = doctor.check_audit()
    assert check["ok"] is True
    assert "1 monthly file(s)" in check["detail"]
    assert check["last_event"] is not None
    # side-effect free: the probe wrote no event of its own
    assert len(_events(audit_dir_guard)) == 1

    # run() carries it as a top-level sibling of `checks` — the v0.9
    # checks membership stays frozen — and folds it into overall ok.
    monkeypatch.setattr(doctor, "_CHECKS", ())  # keep osascript etc. out
    report = doctor.run()
    assert report["checks"] == {}  # no tenth member
    assert report["audit"]["ok"] is True and report["ok"] is True

    audit_dir_guard.chmod(0o755)  # events carry recipients/subjects
    try:
        report = doctor.run()
    finally:
        audit_dir_guard.chmod(0o700)
    assert report["audit"]["ok"] is False
    assert "chmod 700" in report["audit"]["fix"]
    assert report["ok"] is False  # a broken ledger is a red doctor
