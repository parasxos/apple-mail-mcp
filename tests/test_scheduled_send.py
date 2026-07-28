"""Scheduled-send tests: spool round-trip, freeze semantics, due filtering,
atomic claim, retry/backoff/park, cancel, allowlist recheck at fire time.
Transport is always mocked; nothing leaves the machine."""
from __future__ import annotations

import email
import email.policy
import json
from datetime import datetime, timedelta, timezone

import pytest

from email_mcp import dispatcher, sender, spool


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Documented defaults + an isolated spool per test."""
    for k in list(__import__("os").environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris.moschovakos@cern.ch")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Paris Moschovakos")
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml"))


@pytest.fixture
def delivered(monkeypatch):
    """Mock the byte-level transport; record what would have been sent."""
    sent: list[bytes] = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", lambda raw: sent.append(raw))
    monkeypatch.setattr(dispatcher, "_notify", lambda *a, **k: None)
    return sent


def _future(minutes: float = -1.0) -> str:
    """ISO send_at `minutes` from now (negative = already due)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------- #
# scheduling                                                            #
# --------------------------------------------------------------------- #


def test_schedule_freezes_message_with_attachment(tmp_path, delivered):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 frozen")
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(-1), attachments=[str(f)],
    )
    assert entry.status == "pending" and entry.attachments == ["doc.pdf"]
    f.unlink()  # source vanishes AFTER scheduling — must not matter

    raw = spool.read_eml("pending", entry.id)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    att = list(msg.iter_attachments())[0]
    assert att.get_content() == b"%PDF-1.4 frozen"       # frozen bytes
    assert "paris.moschovakos@cern.ch" in msg["Bcc"]     # bcc-self baked in
    assert msg["Message-ID"] == entry.message_id

    summary = dispatcher.run_once()
    assert summary["results"][entry.id] == "sent"
    wire = email.message_from_bytes(delivered[0], policy=email.policy.default)
    assert list(wire.iter_attachments())[0].get_content() == b"%PDF-1.4 frozen"


def test_schedule_naive_send_at_means_local_time():
    naive = (datetime.now() + timedelta(hours=2)).replace(microsecond=0)
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=naive.isoformat(),
    )
    stored = datetime.fromisoformat(entry.send_at)
    expect = naive.astimezone(timezone.utc)
    assert abs((stored - expect).total_seconds()) < 1


def test_schedule_rejects_past():
    with pytest.raises(sender.SendError) as ei:
        sender.schedule_email(
            to="paris.moschovakos@cern.ch", subject="s", body="b",
            send_at=_future(-10),
        )
    assert "past" in str(ei.value)


def test_schedule_enforces_allowlist_at_schedule_time():
    with pytest.raises(sender.SendError):
        sender.schedule_email(
            to="stranger@example.org", subject="s", body="b",
            send_at=_future(5),
        )
    assert spool.entries("pending") == []


# --------------------------------------------------------------------- #
# dispatch                                                              #
# --------------------------------------------------------------------- #


def test_dispatcher_sends_due_skips_future(delivered):
    due = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="due", body="b",
        send_at=_future(-1),
    )
    future = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="future", body="b",
        send_at=_future(60),
    )
    summary = dispatcher.run_once()
    assert summary["results"] == {due.id: "sent"}
    assert len(delivered) == 1
    assert spool.load("sent", due.id).delivered_at
    assert spool.load("pending", future.id).status == "pending"


def test_dispatcher_idle_pass_is_quiet(delivered):
    assert dispatcher.run_once()["due"] == 0
    assert delivered == []


def test_retry_backoff_then_park_failed(monkeypatch, delivered):
    monkeypatch.setenv("EMAIL_MCP_SEND_RETRIES", "2")
    notes = []
    monkeypatch.setattr(dispatcher, "_notify", lambda t, x: notes.append(t))

    def boom(raw):
        raise sender.SendError("smtp exploded")
    monkeypatch.setattr(sender, "_deliver_bytes", boom)

    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(-1),
    )
    # attempt 1 → back to pending with backoff
    assert dispatcher.run_once()["results"][entry.id] == "retry in 2m"
    e = spool.load("pending", entry.id)
    assert e.attempts == 1 and "exploded" in e.last_error
    # not due again until backoff elapses
    assert dispatcher.run_once()["results"] == {}
    # force the clock past the backoff → attempt 2 → park in failed/
    later = spool.utcnow() + timedelta(minutes=3)
    assert dispatcher.run_once(now=later)["results"][entry.id] == "failed"
    assert spool.load("failed", entry.id).attempts == 2
    assert notes  # user was notified of the final failure


def test_stranded_sending_recovers_then_delivers(delivered):
    """A crashed dispatcher leaves its claim in sending/; the next pass
    (after the staleness window) must recover and deliver it."""
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(-1),
    )
    assert spool.claim(entry.id)  # simulate: claimed, then the process died
    assert dispatcher.run_once()["results"] == {}  # too fresh — left alone
    later = spool.utcnow() + timedelta(minutes=dispatcher.STALE_SENDING_MINUTES + 1)
    summary = dispatcher.run_once(now=later)
    assert summary["results"][entry.id] == "sent"
    got = spool.load("sent", entry.id)
    # attempts carries the retry history; last_error clears on success so a
    # delivered entry never reads like a failure.
    assert got.attempts == 1 and got.last_error is None


