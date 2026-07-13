"""Sender tests — MIME composition, the self-only allowlist guard, and reply
threading. Transport is always mocked; nothing leaves the machine."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from email_mcp import sender


@pytest.fixture(autouse=True)
def _clean_send_env(monkeypatch):
    """Start each test from documented defaults, not the caller's shell env."""
    for k in list(__import__("os").environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "paris.moschovakos@cern.ch")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Paris Moschovakos")


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
# allowlist guard                                                       #
# --------------------------------------------------------------------- #


def test_allowlist_blocks_foreign_recipient(capture_delivery):
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


def test_foreign_cc_also_blocked(capture_delivery):
    with pytest.raises(sender.SendError):
        sender.send_email(
            to="paris.moschovakos@cern.ch",
            cc="someoneelse@cern.ch",
            subject="s", body="b",
        )
    assert capture_delivery == []


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
