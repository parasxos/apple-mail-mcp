"""Graph executor tests — token cache hygiene (F14), device login,
create_deferred_draft (payloads, ordering, no-orphan cleanup), throttle
policy (F7), draft_status's four verdicts with Sent-Items disambiguation
(F9), message-id lookups, delete semantics (F4 precursor), and the W2
integration surface: schedule-time two-phase manifest write + fallback
(F5/F8), the dispatcher's graph reconcile (F1-F4, F6, F9, F12),
cancel_scheduled's revoke-first branch (F10), and the doctor graph check.

Every HTTP interaction goes through a monkeypatched `graph._http` — the
module's single wire seam. No test touches urllib; nothing leaves the
machine.
"""
from __future__ import annotations

import base64
import email
import email.policy
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from email_mcp import config, dispatcher, graph, sender, server, spool, state
from email_mcp.graph import GraphError
from email_mcp.identities import Identity
from email_mcp.transports import SendError


@pytest.fixture(autouse=True)
def _graph_env(monkeypatch, tmp_path):
    """State tree (token caches + spool) in tmp, identities pointed at a
    nonexistent file, and no env leakage — nothing outside tmp_path is
    touched."""
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml")
    )
    return tmp_path


@pytest.fixture
def sleeps(monkeypatch):
    """Record (and skip) every time.sleep the module asks for."""
    rec: list = []
    monkeypatch.setattr(graph.time, "sleep", rec.append)
    return rec


