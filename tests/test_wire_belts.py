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

import pytest

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
    """A plan id that a plan really could carry threads to the ledger's op.

    The ids must be minted-SHAPED (`plans.new_id` is `ids.new_id`): §2 says
    operation_id is never minted *for* a failure, so the belt echoes an
    argument only when it looks like an id this package produced. The old
    synthetic "P-123" fixture passed while the belt reflected any argument
    at all, including a caller's 60 KB string.
    """
    from email_mcp import ids

    monkeypatch.setattr(server, "_SOURCE", object())

    def _boom(src, plan_id):
        raise RuntimeError("exploded mid-apply")

    monkeypatch.setattr(server, "apply_plan", _boom)

    plan_id = ids.new_id()
    out = server.tool_triage_apply(plan_id=plan_id)
    assert out["ok"] is False and out["code"] == "internal_error"
    assert out["operation_id"] == plan_id  # threads to the ledger's op

    other = ids.new_id()
    out2 = server.tool_triage_apply(other)  # positional binding too
    assert out2["operation_id"] == other

    # …and an id shape we never mint is a reference to nothing.
    out3 = server.tool_triage_apply("P-123")
    assert out3["ok"] is False
    assert "operation_id" not in out3


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


# --------------------------------------------------------------------- #
# Wire audit: a corrupt Envelope Index is ONE code across all 20 tools   #
# --------------------------------------------------------------------- #


@pytest.fixture
def junk_index(tmp_path, monkeypatch):
    """A Mail tree whose Envelope Index is not a SQLite file at all."""
    mail_dir = tmp_path / "V10"
    (mail_dir / "MailData").mkdir(parents=True)
    (mail_dir / "MailData" / "Envelope Index").write_bytes(
        b"this is not a database at all" * 64)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_dir))
    monkeypatch.setenv("EMAIL_MCP_SOURCE", "apple")
    monkeypatch.setattr(server, "_SOURCE", None)  # force the lazy build
    return mail_dir


def test_corrupt_index_reads_as_mail_unavailable_on_every_tool(junk_index):
    """The live audit found ONE store yielding TWO codes: sqlite3 raises a
    bare DatabaseError ("file is not a database") on some paths and an
    OperationalError on others, and the belt caught only the latter — so
    list_mailboxes/list_recent/get_attachment said internal_error while
    search/get said mail_unavailable. §3.5 makes mail_unavailable the
    single honest answer: the store is unreadable."""
    calls = {
        "search_emails": lambda: server.tool_search_emails(query="x"),
        "get_email": lambda: server.tool_get_email("100"),
        "get_emails_batch": lambda: server.tool_get_emails_batch(["100"]),
        "get_thread": lambda: server.tool_get_thread("7001"),
        "list_mailboxes": lambda: server.tool_list_mailboxes(),
        "list_recent": lambda: server.tool_list_recent(),
        "get_attachment": lambda: server.tool_get_attachment("100", "1.2"),
    }
    got = {}
    for name, call in calls.items():
        server._SOURCE = None
        out = call()
        got[name] = out.get("code") if out.get("ok") is False else "OK"
    assert set(got.values()) == {"mail_unavailable"}, got


def test_corrupt_index_never_reads_as_an_empty_thread(junk_index):
    """get_thread returned {ok: true, thread: []} on the junk store:
    _probe_columns swallowed the DatabaseError per table, so
    _have('messages','conversation_id') was False and thread() reported a
    broken store as an empty conversation."""
    out = server.tool_get_thread("7001")
    assert out["ok"] is False
    assert out.get("thread") is None


def _partial_conn(readable: set[str]):
    """A connection whose PRAGMA table_info RAISES for every table outside
    `readable` — the shape of version drift that actually errors (a merely
    absent table returns no rows and no exception)."""
    import sqlite3

    class _Cur:
        def execute(self, sql):
            table = sql.split("(", 1)[1].rstrip(")")
            if table not in readable:
                raise sqlite3.DatabaseError(f"no such table: {table}")
            self._rows = [(0, "ROWID", "INTEGER", 0, None, 1)]
            return self

        def fetchall(self):
            return self._rows

    class _Conn:
        def cursor(self):
            return _Cur()

    return _Conn()