def test_claim_race_single_winner(delivered):
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(-1),
    )
    assert spool.claim(entry.id) is True
    assert spool.claim(entry.id) is False  # second claim loses


def test_authorized_at_schedule_time_fires_under_bare_env(monkeypatch, delivered):
    """The dispatcher runs under launchd's bare env (guard defaults ON).
    A message authorized at schedule time must still go out — the allowlist
    is enforced when scheduling, never re-checked at fire time."""
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    entry = sender.schedule_email(
        to="colleague@cern.ch", subject="s", body="b", send_at=_future(-1),
    )
    monkeypatch.delenv("EMAIL_MCP_SEND_ALLOW_ALL")  # launchd-like bare env
    assert dispatcher.run_once()["results"][entry.id] == "sent"
    assert len(delivered) == 1


# --------------------------------------------------------------------- #
# cancel + listing (server-layer)                                       #
# --------------------------------------------------------------------- #


def test_cancel_pending_and_refuse_sent(delivered):
    from email_mcp import server

    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(60),
    )
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is True and spool.load("cancelled", entry.id)
    # cancelled messages never send
    assert dispatcher.run_once(now=spool.utcnow() + timedelta(hours=2))["due"] == 0

    sent = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s2", body="b",
        send_at=_future(-1),
    )
    dispatcher.run_once()
    res = server.tool_cancel_scheduled(sent.id)
    assert res["ok"] is False and "sent" in res["error"]

    assert server.tool_cancel_scheduled("nope-123")["ok"] is False


def test_list_scheduled_shape(delivered):
    from email_mcp import server

    sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="pending one", body="b",
        send_at=_future(60),
    )
    res = server.tool_list_scheduled()
    assert res["ok"] is True
    assert len(res["pending"]) == 1
    assert res["pending"][0]["subject"] == "pending one"
    assert set(spool.STATES) <= set(res)
    assert server.tool_list_scheduled(state="bogus")["ok"] is False


def test_spool_manifest_is_valid_json_on_disk(tmp_path):
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(30),
    )
    from email_mcp import config
    manifest = config.spool_dir() / "pending" / f"{entry.id}.json"
    data = json.loads(manifest.read_text())
    assert data["id"] == entry.id and data["status"] == "pending"


# --------------------------------------------------------------------- #
# identities at fire time                                               #
# --------------------------------------------------------------------- #


def test_manifest_carries_identity_and_old_manifests_still_load():
    entry = sender.schedule_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        send_at=_future(60),
    )
    assert entry.identity == "default"
    from email_mcp import config
    manifest = config.spool_dir() / "pending" / f"{entry.id}.json"
    data = json.loads(manifest.read_text())
    assert data["identity"] == "default"
    # pre-0.7.0 manifest: no identity field → must load as the legacy default
    del data["identity"]
    manifest.write_text(json.dumps(data))
    loaded = spool.load("pending", entry.id)
    assert loaded is not None and loaded.identity == "default"


def test_fire_time_envelope_uses_frozen_from_not_current_env(monkeypatch):
    """The latent-bug fix: the envelope sender at fire time is the message's
    frozen From: header, not whatever mutable config resolves to by then."""
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "frozen@example.org")
    entry = sender.schedule_email(
        to="frozen@example.org", subject="s", body="b", send_at=_future(-1),
    )
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "drifted@example.org")

    seen: list = []

    class FakeTransport:
        name = "fake"
        last_ensure_error = None

        def ensure(self):
            return True

        def deliver(self, raw, mail_from, rcpt_to):
            seen.append((raw, mail_from, rcpt_to))

        def healthcheck(self):
            return {"ok": True}

    monkeypatch.setattr(
        sender.transports, "get_transport", lambda ident: FakeTransport())
    monkeypatch.setattr(dispatcher, "_notify", lambda *a, **k: None)
    assert dispatcher.run_once()["results"][entry.id] == "sent"
    raw, mail_from, rcpt_to = seen[-1]
    assert mail_from == "frozen@example.org"      # frozen, not drifted
    assert "frozen@example.org" in rcpt_to
    wire = email.message_from_bytes(raw, policy=email.policy.default)
    assert "frozen@example.org" in wire["From"]


def test_dispatcher_ensures_transport_once_per_identity(monkeypatch, delivered):
    """Two due messages riding one identity → exactly one ensure per run."""
    checks: list = []
    monkeypatch.setattr(
        sender, "_socket_alive", lambda: checks.append(1) or True)
    for i in (1, 2):
        sender.schedule_email(
            to="paris.moschovakos@cern.ch", subject=f"s{i}", body="b",
            send_at=_future(-1),
        )
    summary = dispatcher.run_once()
    assert summary["due"] == 2
    assert set(summary["results"].values()) == {"sent"}
    assert len(delivered) == 2
    assert len(checks) == 1  # memoised: one preflight for both messages