class FakeHttp:
    """Scripted stand-in for graph._http: pops one canned (status, dict)
    response — or raises one canned exception — per call, recording
    (method, url, body, headers) for assertions."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple] = []

    def __call__(self, method, url, ident, body=None, headers=None):
        self.calls.append((method, url, body, dict(headers or {})))
        if not self.script:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self):
        return [u for _m, u, _b, _h in self.calls]


def _fake(monkeypatch, *script) -> FakeHttp:
    fake = FakeHttp(*script)
    monkeypatch.setattr(graph, "_http", fake)
    return fake


def _ident(name="cern", executor="graph"):
    return Identity(
        name=name,
        from_addr="someone@example.org",
        executor=executor,
        graph={"tenant": "example.org", "client_id": "app-123"},
    )


def _seed_token(name="cern", access="acc-token", refresh="ref-token",
                expires_in=3600.0, bound_to="someone@example.org"):
    path = state.State.resolve().adopt().graph / f"{name}.token.json"
    path.write_text(json.dumps({
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expires_in,
        "scope": "Mail.ReadWrite Mail.Send",
        # Matches _ident().from_addr — the binding every login since
        # 2026-08-02 proves via GET /me. bound_to="" models a stale
        # pre-binding cache.
        "bound_to": bound_to,
    }))
    path.chmod(0o600)
    return path


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------- #
# token cache: perms, atomic rewrite, refresh, fix strings (F14, F5/F6)  #
# --------------------------------------------------------------------- #


def test_token_missing_cache_names_login_fix_and_makes_no_http_call(
    monkeypatch,
):
    fake = _fake(monkeypatch)  # any HTTP call would blow up
    with pytest.raises(GraphError) as ei:
        graph._token(_ident())
    assert "python -m email_mcp.graph --login cern" in str(ei.value)
    assert fake.calls == []


def test_token_valid_cache_served_without_http(monkeypatch):
    _seed_token()
    fake = _fake(monkeypatch)
    assert graph._token(_ident()) == "acc-token"
    assert fake.calls == []


def test_token_refresh_rewrites_cache_atomically_0600(monkeypatch):
    """Expired access token → silent refresh through the seam, rotated
    refresh token persisted, file back to 0600, no .tmp leftovers."""
    path = _seed_token(expires_in=-10)
    fake = _fake(monkeypatch, (200, {
        "access_token": "acc-2", "refresh_token": "ref-2",
        "expires_in": 3599, "scope": "Mail.ReadWrite Mail.Send",
    }))
    assert graph._token(_ident()) == "acc-2"

    method, url, body, headers = fake.calls[0]
    assert method == "POST"
    assert url == f"{graph.LOGIN}/example.org/oauth2/v2.0/token"
    form = dict(
        p.split("=", 1) for p in body.decode().split("&")
    )
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "ref-token"
    assert form["client_id"] == "app-123"

    cache = json.loads(path.read_text())
    assert cache["access_token"] == "acc-2"
    assert cache["refresh_token"] == "ref-2"  # rotation honored
    assert cache["expires_at"] > time.time()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert list(path.parent.glob("*.tmp")) == []  # tmp+rename left no debris


def test_token_refresh_keeps_old_refresh_token_when_none_returned(
    monkeypatch,
):
    path = _seed_token(expires_in=-10)
    _fake(monkeypatch, (200, {"access_token": "acc-2", "expires_in": 3599}))
    assert graph._token(_ident()) == "acc-2"
    assert json.loads(path.read_text())["refresh_token"] == "ref-token"


def test_token_refresh_failure_names_login_fix_with_aadsts(monkeypatch):
    """F5/F6 precursor: a refused refresh is ONE GraphError carrying the
    AADSTS reason verbatim plus the interactive remedy."""
    _seed_token(expires_in=-10)
    _fake(monkeypatch, (400, {
        "error": "invalid_grant",
        "error_description": "AADSTS700082: refresh token expired",
    }))
    with pytest.raises(GraphError) as ei:
        graph._token(_ident())
    s = str(ei.value)
    assert "AADSTS700082" in s
    assert "python -m email_mcp.graph --login cern" in s


def test_graph_error_is_a_send_error():
    assert issubclass(GraphError, SendError)


# --------------------------------------------------------------------- #
# device login (interactive): polling contract + cache hygiene (F14)     #
# --------------------------------------------------------------------- #


def test_device_login_polls_writes_0600_cache_in_0700_dir(
    monkeypatch, sleeps, capsys,
):
    _fake(
        monkeypatch,
        (200, {"device_code": "DC", "user_code": "ABCD-1234",
               "verification_uri": "https://microsoft.com/devicelogin",
               "interval": 1, "expires_in": 900,
               "message": "Visit https://microsoft.com/devicelogin and "
                          "enter ABCD-1234"}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "acc-new", "refresh_token": "ref-new",
               "expires_in": 3599, "scope": "Mail.ReadWrite Mail.Send"}),
        (200, {"mail": "someone@example.org"}),  # GET /me binding proof
    )
    path = graph.device_login(_ident())

    assert "ABCD-1234" in capsys.readouterr().err  # code shown to the human
    assert sleeps == [1, 1, 6]  # interval honored; slow_down added 5s
    cache = json.loads(path.read_text())
    assert cache["access_token"] == "acc-new"
    assert cache["refresh_token"] == "ref-new"
    assert cache["bound_to"] == "someone@example.org"
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700


def test_device_login_refuses_wrong_mailbox_and_writes_no_cache(
    monkeypatch, sleeps,
):
    """The binding fence at its strongest point: the human is present.
    Signing in as somebody else's mailbox must refuse BEFORE a cache
    exists — a token for the wrong account never becomes state (panel
    finding 2026-08-02: the wrong-account draft is an egress bug)."""
    _fake(
        monkeypatch,
        (200, {"device_code": "DC", "user_code": "X", "interval": 1,
               "expires_in": 900, "verification_uri": "u"}),
        (200, {"access_token": "acc-new", "refresh_token": "ref-new",
               "expires_in": 3599}),
        (200, {"mail": "somebody.else@example.org"}),
    )
    with pytest.raises(GraphError) as ei:
        graph.device_login(_ident())
    s = str(ei.value)
    assert "somebody.else@example.org" in s
    assert "someone@example.org" in s
    graph_dir = state.State.resolve().adopt().graph
    assert list(graph_dir.glob("*.token.json")) == []  # nothing cached


def test_token_refuses_unbound_cache(monkeypatch):
    """A cache without a binding (pre-2026-08 login) is refused with the
    login fix — never trusted on faith."""
    _seed_token(bound_to="")
    fake = _fake(monkeypatch)  # any HTTP call would blow up
    with pytest.raises(GraphError) as ei:
        graph._token(_ident())
    assert "no mailbox binding" in str(ei.value)
    assert "python -m email_mcp.graph --login cern" in str(ei.value)
    assert fake.calls == []  # pure refusal, no network


def test_token_refuses_binding_identity_mismatch(monkeypatch):
    """identities.toml edited to a different from_addr over an old cache:
    the token is for the OLD mailbox — refuse, naming both addresses."""
    _seed_token(bound_to="old.mailbox@example.org")
    fake = _fake(monkeypatch)
    with pytest.raises(GraphError) as ei:
        graph._token(_ident())
    s = str(ei.value)
    assert "old.mailbox@example.org" in s and "someone@example.org" in s
    assert fake.calls == []


def test_token_refresh_preserves_the_binding(monkeypatch):
    path = _seed_token(expires_in=-10)
    _fake(monkeypatch, (200, {"access_token": "acc-2", "expires_in": 3599}))
    assert graph._token(_ident()) == "acc-2"
    assert json.loads(path.read_text())["bound_to"] == "someone@example.org"


def test_device_login_refusal_passes_aadsts_verbatim(monkeypatch):
    _fake(monkeypatch, (400, {
        "error": "invalid_client",
        "error_description": "AADSTS7000218: client not allowed",
    }))
    with pytest.raises(GraphError) as ei:
        graph.device_login(_ident())
    assert "AADSTS7000218" in str(ei.value)


def test_device_login_without_graph_config_names_identities_file():
    bare = Identity(name="cern", from_addr="someone@example.org")
    with pytest.raises(GraphError) as ei:
        graph.device_login(bare)
    s = str(ei.value)
    assert "tenant" in s and "client_id" in s and "[cern.graph]" in s


# --------------------------------------------------------------------- #
# create_deferred_draft: payloads, ordering, no-orphan cleanup           #
# --------------------------------------------------------------------- #

RAW = b"Message-ID: <m1@example.org>\r\nSubject: hi\r\n\r\nbody\r\n"
WHEN = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)


def test_create_deferred_draft_happy_path_payloads_and_order(monkeypatch):
    _seed_token()
    fake = _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        (202, {}),
    )
    assert graph.create_deferred_draft(_ident(), RAW, WHEN) == "D1"

    create, patch, send = fake.calls
    # 1) MIME create: base64 of the FROZEN bytes, text/plain, bearer auth.
    assert create[0] == "POST"
    assert create[1] == f"{graph.GRAPH}/me/messages"
    assert create[2] == base64.b64encode(RAW)
    assert create[3]["Content-Type"] == "text/plain"
    assert create[3]["Authorization"] == "Bearer acc-token"
    # 2) PATCH PidTagDeferredSendTime, ISO-8601 UTC.
    assert patch[0] == "PATCH"
    assert patch[1] == f"{graph.GRAPH}/me/messages/D1"
    assert json.loads(patch[2]) == {
        "singleValueExtendedProperties": [
            {"id": "SystemTime 0x3FEF", "value": "2026-08-01T09:30:00Z"},
        ]
    }
    # 3) /send arms it — strictly last.
    assert send[0] == "POST"
    assert send[1] == f"{graph.GRAPH}/me/messages/D1/send"


def test_create_deferred_draft_naive_datetime_treated_as_utc(monkeypatch):
    _seed_token()
    fake = _fake(monkeypatch, (201, {"id": "D1"}), (200, {}), (202, {}))
    graph.create_deferred_draft(
        _ident(), RAW, datetime(2026, 8, 1, 9, 30)
    )
    patched = json.loads(fake.calls[1][2])
    assert patched["singleValueExtendedProperties"][0]["value"] == (
        "2026-08-01T09:30:00Z"
    )


def test_create_failure_before_id_raises_without_cleanup(monkeypatch):
    """F8: a 5xx on create raises GraphError (caller falls back to
    launchd); nothing was created so nothing is deleted."""
    _seed_token()
    fake = _fake(monkeypatch, (503, {"error": {
        "code": "ErrorServerBusy", "message": "try later"}}))
    with pytest.raises(GraphError) as ei:
        graph.create_deferred_draft(_ident(), RAW, WHEN)
    assert "503" in str(ei.value)
    assert len(fake.calls) == 1


def test_create_patch_failure_deletes_the_created_draft(monkeypatch):
    """No orphan armed drafts: PATCH refusal → best-effort DELETE of the
    draft we just created, then the original error propagates."""
    _seed_token()
    fake = _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (400, {"error": {"code": "ErrorInvalidProperty", "message": "no"}}),
        (204, {}),
    )
    with pytest.raises(GraphError) as ei:
        graph.create_deferred_draft(_ident(), RAW, WHEN)
    assert "ErrorInvalidProperty" in str(ei.value)
    assert fake.calls[-1][0] == "DELETE"
    assert fake.calls[-1][1] == f"{graph.GRAPH}/me/messages/D1"


def test_create_send_failure_deletes_the_created_draft(monkeypatch):
    _seed_token()
    fake = _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        (403, {"error": {"code": "ErrorAccessDenied", "message": "no"}}),
        (204, {}),
    )
    with pytest.raises(GraphError) as ei:
        graph.create_deferred_draft(_ident(), RAW, WHEN)
    assert "/send rejected" in str(ei.value)
    assert fake.calls[-1][0] == "DELETE"


def test_create_cleanup_failure_still_raises_the_original_error(
    monkeypatch,
):
    _seed_token()
    _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (400, {"error": {"code": "ErrorInvalidProperty", "message": "no"}}),
        GraphError("[cern/graph] network error reaching graph"),  # DELETE
    )
    with pytest.raises(GraphError) as ei:
        graph.create_deferred_draft(_ident(), RAW, WHEN)
    assert "ErrorInvalidProperty" in str(ei.value)  # original error wins


# --------------------------------------------------------------------- #
# throttling: Retry-After honored, bounded, never a busy-loop (F7)       #
# --------------------------------------------------------------------- #


def test_429_retry_after_honored_with_single_bounded_wait(
    monkeypatch, sleeps,
):
    _seed_token()
    fake = _fake(
        monkeypatch,
        (429, {"retry_after": 3}),
        (200, {"value": []}),
    )
    assert graph.sent_by_message_id(_ident(), "<m1@example.org>") is False
    assert sleeps == [3]
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == fake.calls[1][1]  # same request retried


def test_429_long_retry_after_defers_without_sleeping(monkeypatch, sleeps):
    _seed_token()
    fake = _fake(monkeypatch, (429, {"retry_after": 120}))
    with pytest.raises(GraphError) as ei:
        graph.sent_by_message_id(_ident(), "<m1@example.org>")
    assert "429" in str(ei.value)
    assert sleeps == []          # no in-process wait beyond the bound
    assert len(fake.calls) == 1  # deferred to the next pass instead


def test_429_persisting_after_one_retry_defers(monkeypatch, sleeps):
    _seed_token()
    fake = _fake(
        monkeypatch,
        (429, {"retry_after": 1}),
        (429, {"retry_after": 1}),
    )
    with pytest.raises(GraphError):
        graph.sent_by_message_id(_ident(), "<m1@example.org>")
    assert sleeps == [1]         # exactly one wait — never a busy-loop
    assert len(fake.calls) == 2


# --------------------------------------------------------------------- #
# draft_status: all four verdicts; 'sent' only on positive evidence (F9) #
# --------------------------------------------------------------------- #


def test_draft_status_held(monkeypatch):
    _seed_token()
    fake = _fake(monkeypatch, (200, {"id": "D1", "isDraft": True}))
    assert graph.draft_status(_ident(), "D1", "<m1@example.org>") == "held"
    assert len(fake.calls) == 1


def test_draft_status_sent_needs_sentitems_hit(monkeypatch):
    _seed_token()
    fake = _fake(
        monkeypatch,
        (404, {}),
        (200, {"value": [{"id": "S1"}]}),
    )
    assert graph.draft_status(_ident(), "D1", "<m1@example.org>") == "sent"
    # the disambiguation really asked Sent Items, filtered by Message-ID
    assert "/me/mailFolders/sentitems/messages" in fake.calls[1][1]
    assert "internetMessageId" in fake.calls[1][1]


def test_draft_status_cancelled_externally_on_confirmed_sent_miss(
    monkeypatch,
):
    _seed_token()
    _fake(monkeypatch, (404, {}), (200, {"value": []}))
    assert graph.draft_status(
        _ident(), "D1", "<m1@example.org>"
    ) == "cancelled_externally"


def test_draft_status_unknown_when_sent_lookup_fails(monkeypatch):
    """The CRITICAL fence: a vanished draft whose Sent Items lookup FAILS
    must read 'unknown' (leave + retry) — never 'sent', never
    'cancelled_externally'."""
    _seed_token()
    _fake(monkeypatch, (404, {}), (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    assert graph.draft_status(
        _ident(), "D1", "<m1@example.org>"
    ) == "unknown"


def test_draft_status_unknown_on_odd_probe_status(monkeypatch):
    _seed_token()
    _fake(monkeypatch, (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    assert graph.draft_status(
        _ident(), "D1", "<m1@example.org>"
    ) == "unknown"


def test_draft_status_no_longer_a_draft_disambiguates_via_sentitems(
    monkeypatch,
):
    _seed_token()
    _fake(
        monkeypatch,
        (200, {"id": "D1", "isDraft": False}),
        (200, {"value": [{"id": "S1"}]}),
    )
    assert graph.draft_status(_ident(), "D1", "<m1@example.org>") == "sent"


# --------------------------------------------------------------------- #
# message-id lookups (the two-phase-manifest recovery key)               #
# --------------------------------------------------------------------- #


def test_find_draft_by_message_id_found(monkeypatch):
    _seed_token()
    fake = _fake(monkeypatch, (200, {"value": [{"id": "D9"}]}))
    got = graph.find_draft_by_message_id(_ident(), "<m1@example.org>")
    assert got == "D9"
    url = fake.calls[0][1]
    assert "/me/mailFolders/drafts/messages" in url
    assert "internetMessageId" in url
    assert "m1%40example.org" in url  # the Message-ID rode along, encoded


def test_find_draft_by_message_id_confirmed_absent_is_none(monkeypatch):
    _seed_token()
    _fake(monkeypatch, (200, {"value": []}))
    assert graph.find_draft_by_message_id(
        _ident(), "<m1@example.org>"
    ) is None


def test_lookup_failure_raises_instead_of_reading_as_absence(monkeypatch):
    """A 403 on the folder query must raise — absence of evidence is not
    evidence of absence, or F9/F10 disambiguation would guess."""
    _seed_token()
    _fake(monkeypatch, (403, {"error": {
        "code": "ErrorAccessDenied", "message": "no"}}))
    with pytest.raises(GraphError):
        graph.find_draft_by_message_id(_ident(), "<m1@example.org>")


def test_sent_by_message_id_true_and_false(monkeypatch):
    _seed_token()
    _fake(
        monkeypatch,
        (200, {"value": [{"id": "S1"}]}),
        (200, {"value": []}),
    )
    assert graph.sent_by_message_id(_ident(), "<m1@example.org>") is True
    assert graph.sent_by_message_id(_ident(), "<m1@example.org>") is False


# --------------------------------------------------------------------- #
# delete_draft: only a CONFIRMED delete releases Exchange's claim (F4)   #
# --------------------------------------------------------------------- #


def test_delete_draft_deleted_and_gone(monkeypatch):
    _seed_token()
    _fake(monkeypatch, (204, {}), (404, {}))
    assert graph.delete_draft(_ident(), "D1") == "deleted"
    assert graph.delete_draft(_ident(), "D1") == "gone"


def test_delete_draft_server_error_raises(monkeypatch):
    _seed_token()
    _fake(monkeypatch, (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    with pytest.raises(GraphError):
        graph.delete_draft(_ident(), "D1")


def test_delete_draft_network_error_propagates(monkeypatch):
    """F4: a transport failure surfaces as GraphError so the caller keeps
    the entry on graph, untouched, for the next pass."""
    _seed_token()
    _fake(monkeypatch, GraphError("[cern/graph] network error"))
    with pytest.raises(GraphError):
        graph.delete_draft(_ident(), "D1")


# --------------------------------------------------------------------- #
# CLI: --status is local-only; unknown identities exit nonzero           #
# --------------------------------------------------------------------- #


def test_cli_status_unknown_identity_exits_nonzero_without_network(
    monkeypatch, capsys,
):
    fake = _fake(monkeypatch)  # any HTTP call would blow up
    rc = graph.main(["--status", "nonexistent"])
    assert rc != 0
    assert fake.calls == []
    assert "error:" in capsys.readouterr().err


def _write_graph_toml(tmp_path, monkeypatch):
    p = tmp_path / "identities.toml"
    p.write_text(
        'default = "cern"\n\n'
        "[cern]\n"
        'from_addr = "someone@example.org"\n'
        'driver = "ssh_sendmail"\n'
        'executor = "graph"\n'
        'host = "mailhost.example.org"\n'
        'user = "someone"\n'
        'socket = "/tmp/sock"\n\n'
        "[cern.graph]\n"
        'tenant = "example.org"\n'
        'client_id = "app-123"\n'
    )
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))


def test_cli_status_logged_in_reports_cache_and_exits_zero(
    monkeypatch, tmp_path, capsys,
):
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    fake = _fake(monkeypatch)  # still no network on --status
    rc = graph.main(["--status", "cern"])
    assert rc == 0
    assert fake.calls == []
    info = json.loads(capsys.readouterr().out)
    assert info["identity"] == "cern"
    assert info["executor"] == "graph"
    assert info["has_refresh_token"] is True
    assert info["access_token_valid"] is True


def test_cli_status_without_cache_exits_one(monkeypatch, tmp_path, capsys):
    _write_graph_toml(tmp_path, monkeypatch)
    rc = graph.main(["--status", "cern"])
    assert rc == 1
    info = json.loads(capsys.readouterr().out)
    assert info["has_refresh_token"] is False


# ===================================================================== #
# W2 integration: schedule fork, dispatcher reconcile, cancel, doctor    #
# ===================================================================== #


def _future(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _spool_entry(minutes_past_grace: float = 5.0, draft_id: str | None = "D1",
                 identity: str = "cern",
                 message_id: str = "<m1@example.org>") -> spool.Entry:
    """Fabricate a pending graph-executor entry whose send_at is
    `minutes_past_grace` minutes beyond the reconcile grace window
    (negative = still inside Exchange's window)."""
    now = spool.utcnow()
    entry = spool.Entry(
        id=spool.new_id(now),
        send_at=spool.iso(now - timedelta(
            minutes=dispatcher.GRAPH_GRACE_MINUTES + minutes_past_grace)),
        created_at=spool.iso(now - timedelta(hours=1)),
        to=["someone@example.org"], cc=[], bcc=[],
        subject="deferred", attachments=[],
        message_id=message_id,
        identity=identity,
        executor="graph",
        graph_draft_id=draft_id,
    )
    spool.save(RAW, entry)
    return entry


@pytest.fixture
def local_delivery(monkeypatch):
    """Mock the local transport seams; record frozen bytes handed to it.
    Any append here from a graph test is a double-send alarm."""
    delivered: list[bytes] = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", delivered.append)
    monkeypatch.setattr(dispatcher, "_notify", lambda *a, **k: None)
    return delivered


# --------------------------------------------------------------------- #
# schedule-time fork: two-phase manifest write + silent fallback         #
# --------------------------------------------------------------------- #


def test_schedule_graph_two_phase_manifest_write(monkeypatch, tmp_path):
    """Amendment A: manifest hits pending/ (executor=graph, no draft id)
    BEFORE the first byte goes to Graph; the id lands via update after."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    inner = FakeHttp((201, {"id": "D1"}), (200, {}), (202, {}))
    manifests_during_create: list[dict] = []

    def spy(method, url, ident, body=None, headers=None):
        if not inner.calls:  # first wire call = the MIME draft create
            manifests_during_create.extend(
                json.loads(p.read_text())
                for p in (config.spool_dir() / "pending").glob("*.json"))
        return inner(method, url, ident, body=body, headers=headers)

    monkeypatch.setattr(graph, "_http", spy)
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body="b",
        send_at=_future(30), from_identity="cern",
    )
    assert manifests_during_create, "Graph was called before the manifest hit disk"
    assert manifests_during_create[0]["executor"] == "graph"
    assert manifests_during_create[0]["graph_draft_id"] is None
    assert manifests_during_create[0]["message_id"] == entry.message_id

    assert entry.executor == "graph" and entry.graph_draft_id == "D1"
    stored = spool.load("pending", entry.id)
    assert stored.executor == "graph" and stored.graph_draft_id == "D1"
    raw = spool.read_eml("pending", entry.id)
    assert entry.message_id.encode() in raw       # frozen recovery key
    assert inner.calls[0][2] == base64.b64encode(raw)  # Exchange got the SAME bytes


def test_schedule_graph_freezes_base64_text_parts(monkeypatch, tmp_path):
    """The deferred-send sibling of the drafts pin: Exchange's MIME
    importer garbles QP soft breaks ("prepared" → "prepar=d", measured
    2026-08-03 on a deferred-draft readback), so the graph lane freezes
    text parts as base64 and the body round-trips verbatim."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    _fake(monkeypatch, (201, {"id": "D1"}), (200, {}), (202, {}))
    body = ("Two quick questions before tomorrow: would it help if I "
            "prepared the calibration summary in advance? "
            "SNOW Ref=INC4471902 stays open until then — à demain.")
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body=body,
        send_at=_future(30), from_identity="cern",
    )
    assert entry.executor == "graph"
    wire = email.message_from_bytes(spool.read_eml("pending", entry.id),
                                    policy=email.policy.default)
    for part in wire.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            assert part["Content-Transfer-Encoding"] == "base64"
    assert body in wire.get_body(("plain",)).get_content()