def test_probe_columns_degrades_per_table_but_not_when_all_fail(
    mail_fixture,
):
    """Per-table degradation for genuine version drift must survive; ONLY
    the nothing-came-back case may raise."""
    import sqlite3

    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)

    src._conn = _partial_conn({"messages", "mailboxes"})
    cols = src._probe_columns()  # must NOT raise
    assert cols["messages"] == {"ROWID"}
    assert cols["conversations"] == set()  # drifted away → degraded

    src._conn = _partial_conn(set())
    with pytest.raises(sqlite3.DatabaseError):
        src._probe_columns()


def test_sqlite_programming_error_is_internal_error_not_the_store(
    monkeypatch,
):
    """ProgrammingError is a DatabaseError sibling, but it means OUR SQL or
    binding is wrong — a bug here, never an unreadable Mail store."""
    import sqlite3

    class BadSql:
        def search(self, q):
            raise sqlite3.ProgrammingError(
                "Incorrect number of bindings supplied")

    monkeypatch.setattr(server, "_SOURCE", BadSql())
    out = server.tool_search_emails(query="x")
    assert out["ok"] is False and out["code"] == "internal_error"


def test_missing_local_emlx_is_not_found_not_mail_unavailable(
    mail_fixture, monkeypatch
):
    """Message 200 exists in the index but was never downloaded (the
    ordinary IMAP case). §3.5 files a vanished attachment under not_found;
    the store itself is perfectly readable."""
    from email_mcp.sources.apple_mail import AppleMailSource

    monkeypatch.setattr(server, "_SOURCE",
                        AppleMailSource(mail_base=mail_fixture))
    out = server.tool_get_attachment("200", "1.2")
    assert out["ok"] is False and out["code"] == "not_found"

    # The readable store still serves its neighbours.
    assert server.tool_get_email("100")["ok"] is True


# --------------------------------------------------------------------- #
# serverInfo.version is OURS, not the mcp library's                     #
# --------------------------------------------------------------------- #


def test_server_advertises_the_package_version_on_the_wire():
    """FastMCP takes no version=, so the low-level Server fell back to
    importlib.metadata.version("mcp") — clients were told the MCP library's
    version (1.29.0, or whatever resolved), never 0.11.0."""
    import importlib.metadata

    from email_mcp import __version__

    mcp = server._build_mcp_server()
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_name == "apple-mail"
    assert opts.server_version == __version__
    assert opts.server_version != importlib.metadata.version("mcp")


# --------------------------------------------------------------------- #
# §3.4: the smtp secret-source lane prefix (the shipped-late promise)    #
# --------------------------------------------------------------------- #


def test_smtp_secret_errors_carry_the_lane_prefix_and_still_code(
    monkeypatch,
):
    from email_mcp.transports import smtp as smtp_mod
    from email_mcp.transports import SendError

    t = smtp_mod.SmtpTransport(host="smtp.gmail.com", keychain="email-mcp-g",
                               identity="gmail", from_addr="g@example.org")

    def _no_cli(item, account):
        raise SendError("`security` CLI not found — the smtp driver needs "
                        "macOS.")

    monkeypatch.setattr(smtp_mod, "_read_keychain", _no_cli)
    with pytest.raises(SendError) as ei:
        t._secret()
    prose = str(ei.value)
    assert prose.startswith("[gmail/smtp] ")
    assert "`security` CLI not found" in prose
    # …and the prefixed prose keeps its §3.4 code.
    assert server._send_code(SendError(prose)) == "credentials_unavailable"

    # ensure() feeds sender._transport_unavailable, which prepends the lane
    # itself — the stash must not carry a second copy.
    assert t.ensure() is False
    assert not t.last_ensure_error.startswith("[gmail/smtp]")

    # op lane too
    t2 = smtp_mod.SmtpTransport(host="smtp.gmail.com", op="op://v/i/password",
                                identity="work", from_addr="w@example.org")
    monkeypatch.setattr(
        smtp_mod, "_read_op",
        lambda ref: (_ for _ in ()).throw(
            SendError("1Password read for 'op://v/i/password' failed "
                      "(op exit 1)")))
    with pytest.raises(SendError) as ei2:
        t2._secret()
    assert str(ei2.value).startswith("[work/smtp] ")
    assert server._send_code(SendError(str(ei2.value))) == \
        "credentials_unavailable"


