"""create_draft — intent handed back, never executed (docs/draft-design.md,
panel decision 2026-08-02).

The policy table: the declared-lane refusal (never a fallback), the
three-legged acceptance readback, the founding-bug assertion on OUR bytes
(non-empty text/plain, no cite-blockquote wrapper), the no-allowlist rule
(nothing transmits), the audit record, and the no-transport invariant
proven over the call graph rather than asserted in prose.
"""
from __future__ import annotations

import ast
import base64
import email
import email.policy
import inspect
import json
import time

import pytest

from email_mcp import codes, graph, sender, server, state
from email_mcp.identities import Identity


@pytest.fixture(autouse=True)
def _draft_env(monkeypatch, tmp_path):
    import os

    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    ident_file = tmp_path / "identities.toml"
    ident_file.write_text(
        'default = "cern"\n\n[cern]\n'
        'from_addr = "camilla@example.org"\n'
        'driver = "pipe"\ncommand = "cat"\n'
        # Guard ENGAGED on purpose: the no-allowlist-jurisdiction row
        # below is only a real pin if a send to the same stranger would
        # actually be blocked.
        "allow_all = false\n"
        'drafts = "graph"\nexecutor = "graph"\n\n'
        "[cern.graph]\n"
        'tenant = "organizations"\nclient_id = "app-123"\n\n'
        "[nodrafts]\n"
        'from_addr = "other@example.org"\n'
        'driver = "pipe"\ncommand = "cat"\n'
    )
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(ident_file))
    # A bound token cache so _token never needs the network.
    path = state.State.resolve().adopt().graph / "cern.token.json"
    path.write_text(json.dumps({
        "access_token": "acc", "refresh_token": "ref",
        "expires_at": time.time() + 3600,
        "bound_to": "camilla@example.org",
    }))
    path.chmod(0o600)


class _FakeGraph:
    """Stateful Graph double behind the graph._http seam: captures the
    POSTed MIME and answers the readback from it, like Exchange would."""

    def __init__(self, *, is_draft=True, right_folder=True, right_mid=True):
        self.posted_mime: bytes | None = None
        self.is_draft = is_draft
        self.right_folder = right_folder
        self.right_mid = right_mid
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, ident, body=None, headers=None):
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/me/messages"):
            self.posted_mime = base64.b64decode(body)
            return 201, {"id": "GRAPH-DRAFT-1"}
        if method == "GET" and "/me/messages/" in url:
            msg = email.message_from_bytes(self.posted_mime,
                                           policy=email.policy.default)
            mid = msg["Message-ID"] if self.right_mid else "<wrong@x>"
            return 200, {"isDraft": self.is_draft,
                         "internetMessageId": mid,
                         "parentFolderId": "FOLDER-DRAFTS"}
        if method == "GET" and url.endswith("/me/mailFolders/drafts"):
            return 200, {"id": "FOLDER-DRAFTS" if self.right_folder
                         else "FOLDER-OTHER"}
        raise AssertionError(f"unexpected Graph call {method} {url}")


def test_refusal_without_a_declared_lane_names_the_enable_steps(monkeypatch):
    """No lane, no draft, NO fallback — and the error is the documentation
    (the enable command, not a doc pointer alone)."""
    fake = _FakeGraph()
    monkeypatch.setattr(graph, "_http", fake)
    out = server.tool_create_draft(to="a@b.org", subject="s", body="b",
                                   from_identity="nodrafts")
    assert out["ok"] is False
    assert out["code"] == codes.DRAFT_UNSUPPORTED
    assert "email-mcp setup" in out["error"]
    assert fake.calls == []  # refused before any network