def test_schedule_graph_error_falls_back_to_launchd(monkeypatch, tmp_path):
    """F5/F8: token refusal or 5xx at schedule time → entry flips to the
    launchd executor, frozen .eml intact, no exception to the caller."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    _fake(monkeypatch, (503, {"error": {
        "code": "ErrorServerBusy", "message": "try later"}}))
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body="b",
        send_at=_future(30), from_identity="cern",
    )
    assert entry.executor == "launchd" and entry.graph_draft_id is None
    stored = spool.load("pending", entry.id)
    assert stored.executor == "launchd"
    assert spool.read_eml("pending", entry.id)  # frozen .eml in EVERY path


def test_schedule_graph_token_missing_falls_back_without_http(
    monkeypatch, tmp_path,
):
    """F5: no token cache → GraphError before any wire call → launchd."""
    _write_graph_toml(tmp_path, monkeypatch)
    fake = _fake(monkeypatch)  # any HTTP call would blow up
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body="b",
        send_at=_future(30), from_identity="cern",
    )
    assert entry.executor == "launchd"
    assert fake.calls == []


# --------------------------------------------------------------------- #
# dispatcher reconcile: crash-window recovery (F1/F2)                    #
# --------------------------------------------------------------------- #


def test_reconcile_f1_no_draft_anywhere_flips_to_launchd_then_delivers(
    monkeypatch, tmp_path, local_delivery,
):
    """F1: crash between manifest-save and draft-create. Drafts AND Sent
    Items confirmed empty → flip to launchd; the SAME frozen .eml goes out
    locally on the NEXT pass — exactly once."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(draft_id=None)
    fake = _fake(
        monkeypatch,
        (200, {"value": []}),   # drafts: confirmed absent
        (200, {"value": []}),   # sentitems: confirmed absent
    )
    summary = dispatcher.run_once()
    assert "launchd" in summary["results"][entry.id] or \
           "local" in summary["results"][entry.id]
    assert "/me/mailFolders/drafts/" in fake.urls[0]
    assert "/me/mailFolders/sentitems/" in fake.urls[1]
    got = spool.load("pending", entry.id)
    assert got.executor == "launchd" and got.graph_draft_id is None
    assert local_delivery == []          # flip pass delivers NOTHING

    summary2 = dispatcher.run_once()     # next pass: local path takes over
    assert summary2["results"][entry.id] == "sent"
    assert local_delivery == [RAW]       # the same frozen bytes, once