def test_failure_envelopes_are_bounded_and_do_not_reflect_the_caller():
    """§2/§7: a hostile argument must not come back as a hostile envelope.

    `triage_apply` with a 60 000-character plan_id used to return a
    120 KB envelope — the OSError names the whole offending path, and
    operation_id echoed the raw argument verbatim — straight onto a stdio
    transport, for an operation that minted nothing.
    """
    import json

    big = "A" * 60000
    # Argument validation only — no Mail store, no fixture, no $HOME.
    # An earlier version of this test drove triage_apply, which reaches the
    # truncation path ONLY on a machine that already has ~/Library/Mail:
    # everywhere else it short-circuits on the precondition with a short
    # message, so the test passed on the author's laptop and would have gone
    # red on the first CI run.
    paths = {
        "get_email": lambda: server.tool_get_email("someid", view=big),
        "get_emails_batch": lambda: server.tool_get_emails_batch(["x"], view=big),
        "list_scheduled": lambda: server.tool_list_scheduled(state=big),
        "cancel_scheduled": lambda: server.tool_cancel_scheduled(big),
        "search_emails": lambda: server.tool_search_emails(query="x", after=big),
    }
    for name, call in paths.items():
        out = call()
        assert out["ok"] is False, name
        assert len(json.dumps(out)) < 4000, f"{name} envelope unbounded"
        assert "truncated" in out["error"], name
        assert big not in json.dumps(out), f"{name} reflected the payload"


def test_operation_id_is_echoed_only_for_minted_shaped_ids():
    """The raw argument is the caller's claim, not proof of an artifact."""
    from email_mcp import ids

    for bogus in ("A" * 60000, "a\x00b\x07c", "not-an-id", ""):
        out = server.tool_triage_apply(bogus)
        assert "operation_id" not in out, f"echoed {bogus[:20]!r}"

    assert ids.is_minted_id(ids.new_id())
    assert not ids.is_minted_id("20260731T123552Z-nothex000000")
    assert not ids.is_minted_id("20260731T123552Z-3793a80f72f3 ")


def test_batch_and_single_read_agree_on_every_code():
    """§3: a code means the same thing on every tool that uses it.

    The batch loop mapped ValueError->invalid_input and LookupError->
    not_found with no carve-outs, while the belt routes UnicodeDecodeError
    and KeyError to internal_error — so one store fault yielded two
    different codes depending on which tool the caller used.
    """
    import sqlite3

    class Poison:
        def __init__(self, exc):
            self.exc = exc

        def get(self, id):
            raise self.exc

    cases = [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        KeyError("internal container miss"),
        IndexError("internal index miss"),
        LookupError("no such message"),
        ValueError("bad id"),
        sqlite3.DatabaseError("file is not a database"),
        sqlite3.OperationalError("database is locked"),
    ]
    original = server._SOURCE
    try:
        for exc in cases:
            server._SOURCE = Poison(exc)
            batch = server.tool_get_emails_batch(["100"])
            single = server.tool_get_email("100")
            assert batch["errors"][0]["code"] == single["code"], (
                f"{type(exc).__name__}: batch={batch['errors'][0]['code']} "
                f"single={single['code']}")
    finally:
        server._SOURCE = original


def test_batch_rejects_oversized_ids_without_echoing_them():
    """50 ids AT the cap × a 60 KB "id" was a 3 MB envelope: per-id errors[]
    echo each id back for correlation, and the over-cap reject never fires
    at exactly 50. A real id is a ROWID or a minted spool id — never
    hundreds of characters — so length-validate before the loop, naming the
    position rather than reflecting the value."""
    import json

    out = server.tool_get_emails_batch(["B" * 60000] * 50, view="minimal")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert len(json.dumps(out)) < 500
    assert "position 0" in out["error"]
    assert "B" * 100 not in json.dumps(out)


def test_clip_bounds_wire_bytes_not_characters():
    """2 000 astral-plane characters JSON-escape to ~24 KB of ASCII — a
    character cap bounds the prose while leaving the wire payload §7
    actually promises unbounded. The clip measures UTF-8 bytes."""
    import json

    out = server.tool_get_email("x", view="\U0001F600" * 60000)
    assert out["ok"] is False
    assert len(json.dumps(out)) < 16000
    # And the truncation must not have split a codepoint into garbage.
    out["error"].encode("utf-8")  # raises on a broken surrogate
