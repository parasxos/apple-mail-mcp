"""Reply routing from fixture mailbox records, without transport access."""
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import _make_emlx
from email_mcp import sender
from email_mcp.sources.apple_mail import AppleMailSource


@pytest.fixture
def reply_arguments(monkeypatch):
    monkeypatch.setattr(sender.identities, "get", lambda _: SimpleNamespace(
        from_addr="paris.moschovakos@cern.ch"))
    monkeypatch.setattr(sender, "send_email", lambda **values: values)


def test_reply_all_never_promotes_bcc_to_cc(mail_fixture, reply_arguments):
    conn = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    try:
        conn.executemany(
            "INSERT INTO addresses(ROWID, address) VALUES (?, ?)",
            [(901, "visible@example.org"), (902, "hidden@example.org")],
        )
        conn.executemany(
            "INSERT INTO recipients(message, address, type, position) "
            "VALUES (101, ?, ?, ?)",
            [(901, 1, 8), (902, 2, 9)],
        )
        conn.commit()
    finally:
        conn.close()

    source = AppleMailSource(mail_fixture)
    original = source.get("101")
    assert "visible@example.org" in original.ref.cc
    assert "hidden@example.org" not in original.ref.to + original.ref.cc

    reply = sender.reply_email(source, id="101", body="Received", reply_all=True)
    assert reply["cc"] == ["visible@example.org"]
    assert "hidden@example.org" not in reply["to"]


def test_partial_headers_preserve_reply_to_and_thread(
        mail_fixture, reply_arguments):
    path = next(mail_fixture.rglob("100.emlx"))
    partial = path.with_name("100.partial.emlx")
    path.rename(partial)
    partial.write_bytes(_make_emlx(
        b"From: original@example.org\r\n"
        b"Reply-To: replies@example.org\r\n"
        b"Message-ID: <partial@example.org>\r\n"
        b"References: <ancestor@example.org>\r\n\r\n"
    ))

    reply = sender.reply_email(
        AppleMailSource(mail_fixture), id="100", body="Received")
    assert reply["to"] == ["replies@example.org"]
    assert reply["in_reply_to"] == "<partial@example.org>"
    assert reply["references"] == "<ancestor@example.org>"