def test_reconcile_f1_no_draft_but_sent_items_hit_moves_to_sent(
    monkeypatch, tmp_path, local_delivery,
):
    """F1 disambiguation: the draft is gone from Drafts because Exchange
    already transmitted it — never deliver locally on top."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(draft_id=None)
    _fake(monkeypatch, (200, {"value": []}), (200, {"value": [{"id": "S1"}]}))
    summary = dispatcher.run_once()
    assert "Exchange" in summary["results"][entry.id]
    got = spool.load("sent", entry.id)
    assert got is not None and got.delivered_at and got.last_error is None
    assert local_delivery == []


def test_reconcile_f2_adopts_orphan_draft_by_message_id(
    monkeypatch, tmp_path, local_delivery,
):
    """F2: crash after draft-create, before manifest-update. The Drafts
    lookup by the frozen Message-ID adopts the orphan — Exchange keeps the
    job; nothing is armed twice, nothing delivers locally."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(draft_id=None)
    fake = _fake(monkeypatch, (200, {"value": [{"id": "D9"}]}))
    summary = dispatcher.run_once()
    assert "adopted" in summary["results"][entry.id]
    got = spool.load("pending", entry.id)
    assert got.executor == "graph" and got.graph_draft_id == "D9"
    assert len(fake.calls) == 1
    assert local_delivery == []


