"""Network-free coverage of email_mcp.imap — the pure MIME/quoting
helpers the backfill's fetched bytes flow through. The protocol
conversation itself is exercised against the live estate; these lock
the parsing rules a truncated first-window FETCH depends on."""
from __future__ import annotations

from types import SimpleNamespace

from email_mcp import imap


def _mime(parts: list[tuple[str, str]], boundary: str = "b1") -> bytes:
    body = "".join(
        f"--{boundary}\r\nContent-Type: {ctype}; charset=utf-8\r\n"
        f"{extra}\r\n{text}\r\n"
        for ctype, extra, text in (
            (p[0], p[2] if len(p) > 2 else "", p[1])  # type: ignore[misc]
            for p in parts
        ))
    return (
        f"From: a@x\r\nTo: b@x\r\nSubject: s\r\n"
        f"Content-Type: multipart/mixed; boundary=\"{boundary}\"\r\n\r\n"
        f"{body}--{boundary}--\r\n").encode()


def test_extract_prefers_plain_text_over_html():
    raw = _mime([("text/plain", "the plain truth"),
                 ("text/html", "<p>the html copy</p>")])
    out = imap._extract_body(raw)
    assert out["contentType"] == "text"
    assert "the plain truth" in out["content"]
    assert "html copy" not in out["content"]


def test_extract_html_only_declares_html():
    raw = _mime([("text/html", "<p>only html</p>")])
    out = imap._extract_body(raw)
    assert out["contentType"] == "html"
    assert "<p>only html</p>" in out["content"]


def test_extract_skips_text_attachments():
    raw = _mime([
        ("text/plain", "real body"),
        ("text/plain", "attached log line",
         "Content-Disposition: attachment; filename=\"a.log\"\r\n"),
    ])
    out = imap._extract_body(raw)
    assert "real body" in out["content"]
    assert "attached log" not in out["content"]


def test_extract_simple_singlepart_message():
    raw = (b"From: a@x\r\nSubject: s\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"single part body\r\n")
    out = imap._extract_body(raw)
    assert out == {"contentType": "text", "content": "single part body\r\n"}


def test_extract_empty_message_is_an_empty_hit_not_a_crash():
    raw = b"From: a@x\r\nSubject: s\r\n\r\n"
    out = imap._extract_body(raw)
    assert out["content"].strip() == ""


def test_quote_escapes_imap_string_specials():
    assert imap._quote('[Gmail]/All Mail') == '"[Gmail]/All Mail"'
    assert imap._quote('a"b\\c') == '"a\\"b\\\\c"'


def _scope_for(listing: list[bytes]) -> list[str]:
    """_search_folders over a canned LIST answer — no login, no socket."""
    sess = object.__new__(imap._Session)
    sess.prefix = "[fixture/imap]"
    sess.conn = SimpleNamespace(list=lambda: ("OK", listing))
    return sess._search_folders()


def test_scope_without_all_mail_searches_inbox_before_the_bins():
    """Codex IMAP_NO_ALL_SCOPE (2026-09-12): a server advertising Trash
    and Junk but no \\All used to scope to the bins alone, so every
    INBOX message backfilled as a confirmed miss."""
    assert _scope_for([
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Trash) "/" "Trash"',
        b'(\\HasNoChildren \\Junk) "/" "Junk"',
    ]) == ["INBOX", "Trash", "Junk"]


def test_scope_with_all_mail_puts_it_first():
    assert _scope_for([
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
    ]) == ["[Gmail]/All Mail", "[Gmail]/Trash"]
