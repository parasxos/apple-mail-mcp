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


# --------------------------------------------------------------------- #
# display names holding a comma (field case, 18 Oct 2026)               #
# --------------------------------------------------------------------- #

NICOLE = '"Leung Yong Ting, Nicole" <nicole.leung@u.nus.edu>'


def _add_recipient(mail_fixture, *, address, comment, message, rtype, position):
    conn = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    try:
        conn.execute(
            "INSERT INTO addresses(ROWID, address, comment) VALUES (903, ?, ?)",
            (address, comment),
        )
        conn.execute(
            "INSERT INTO recipients(message, address, type, position) "
            "VALUES (?, 903, ?, ?)",
            (message, rtype, position),
        )
        conn.commit()
    finally:
        conn.close()


def test_reply_all_keeps_a_comma_inside_a_cc_display_name(
        mail_fixture, reply_arguments):
    """Mail's index stores the bare name "Leung Yong Ting, Nicole" next to
    the address. Reply-all used to re-split that at the comma and refuse
    'Leung' as a recipient; the name is one recipient."""
    _add_recipient(mail_fixture, address="nicole.leung@u.nus.edu",
                   comment="Leung Yong Ting, Nicole", message=101,
                   rtype=1, position=2)
    source = AppleMailSource(mail_fixture)
    assert source.get("101").ref.cc == ["DCS Ops <ops-bot@cern.ch>", NICOLE]

    reply = sender.reply_email(source, id="101", body="Received", reply_all=True)
    assert reply["to"] == ["DCS Ops <ops-bot@cern.ch>"]
    assert reply["cc"] == [NICOLE]


def test_reply_all_keeps_a_comma_inside_a_to_display_name(
        mail_fixture, reply_arguments):
    _add_recipient(mail_fixture, address="nicole.leung@u.nus.edu",
                   comment="Leung Yong Ting, Nicole", message=101,
                   rtype=0, position=2)
    source = AppleMailSource(mail_fixture)
    assert NICOLE in source.get("101").ref.to

    reply = sender.reply_email(source, id="101", body="Received", reply_all=True)
    assert reply["cc"] == [NICOLE]


def test_reply_all_accepts_a_quoted_name_the_index_kept(
        mail_fixture, reply_arguments):
    """Whether or not the index keeps the wire quotes, the recipient comes
    out quoted exactly once."""
    _add_recipient(mail_fixture, address="nicole.leung@u.nus.edu",
                   comment='"Leung Yong Ting, Nicole"', message=101,
                   rtype=1, position=2)
    source = AppleMailSource(mail_fixture)
    assert source.get("101").ref.cc == ["DCS Ops <ops-bot@cern.ch>", NICOLE]

    reply = sender.reply_email(source, id="101", body="Received", reply_all=True)
    assert reply["cc"] == [NICOLE]


def test_reply_routes_to_a_sender_whose_name_holds_a_comma(
        mail_fixture, reply_arguments):
    """A plain reply goes to the index's From when there is no Reply-To,
    so the sender's own name needs the same quoting."""
    conn = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    try:
        conn.execute("UPDATE addresses SET comment = 'Ops, DCS' WHERE ROWID = 3")
        conn.commit()
    finally:
        conn.close()
    source = AppleMailSource(mail_fixture)
    assert source.get("101").ref.from_addr == '"Ops, DCS" <ops-bot@cern.ch>'

    reply = sender.reply_email(source, id="101", body="Received")
    assert reply["to"] == ['"Ops, DCS" <ops-bot@cern.ch>']


def test_reply_all_never_routes_to_a_display_name_without_an_address(
        mail_fixture, reply_arguments):
    """An index row with a NULL address and an address-shaped comment must
    not become a recipient: the comment is display text, never routing.
    (Regression caught by the Codex peer's review.)"""
    conn = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    try:
        conn.execute(
            "INSERT INTO addresses(ROWID, address, comment) "
            "VALUES (903, NULL, 'wrong-target@example.org')")
        conn.execute(
            "INSERT INTO recipients(message, address, type, position) "
            "VALUES (101, 903, 1, 2)")
        conn.commit()
    finally:
        conn.close()
    source = AppleMailSource(mail_fixture)
    assert source.get("101").ref.cc == [
        "DCS Ops <ops-bot@cern.ch>", '"wrong-target@example.org" <>']

    reply = sender.reply_email(source, id="101", body="Received", reply_all=True)
    assert reply["to"] == ["DCS Ops <ops-bot@cern.ch>"]
    assert reply["cc"] == []
    assert "wrong-target" not in " ".join(reply["to"] + reply["cc"])