def test_reconcile_f1_lookup_failure_leaves_entry_untouched(
    monkeypatch, tmp_path, local_delivery,
):
    """Recovery lookups failing must NOT read as absence: entry stays on
    graph with last_error, retried next pass."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(draft_id=None)
    _fake(monkeypatch, (403, {"error": {
        "code": "ErrorAccessDenied", "message": "no"}}))
    dispatcher.run_once()
    got = spool.load("pending", entry.id)
    assert got.executor == "graph" and got.graph_draft_id is None
    assert got.last_error and "drafts lookup" in got.last_error
    assert local_delivery == []


# --------------------------------------------------------------------- #
# dispatcher reconcile: held / sent / cancelled / ambiguity (F3/F4/F9)   #
# --------------------------------------------------------------------- #


def test_reconcile_pre_grace_makes_no_http_calls(monkeypatch, tmp_path):
    """Inside send_at + grace the entry is Exchange's business — the
    reconcile pass asks nothing and touches nothing."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-5)  # still in Exchange's window
    fake = _fake(monkeypatch)
    summary = dispatcher.run_once()
    assert fake.calls == []
    assert entry.id not in summary["results"]
    assert spool.load("pending", entry.id).executor == "graph"


def test_reconcile_f3_held_past_grace_deletes_draft_before_flipping(
    monkeypatch, tmp_path, local_delivery,
):
    """F3: Exchange is late and still holds the draft → DELETE first; only
    the CONFIRMED delete flips the entry to launchd (delivery next pass)."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    fake = _fake(
        monkeypatch,
        (200, {"id": "D1", "isDraft": True}),  # status probe: held
        (204, {}),                             # DELETE: confirmed
    )
    summary = dispatcher.run_once()
    assert fake.calls[1][0] == "DELETE"
    assert fake.calls[1][1] == f"{graph.GRAPH}/me/messages/D1"
    assert "revoked" in summary["results"][entry.id]
    got = spool.load("pending", entry.id)
    assert got.executor == "launchd" and got.graph_draft_id is None
    assert got.next_attempt_at is not None
    assert local_delivery == []              # never in the same pass

    assert dispatcher.run_once()["results"][entry.id] == "sent"
    assert local_delivery == [RAW]


def test_reconcile_f3_delete_gone_redisambiguates_to_sent(
    monkeypatch, tmp_path, local_delivery,
):
    """F3 race: DELETE returns 404 — Exchange won. The re-run status
    disambiguation finds the Sent Items hit; NO local delivery."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(
        monkeypatch,
        (200, {"id": "D1", "isDraft": True}),  # held
        (404, {}),                             # DELETE → gone
        (404, {}),                             # re-probe: vanished
        (200, {"value": [{"id": "S1"}]}),      # Sent Items: Exchange sent it
    )
    summary = dispatcher.run_once()
    assert "Exchange" in summary["results"][entry.id]
    got = spool.load("sent", entry.id)
    assert got is not None and got.delivered_at
    assert local_delivery == []


