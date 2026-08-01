"""Integration tests for the Apple Mail source against the fixture tree."""
from __future__ import annotations

from datetime import datetime, timezone

from email_mcp.sources.apple_mail import AppleMailSource
from email_mcp.sources.base import SearchQuery


def test_mailboxes(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    boxes = src.mailboxes()
    names = sorted((b.account[:8], b.name) for b in boxes)
    assert names == [("AAAAAAAA", "Inbox"), ("BBBBBBBB", "Promo"),
                     ("BBBBBBBB", "[Gmail]/All Mail")]
    inbox = next(b for b in boxes if b.name == "Inbox")
    assert inbox.total == 3
    assert inbox.unread == 1
    assert inbox.local_count == 3  # synced mailbox: both counts agree
    ghost = next(b for b in boxes if b.name == "Promo")
    assert ghost.total == 133 and ghost.unread == 5
    assert ghost.local_count == 0  # server-side only: zero local rows


def test_recent_orders_by_date_desc(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    refs = src.recent(None, None, limit=10)
    # Date order: 101 (newest), 100, 200, 300 (oldest)
    assert [r.id for r in refs] == ["101", "100", "200", "300"]


def test_search_substring_matches_subject_or_snippet(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    # Subject hit
    hits = src.search(SearchQuery(query="I2C"))
    assert [r.id for r in hits] == ["100"]
    # Snippet hit
    hits = src.search(SearchQuery(query="Production figures"))
    assert [r.id for r in hits] == ["101"]
    # No match
    assert src.search(SearchQuery(query="doesnotexist-zzz")) == []


def test_search_from_filter(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    hits = src.search(SearchQuery(from_addr="stefan"))
    assert [r.id for r in hits] == ["100"]
    # The documented truth (field-observed 2026-08-01): from_addr is a
    # SUBSTRING match over both the address and the display name — a bare
    # domain matches every sender at it.
    hits = src.search(SearchQuery(from_addr="cern.ch"))
    assert [r.id for r in hits] == ["101", "100", "200"]
    hits = src.search(SearchQuery(from_addr="DCS Ops"))
    assert [r.id for r in hits] == ["101", "200"]


def test_search_from_exact_matches_bare_address_only(mail_fixture):
    """from_exact (set only by the delete planner) narrows from_addr to
    case-insensitive equality against the bare sender address — fragments
    and display names select nothing."""
    src = AppleMailSource(mail_base=mail_fixture)

    def exact(addr):
        return [r.id for r in
                src.search(SearchQuery(from_addr=addr, from_exact=True))]

    assert exact("stefan.schlenker@cern.ch") == ["100"]
    assert exact("STEFAN.SCHLENKER@CERN.CH") == ["100"]  # case-insensitive
    assert exact("cern.ch") == []            # domain fragment: nothing
    assert exact("stefan") == []             # local-part fragment: nothing
    assert exact("Stefan Schlenker") == []   # display name: nothing


def test_search_has_attachment_true(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    hits = src.search(SearchQuery(has_attachment=True))
    assert [r.id for r in hits] == ["101"]


def test_search_unread_only(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    hits = src.search(SearchQuery(unread_only=True))
    assert [r.id for r in hits] == ["100"]


def test_search_after_filter(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    # 1714600000 = 2024-05-02. Use a between-message threshold.
    after = datetime.fromtimestamp(1714650000, tz=timezone.utc)
    hits = src.search(SearchQuery(after=after))
    assert [r.id for r in hits] == ["101"]


def test_search_mailbox_filter(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    hits = src.search(SearchQuery(mailbox="All Mail"))
    assert [r.id for r in hits] == ["300"]


def test_thread_returns_both_messages(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    thread = src.thread("7001")
    # Ordered asc by date: 200 (older) then 100 (newer)
    assert [r.id for r in thread] == ["200", "100"]


def test_get_email_plain_text(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    m = src.get("100")
    assert m.ref.id == "100"
    assert m.headers["Subject"] == "I2C disclosure on April 20"
    assert "I2C disclosure on April 20 should be retracted" in m.body_text
    assert m.body_html == ""
    assert m.attachments == []
    assert m.flags == {"read": False, "flagged": False}


def test_get_email_with_attachment(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    m = src.get("101")
    assert len(m.attachments) == 1
    att = m.attachments[0]
    assert att.name == "production.csv"
    assert att.mime == "text/csv"
    assert "See attached production figures" in m.body_text


def test_get_email_html_only_strips_to_text(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    m = src.get("300")
    # body_html present; body_text derived from HTML, script content excluded
    assert "<b>Paris</b>" in m.body_html
    assert "Paris" in m.body_text
    assert "noise()" not in m.body_text


def test_get_email_fallback_when_emlx_missing(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    # Message 200 has no .emlx on disk — body should fall back to snippet.
    m = src.get("200")
    assert m.body_text == "Routine ops digest"
    assert m.attachments == []


def test_get_attachment_materialises_file(mail_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(tmp_path / "atts"))
    src = AppleMailSource(mail_base=mail_fixture)
    m = src.get("101")
    att_id = m.attachments[0].attachment_id
    blob = src.attachment("101", att_id)
    from pathlib import Path
    p = Path(blob.path)
    assert p.exists()
    body = p.read_bytes()
    assert b"date,units" in body
    assert blob.name == "production.csv"


def test_schema_probe_records_columns(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    # Sanity-check the probe — these are the columns our SQL builders branch on.
    assert "summary" in src._columns["messages"]
    assert "conversation_id" in src._columns["messages"]
    assert "deleted" in src._columns["messages"]


# ---------------------------------------------------------------------- #
# v0.8: FTS body hits folded into search()                                #
# ---------------------------------------------------------------------- #


def test_search_finds_body_only_term_after_build(mail_fixture):
    from email_mcp.fts import FtsIndex

    src = AppleMailSource(mail_base=mail_fixture)
    # "retracted" lives only in message 100's BODY — never in subject,
    # sender, or snippet — so the LIKE path alone cannot find it.
    assert src.search(SearchQuery(query="retracted")) == []
    assert src.fts_status()["state"] == "absent"

    FtsIndex(mail_base=mail_fixture).build()
    hits = src.search(SearchQuery(query="retracted"))
    assert [r.id for r in hits] == ["100"]
    st = src.fts_status()
    assert st["state"] == "ready"
    assert st["hits"] == 1
    assert st["hits_capped"] is False
    assert st["backlog"] == 0


def test_search_multiword_body_query_has_and_semantics(mail_fixture):
    from email_mcp.fts import FtsIndex

    FtsIndex(mail_base=mail_fixture).build()
    src = AppleMailSource(mail_base=mail_fixture)
    # Both tokens in message 101's body ("See attached production figures.")
    hits = src.search(SearchQuery(query="See attached"))
    assert [r.id for r in hits] == ["101"]
    # Tokens split across DIFFERENT bodies (101 has "attached", 100 has
    # "retracted") must not match — AND-of-terms, not OR.
    assert src.search(SearchQuery(query="attached retracted")) == []


def test_absent_index_degrades_to_like_and_creates_nothing(
        mail_fixture, monkeypatch, tmp_path):
    from email_mcp import server

    target = tmp_path / "state-never-created"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(target))
    monkeypatch.setattr(server, "_SOURCE", AppleMailSource(mail_base=mail_fixture))

    out = server.tool_search_emails(query="I2C")
    assert out["ok"] is True
    assert out["fts"]["state"] == "absent"
    assert out["fts"]["remedy"] == "python -m email_mcp.fts --build"
    assert out["fts"]["hits"] == 0
    # LIKE path still answers…
    assert [r["id"] for r in out["results"]] == ["100"]
    assert out["results"][0]["body_match"] is False
    # …and the read path left zero traces on disk.
    assert not target.exists()


def test_readonly_open_reads_wal_snapshot_beside_a_live_writer(tmp_path):
    """The shipped contract (apple_mail._connect_readonly): plain `mode=ro`
    honoring WAL — a reader sees the latest committed snapshot while a
    separate WAL writer holds an uncommitted transaction. This replaces the
    old `immutable=1` test: immutable was deliberately abandoned because it
    bypasses WAL and yields "database disk image is malformed" whenever
    Mail.app has pending WAL frames (see _connect_readonly's docstring)."""
    import sqlite3
    from email_mcp.sources.apple_mail import _connect_readonly

    db = tmp_path / "Envelope Index"
    db.parent.mkdir(parents=True, exist_ok=True)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")  # the live Mail.app mode
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    # Writer opens a new transaction and stages an as-yet-uncommitted row.
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (2)")
    try:
        ro = _connect_readonly(db)
        # Reads the last COMMITTED snapshot (1), not the writer's pending 2.
        assert [r[0] for r in ro.execute("SELECT x FROM t")] == [1]
        # And it is genuinely read-only: writes are refused. match= matters —
        # the writer holds a transaction, so a bare OperationalError would also
        # be satisfied by "database is locked", which is a different property.
        import pytest
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO t VALUES (3)")
        ro.close()
        # After the writer commits, a fresh ro connection sees both rows —
        # the property freshness_snapshot() relies on.
        writer.commit()
        # The rows live in the uncheckpointed -wal sidecar: this is precisely
        # what `immutable=1` could not see, so it is the regression guard.
        assert (tmp_path / "Envelope Index-wal").exists()
        ro2 = _connect_readonly(db)
        assert [r[0] for r in ro2.execute("SELECT x FROM t ORDER BY x")] == [1, 2]
        ro2.close()
    finally:
        writer.close()


# --------------------------------------------------------------------- #
# mail_dir — the Full Disk Access boundary (config-owned)                #
# --------------------------------------------------------------------- #


def test_mail_dir_tcc_denial_names_full_disk_access(monkeypatch, tmp_path):
    """Under TCC denial ~/Library/Mail EXISTS, so the not-exists remedy
    never fires — the first user got a raw PermissionError traceback out
    of setup instead of the words "Full Disk Access" (2026-08-01). The
    denial must surface config's own remedy, not [Errno 1] alone."""
    import pytest

    from email_mcp import config

    home = tmp_path / "h"
    mail = home / "Library" / "Mail"
    mail.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EMAIL_MCP_MAIL_DIR", raising=False)
    mail.chmod(0o000)
    try:
        with pytest.raises(PermissionError) as e:
            config.mail_dir()
    finally:
        mail.chmod(0o755)
    assert "Full Disk Access" in str(e.value)
    assert str(mail) in str(e.value)