def test_draft_filed_verified_and_faithful(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(graph, "_http", fake)
    out = server.tool_create_draft(
        to="stranger@example.org", subject="Zcharm meeting",
        body="Dear all,\n\nWe skip Monday.\n\nCheers,\nCamilla",
    )
    assert out["ok"] is True
    assert out["draft_id"] == "GRAPH-DRAFT-1"
    assert out["folder"] == "drafts"
    assert out["account"] == "camilla@example.org"

    msg = email.message_from_bytes(fake.posted_mime,
                                   policy=email.policy.default)
    assert out["message_id"] == msg["Message-ID"]
    # The founding-bug assertion, on OUR lane: the stored bytes are the
    # composer's — non-empty plain part, no cite-blockquote, no Apple
    # wrapper classes (Lane A failed exactly these three).
    plain = msg.get_body(("plain",)).get_content()
    assert "We skip Monday." in plain
    html = msg.get_body(("html",)).get_content()
    assert "We skip Monday." in html
    assert "<blockquote" not in html
    assert "Apple-Mail-URLShare" not in html
    # A human will edit and send this: no Bcc-to-self on a draft.
    assert msg["Bcc"] is None
    # Draft parts are BASE64, never quoted-printable: OWA's editor
    # mangles QP soft breaks in MIME-created drafts ("sent" → "se=t",
    # measured 2026-08-02, first acceptance run).
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            assert part["Content-Transfer-Encoding"] == "base64"


def test_declared_guard_blocks_the_send_but_not_the_draft(monkeypatch):
    """Nothing transmits, so the send guard has no jurisdiction over
    drafts. The fixture identity DECLARES allow_all = false, and the pin
    proves it has teeth: the same stranger is blocked on send_email and
    accepted on create_draft, in the same environment."""
    fake = _FakeGraph()
    monkeypatch.setattr(graph, "_http", fake)
    blocked = server.tool_send_email(to="stranger@example.org",
                                     subject="s", body="b")
    assert blocked["ok"] is False
    assert blocked["code"] == "recipient_not_allowed"  # guard is LIVE
    out = server.tool_create_draft(to="stranger@example.org",
                                   subject="s", body="b")
    assert out["ok"] is True


@pytest.mark.parametrize("breakage,leg", [
    (dict(is_draft=False), "is_draft=False"),
    (dict(right_mid=False), "message_id_match=False"),
    (dict(right_folder=False), "in_drafts_folder=False"),
])
def test_readback_verification_failure_names_the_leg(monkeypatch,
                                                     breakage, leg):
    """Evidence, never assertion: any missing leg of the three-part
    readback is a failure that says exactly which leg."""
    fake = _FakeGraph(**breakage)
    monkeypatch.setattr(graph, "_http", fake)
    out = server.tool_create_draft(to="a@b.org", subject="s", body="b")
    assert out["ok"] is False
    assert leg in out["error"]


def test_draft_audit_records_composition_on_both_terminals(monkeypatch):
    from email_mcp import audit, config

    events = []
    monkeypatch.setattr(audit, "emit",
                        lambda ev, **kw: events.append((ev, kw)) or "op")
    fake = _FakeGraph()
    monkeypatch.setattr(graph, "_http", fake)
    ok = server.tool_create_draft(to="a@b.org", subject="s", body="b")
    bad = server.tool_create_draft(to="a@b.org", subject="s", body="b",
                                   from_identity="nodrafts")
    assert ok["ok"] is True and bad["ok"] is False
    assert [(e, k["outcome"]) for e, k in events] == \
        [("draft", "created"), ("draft", "failed")]
    created = events[0][1]
    assert created["detail"]["draft_id"] == "GRAPH-DRAFT-1"
    assert created["to"] == ["a@b.org"]  # the ledger sees recipients


# --------------------------------------------------------------------- #
# the invariant, proven over the call graph                              #
# --------------------------------------------------------------------- #
# "A draft created by this tool cannot leave the machine by this tool's
# hand." grep cannot pin this (graph.py legitimately contains /send), so
# walk the calls reachable from the draft path inside email_mcp and
# assert no transmission primitive is among them.

_FORBIDDEN_CALLS = {
    "create_deferred_draft",   # the armed sibling
    "deliver", "_deliver", "_deliver_bytes", "deliver_for",
    "get_transport", "preflight", "send_email", "schedule_email",
}


def _called_names(fn) -> set[str]:
    tree = ast.parse(inspect.getsource(fn).lstrip())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute)
                      else getattr(f, "id", ""))
    return names


def _reachable_calls(fn) -> set[str]:
    """Every name CALLED anywhere reachable from `fn` within email_mcp —
    a transitive walk, not a hand-picked inventory: a new intermediate
    helper is discovered, not trusted. Name resolution deliberately
    over-approximates (a called name is looked up in every email_mcp
    module), which can only make this test stricter, never blinder."""
    import email_mcp
    from email_mcp import (audit, config, envelope, identities,  # noqa: F401
                           spool)
    modules = [server, sender, graph, identities, envelope, audit,
               config, spool]
    seen_fns: set = set()
    called: set[str] = set()

    def walk(f):
        f = getattr(f, "__wrapped__", f)
        if f in seen_fns or getattr(f, "__module__", "").split(".")[0] \
                != "email_mcp":
            return
        seen_fns.add(f)
        try:
            names = _called_names(f)
        except (OSError, TypeError):
            return
        called.update(names)
        for name in names:
            for mod in modules:
                cand = getattr(mod, name, None)
                if inspect.isfunction(cand):
                    walk(cand)

    walk(fn)
    return called


def test_no_transmission_primitive_reachable_from_create_draft():
    """The panel's invariant pin: an AST call-graph REACHABILITY check
    from the tool function down — refactoring the draft path through a
    helper that reaches a transmission primitive fails here, whatever
    the helper is named."""
    reached = _reachable_calls(server.tool_create_draft)
    # The walk must genuinely descend: the leaf Graph calls only appear
    # via sender.create_draft — their presence proves transitivity.
    assert "create_mime_draft" in reached and "draft_receipt" in reached
    assert not (reached & _FORBIDDEN_CALLS), reached & _FORBIDDEN_CALLS


def test_create_path_never_arms_the_draft():
    """The request bodies of the create path carry no deferred-send
    property and no /send URL — the fences that keep a user draft from
    ever becoming a timed transmission."""
    src = inspect.getsource(graph.create_mime_draft) + \
        inspect.getsource(graph.draft_receipt)
    assert "singleValueExtendedProperties" not in src
    assert "0x3FEF" not in src and "3FEF" not in src
    assert "/send" not in src