def test_reconcile_f3_delete_gone_ambiguous_never_delivers_locally(
    monkeypatch, tmp_path, local_delivery,
):
    """F3 critical fence: draft gone + Sent Items unreachable = ambiguity.
    The entry stays on graph, is NEVER flipped, NEVER delivered locally."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(
        monkeypatch,
        (200, {"id": "D1", "isDraft": True}),  # held
        (404, {}),                             # DELETE → gone
        (404, {}),                             # re-probe: vanished
        (503, {"error": {"code": "ErrorServerBusy", "message": "later"}}),
    )
    dispatcher.run_once()
    got = spool.load("pending", entry.id)
    assert got is not None and got.executor == "graph"
    assert got.graph_draft_id == "D1"
    assert local_delivery == []


def test_reconcile_f4_delete_network_error_leaves_entry_on_graph(
    monkeypatch, tmp_path, local_delivery,
):
    """F4: revoke times out → no confirmed delete → the entry keeps its
    executor and draft id, gains last_error, and is retried next pass."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(
        monkeypatch,
        (200, {"id": "D1", "isDraft": True}),
        GraphError("[cern/graph] network error reaching graph.microsoft.com"),
    )
    summary = dispatcher.run_once()
    assert "retrying" in summary["results"][entry.id]
    got = spool.load("pending", entry.id)
    assert got.executor == "graph" and got.graph_draft_id == "D1"
    assert "network error" in got.last_error
    assert local_delivery == []


def test_reconcile_f6_token_refresh_failure_records_fix_and_leaves_entry(
    monkeypatch, tmp_path, local_delivery,
):
    """F6: refresh refused at reconcile time → entry untouched on graph,
    last_error carries the AADSTS reason AND the --login fix."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token(expires_in=-10)  # force the refresh path
    entry = _spool_entry()
    _fake(monkeypatch, (400, {
        "error": "invalid_grant",
        "error_description": "AADSTS700082: refresh token expired",
    }))
    dispatcher.run_once()
    got = spool.load("pending", entry.id)
    assert got.executor == "graph" and got.graph_draft_id == "D1"
    assert "AADSTS700082" in got.last_error
    assert "--login cern" in got.last_error
    assert local_delivery == []


def test_reconcile_f9_sent_moves_to_sent_with_delivered_at(
    monkeypatch, tmp_path, local_delivery,
):
    """F9 reconcile-level: vanished draft + Sent Items hit = delivered by
    Exchange → sent/, delivered_at set, last_error cleared."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(monkeypatch, (404, {}), (200, {"value": [{"id": "S1"}]}))
    summary = dispatcher.run_once()
    assert summary["results"][entry.id] == "sent (delivered by Exchange)"
    got = spool.load("sent", entry.id)
    assert got.delivered_at and got.last_error is None
    assert got.status == "sent"
    assert local_delivery == []


def test_reconcile_f9_cancelled_externally_names_owa(
    monkeypatch, tmp_path, local_delivery,
):
    """F9: vanished draft + CONFIRMED Sent Items miss = someone discarded
    it (e.g. OWA) → cancelled/, note naming Outlook/OWA, no local send."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(monkeypatch, (404, {}), (200, {"value": []}))
    summary = dispatcher.run_once()
    assert "OWA" in summary["results"][entry.id]
    got = spool.load("cancelled", entry.id)
    assert got is not None and "OWA" in got.last_error
    assert local_delivery == []


def test_reconcile_f9_unknown_leaves_entry_and_never_guesses(
    monkeypatch, tmp_path, local_delivery,
):
    """F9 critical fence at reconcile level: ambiguity (Sent Items lookup
    failing) leaves the entry pending on graph — guessing 'sent' would
    drop mail, guessing 'cancelled' would double-send."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    _fake(monkeypatch, (404, {}), (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    dispatcher.run_once()
    got = spool.load("pending", entry.id)
    assert got is not None and got.executor == "graph"
    assert local_delivery == []


# --------------------------------------------------------------------- #
# dispatcher reconcile: identity drift (F12)                             #
# --------------------------------------------------------------------- #


def test_reconcile_f12_unknown_identity_retries_and_pass_continues(
    monkeypatch, tmp_path, local_delivery,
):
    """F12: identity removed from the TOML between schedule and fire —
    that entry fails-with-backoff naming the identity; the OTHER graph
    entry in the same pass still reconciles normally."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    ghost = _spool_entry(identity="ghost", draft_id="DG",
                         message_id="<ghost@example.org>")
    fine = _spool_entry(draft_id="D2", message_id="<m2@example.org>")
    _fake(monkeypatch, (404, {}), (200, {"value": [{"id": "S1"}]}))
    summary = dispatcher.run_once()

    assert summary["results"][ghost.id].startswith("retry")
    got = spool.load("pending", ghost.id)
    assert got.attempts == 1 and got.executor == "graph"
    assert "ghost" in got.last_error          # error names the identity
    assert got.next_attempt_at is not None

    assert "Exchange" in summary["results"][fine.id]  # pass continued
    assert spool.load("sent", fine.id) is not None
    assert local_delivery == []

    # backoff respected: an immediate second pass makes no HTTP call and
    # leaves the ghost entry alone (its script is already exhausted).
    assert dispatcher.run_once()["results"] == {}


# --------------------------------------------------------------------- #
# cancel_scheduled: revoke-first + the F10 race                          #
# --------------------------------------------------------------------- #


def test_cancel_graph_revokes_draft_then_cancels(monkeypatch, tmp_path):
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70)  # send_at ~1h ahead
    fake = _fake(monkeypatch, (204, {}))
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is True and res["status"] == "cancelled"
    assert fake.calls[0][0] == "DELETE"
    assert spool.load("cancelled", entry.id) is not None
    assert spool.load("pending", entry.id) is None


def test_cancel_graph_revoke_failure_keeps_entry_pending_names_owa(
    monkeypatch, tmp_path,
):
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70)
    _fake(monkeypatch, (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is False
    assert "OWA" in res["error"]              # remediation names OWA
    got = spool.load("pending", entry.id)     # entry stays pending on graph
    assert got is not None and got.executor == "graph"


def test_cancel_graph_f10_race_already_sent_moves_to_sent(
    monkeypatch, tmp_path,
):
    """F10: DELETE finds the draft gone and Sent Items has it — Exchange
    won the race. {ok:false} names the outcome; the entry lands in sent/."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70)
    _fake(monkeypatch, (404, {}), (200, {"value": [{"id": "S1"}]}))
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is False
    assert "already sent" in res["error"]
    got = spool.load("sent", entry.id)
    assert got is not None and got.delivered_at
    assert spool.load("pending", entry.id) is None


def test_cancel_graph_f10_gone_but_not_sent_treated_as_revoked(
    monkeypatch, tmp_path,
):
    """F10: draft gone AND Sent Items confirmed empty — nothing is armed;
    the cancel proceeds as a normal revoke."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70)
    _fake(monkeypatch, (404, {}), (200, {"value": []}))
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is True and res["status"] == "cancelled"
    assert spool.load("cancelled", entry.id) is not None


def test_cancel_graph_f10_sent_lookup_failure_is_ambiguous_no_action(
    monkeypatch, tmp_path,
):
    """Draft gone but Sent Items unreachable → refuse to guess: the entry
    stays pending and the error says to retry."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70)
    _fake(monkeypatch, (404, {}), (503, {"error": {
        "code": "ErrorServerBusy", "message": "later"}}))
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is False and "ambiguous" in res["error"]
    assert spool.load("pending", entry.id) is not None


