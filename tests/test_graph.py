"""Graph executor tests — token cache hygiene (F14), device login,
create_deferred_draft (payloads, ordering, no-orphan cleanup), throttle
policy (F7), draft_status's four verdicts with Sent-Items disambiguation
(F9), message-id lookups, and delete semantics (F4 precursor).

Every HTTP interaction goes through a monkeypatched `graph._http` — the
module's single wire seam. No test touches urllib; nothing leaves the
machine.
"""
from __future__ import annotations

import base64
import json
import stat
import time
from datetime import datetime, timezone

import pytest

from email_mcp import config, graph
from email_mcp.graph import GraphError
from email_mcp.identities import Identity
from email_mcp.transports import SendError


@pytest.fixture(autouse=True)
def _graph_env(monkeypatch, tmp_path):
    """Token caches in tmp, identities pointed at a nonexistent file, and
    no env-synthesized identity — nothing outside tmp_path is touched."""
    monkeypatch.delenv("EMAIL_MCP_FROM_ADDR", raising=False)
    monkeypatch.setenv("EMAIL_MCP_GRAPH_DIR", str(tmp_path / "graph"))
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
                expires_in=3600.0):
    path = config.graph_dir() / f"{name}.token.json"
    path.write_text(json.dumps({
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expires_in,
        "scope": "Mail.ReadWrite Mail.Send",
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
    )
    path = graph.device_login(_ident())

    assert "ABCD-1234" in capsys.readouterr().err  # code shown to the human
    assert sleeps == [1, 1, 6]  # interval honored; slow_down added 5s
    cache = json.loads(path.read_text())
    assert cache["access_token"] == "acc-new"
    assert cache["refresh_token"] == "ref-new"
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700


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