def test_cancel_graph_unrecorded_draft_is_found_and_revoked(
    monkeypatch, tmp_path,
):
    """Crash-window entry (graph_draft_id=None): cancel recovers the draft
    by internetMessageId and revokes it before cancelling — otherwise the
    orphan draft would still fire at send_at."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(minutes_past_grace=-70, draft_id=None)
    fake = _fake(
        monkeypatch,
        (200, {"value": [{"id": "D9"}]}),  # drafts lookup by Message-ID
        (204, {}),                         # DELETE the recovered draft
    )
    res = server.tool_cancel_scheduled(entry.id)
    assert res["ok"] is True
    assert fake.calls[1][0] == "DELETE"
    assert "D9" in fake.calls[1][1]
    assert spool.load("cancelled", entry.id) is not None


# --------------------------------------------------------------------- #
# doctor: graph check red with fix when a graph identity cannot fire     #
# --------------------------------------------------------------------- #


def test_doctor_graph_check_red_without_token_cache_names_login_fix(
    monkeypatch, tmp_path,
):
    """F6 surface: a graph identity with no token cache is a red check
    with the --login fix — and statting must not create the graph dir."""
    from email_mcp import doctor

    _write_graph_toml(tmp_path, monkeypatch)
    check = doctor.check_graph()
    assert check["ok"] is False
    assert "python -m email_mcp.graph --login cern" in check["fix"]
    assert check["identities"]["cern"]["ok"] is False
    assert not (tmp_path / "graph").exists()  # stat, never create


def test_doctor_graph_check_green_with_refreshable_cache(
    monkeypatch, tmp_path,
):
    from email_mcp import doctor

    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    check = doctor.check_graph()
    assert check["ok"] is True
    ident_report = check["identities"]["cern"]
    assert ident_report["ok"] is True
    assert "age_days" in ident_report


def test_doctor_graph_check_soft_when_no_identity_opts_in(monkeypatch):
    from email_mcp import doctor

    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "someone@example.org")
    check = doctor.check_graph()
    assert check["ok"] is True
    assert "no identities use the graph executor" in check["detail"]


# --------------------------------------------------------------------- #
# red-team additions: escaping, corruption, ambiguity, races, secrets    #
# --------------------------------------------------------------------- #


def test_find_by_message_id_escapes_single_quotes(monkeypatch):
    """internetMessageId is the ONLY adoption key, and it must survive an
    apostrophe (legal in Message-IDs) without breaking the OData $filter —
    a mangled filter could match a WRONG draft or error out."""
    from urllib.parse import parse_qs, urlsplit

    _seed_token()
    fake = _fake(monkeypatch, (200, {"value": []}))
    assert graph.find_draft_by_message_id(
        _ident(), "<o'brien@example.org>"
    ) is None
    q = parse_qs(urlsplit(fake.calls[0][1]).query)
    assert q["$filter"] == ["internetMessageId eq '<o''brien@example.org>'"]
    assert q["$select"] == ["id"]


def test_token_cache_truncated_json_is_clear_error_not_traceback(
    monkeypatch,
):
    """A corrupted (truncated) token cache must surface as ONE GraphError
    naming the --login remedy — never a raw ValueError, never a wire call."""
    path = _seed_token()
    path.write_text('{"access_token": "acc-tok')  # truncated write
    fake = _fake(monkeypatch)
    with pytest.raises(GraphError) as ei:
        graph._token(_ident())
    assert "--login cern" in str(ei.value)
    assert fake.calls == []


def test_schedule_graph_leaf_broken_falls_back_to_launchd(
    monkeypatch, tmp_path,
):
    """A broken graph leaf (a file squatting on it — leaves are
    independent, so the spool still works) at schedule time must behave
    like any GraphError: silent launchd fallback, frozen .eml intact —
    not a traceback through the tool."""
    _write_graph_toml(tmp_path, monkeypatch)
    root = state.State.resolve().adopt().root
    (root / "graph").write_text("squatter")  # the leaf can never be a dir
    fake = _fake(monkeypatch)
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body="b",
        send_at=_future(30), from_identity="cern",
    )
    assert entry.executor == "launchd" and entry.graph_draft_id is None
    assert fake.calls == []
    assert spool.read_eml("pending", entry.id)


def test_token_path_refused_state_is_graph_error(monkeypatch):
    """A refused state resolution at token-cache access surfaces as a
    GraphError naming the cause — the seam schedule callers fall back
    on — never a raw OSError traceback."""
    monkeypatch.setattr(
        state.State, "resolve",
        staticmethod(lambda: state.Refused("state root refused (test)")),
    )
    with pytest.raises(GraphError) as ei:
        graph._token_path(_ident())
    assert "cannot create/open the graph state dir" in str(ei.value)
    assert "state root refused (test)" in str(ei.value)


def test_create_send_wire_death_delete_gone_treated_as_armed(monkeypatch):
    """Double-send fence at CREATE time: the wire dies during /send and
    the cleanup DELETE finds the draft GONE — the /send almost certainly
    won. Falling back to launchd would transmit twice, so the draft id is
    returned and the entry stays on graph for reconcile to confirm."""
    _seed_token()
    fake = _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        graph.GraphTransportError("[cern/graph] network error mid-/send"),
        (404, {}),  # cleanup DELETE: draft already left Drafts
    )
    assert graph.create_deferred_draft(_ident(), RAW, WHEN) == "D1"
    assert fake.calls[-1][0] == "DELETE"


def test_create_send_wire_death_delete_confirmed_falls_back(monkeypatch):
    """Same wire death, but the cleanup DELETE returns 204: the draft is
    CONFIRMED revoked, Exchange cannot send it — the safe launchd
    fallback (GraphError) is correct here."""
    _seed_token()
    _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        graph.GraphTransportError("[cern/graph] network error mid-/send"),
        (204, {}),
    )
    with pytest.raises(GraphError):
        graph.create_deferred_draft(_ident(), RAW, WHEN)


def test_create_send_wire_death_unconfirmed_cleanup_stays_armed(
    monkeypatch,
):
    """Wire death on /send AND on the cleanup DELETE: nothing is
    confirmed, an armed draft may exist — must NOT signal fallback."""
    _seed_token()
    _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        graph.GraphTransportError("[cern/graph] network error mid-/send"),
        graph.GraphTransportError("[cern/graph] network error on DELETE"),
    )
    assert graph.create_deferred_draft(_ident(), RAW, WHEN) == "D1"


def test_schedule_send_ambiguity_keeps_graph_and_never_double_sends(
    monkeypatch, tmp_path, local_delivery,
):
    """End-to-end H4: ambiguous /send at schedule time keeps the entry on
    the graph executor with the draft id recorded — the local dispatcher
    never picks it up, so no double-send is possible."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    _fake(
        monkeypatch,
        (201, {"id": "D1"}),
        (200, {}),
        graph.GraphTransportError("[cern/graph] network error mid-/send"),
        (404, {}),
    )
    entry = sender.schedule_email(
        to="someone@example.org", subject="s", body="b",
        send_at=_future(30), from_identity="cern",
    )
    assert entry.executor == "graph" and entry.graph_draft_id == "D1"
    stored = spool.load("pending", entry.id)
    assert stored.executor == "graph" and stored.graph_draft_id == "D1"
    dispatcher.run_once()  # pre-grace: no HTTP, no local pickup
    assert local_delivery == []


def test_reconcile_malformed_send_at_reconciles_now_not_never(
    monkeypatch, tmp_path, local_delivery,
):
    """A graph entry whose send_at stamp is corrupted must be reconciled
    NOW (the code's own promise) — not skipped forever, which would let
    a sent/held outcome go unrecorded for eternity."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    got = spool.load("pending", entry.id)
    got.send_at = "not-a-timestamp"
    spool.update("pending", got)
    fake = _fake(monkeypatch, (404, {}), (200, {"value": [{"id": "S1"}]}))
    summary = dispatcher.run_once()
    assert len(fake.calls) == 2  # it DID reconcile
    assert "Exchange" in summary["results"][entry.id]
    assert spool.load("sent", entry.id) is not None
    assert local_delivery == []


def test_reconcile_malformed_backoff_stamp_does_not_kill_the_pass(
    monkeypatch, tmp_path, local_delivery,
):
    """A corrupt next_attempt_at must read as 'no backoff', never raise
    out of run_once (which would halt ALL scheduled mail)."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    got = spool.load("pending", entry.id)
    got.next_attempt_at = "garbage"
    spool.update("pending", got)
    _fake(monkeypatch, (404, {}), (200, {"value": [{"id": "S1"}]}))
    summary = dispatcher.run_once()  # must not raise
    assert "Exchange" in summary["results"][entry.id]
    assert spool.load("sent", entry.id) is not None
    assert local_delivery == []


def test_reconcile_concurrent_flip_not_clobbered_by_terminal_move(
    monkeypatch, tmp_path, local_delivery,
):
    """Race fence: while OUR pass has its status probe in flight, ANOTHER
    pass revokes the draft and flips the entry to launchd. Our stale
    'cancelled_externally' verdict must NOT move the flipped entry to
    cancelled/ — that would silently drop the mail."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry()
    inner = FakeHttp((404, {}), (200, {"value": []}))

    def racing(method, url, ident, body=None, headers=None):
        if not inner.calls:  # first wire call: the other pass wins the flip
            other = spool.load("pending", entry.id)
            other.executor = "launchd"
            other.graph_draft_id = None
            other.next_attempt_at = spool.iso(spool.utcnow())
            spool.update("pending", other)
        return inner(method, url, ident, body=body, headers=headers)

    monkeypatch.setattr(graph, "_http", racing)
    summary = dispatcher.run_once()
    assert summary["results"][entry.id] == dispatcher._SUPERSEDED
    got = spool.load("pending", entry.id)
    assert got is not None and got.executor == "launchd"  # flip preserved
    assert spool.load("cancelled", entry.id) is None
    assert local_delivery == []


def test_reconcile_concurrent_adopt_not_clobbered_by_stale_leave(
    monkeypatch, tmp_path, local_delivery,
):
    """Race fence for _graph_leave: our drafts lookup fails, but another
    pass adopted the orphan draft mid-flight. Writing our stale entry
    (graph_draft_id=None) back would erase the adoption."""
    _write_graph_toml(tmp_path, monkeypatch)
    _seed_token()
    entry = _spool_entry(draft_id=None)

    calls = []

    def racing(method, url, ident, body=None, headers=None):
        calls.append(url)
        other = spool.load("pending", entry.id)
        other.graph_draft_id = "D9"  # the other pass adopts
        spool.update("pending", other)
        raise GraphError("[cern/graph] network error")

    monkeypatch.setattr(graph, "_http", racing)
    summary = dispatcher.run_once()
    assert summary["results"][entry.id] == dispatcher._SUPERSEDED
    got = spool.load("pending", entry.id)
    assert got.graph_draft_id == "D9"  # adoption preserved
    assert local_delivery == []


def test_no_token_material_ever_logged(monkeypatch, tmp_path):
    """No access token, refresh token, or Authorization value may ever
    reach the log — the rotating file outlives the tokens' secrecy."""
    import logging

    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("email_mcp")
    handler = Grab()
    logger.addHandler(handler)
    try:
        _write_graph_toml(tmp_path, monkeypatch)
        _seed_token(access="SEKRIT-ACCESS-1", refresh="SEKRIT-REFRESH-1",
                    expires_in=-10)  # force the refresh path
        entry = _spool_entry()
        _fake(
            monkeypatch,
            (200, {"access_token": "SEKRIT-ACCESS-2",
                   "refresh_token": "SEKRIT-REFRESH-2",
                   "expires_in": 3599}),
            (404, {}),
            (200, {"value": [{"id": "S1"}]}),
        )
        summary = dispatcher.run_once()
        assert "Exchange" in summary["results"][entry.id]  # full path ran
    finally:
        logger.removeHandler(handler)
    blob = "\n".join(records)
    for secret in ("SEKRIT-ACCESS-1", "SEKRIT-ACCESS-2",
                   "SEKRIT-REFRESH-1", "SEKRIT-REFRESH-2", "Bearer "):
        assert secret not in blob, f"log leaked {secret!r}"


def test_ssl_context_prefers_certifi_and_caches(monkeypatch):
    """The venv's framework Python has no root-CA bundle; graph must wire
    certifi when available (launchd-safe TLS) and build the context once."""
    from email_mcp import graph as g
    monkeypatch.setattr(g, "_SSL_CTX", None)
    ctx1 = g._ssl_context()
    ctx2 = g._ssl_context()
    assert ctx1 is ctx2  # cached
    import certifi, ssl as _ssl
    assert isinstance(ctx1, _ssl.SSLContext)
    # certifi present in this venv -> the context must have CA certs loaded
    assert ctx1.cert_store_stats()["x509_ca"] > 0
