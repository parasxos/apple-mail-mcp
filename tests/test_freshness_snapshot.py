"""Tests for AppleMailSource.freshness_snapshot()."""
from __future__ import annotations

from datetime import datetime, timezone

from email_mcp.sources.apple_mail import AppleMailSource


def test_freshness_snapshot_reports_total_and_newest(mail_fixture):
    src = AppleMailSource(mail_base=mail_fixture)
    snap = src.freshness_snapshot()

    assert snap["total"] == 4  # four rows in the fixture, none deleted
    # Newest by date_sent is ROWID 101 (1714700000): "EMCI production update"
    # from DCS Ops <ops-bot@cern.ch>.
    assert snap["newest_date"] is not None
    parsed = datetime.fromisoformat(snap["newest_date"])
    assert parsed.tzinfo is not None
    assert parsed == datetime.fromtimestamp(1714700000, tz=timezone.utc)
    assert snap["newest_subject"] == "EMCI production update"
    assert "ops-bot@cern.ch" in snap["newest_from"]


def test_freshness_snapshot_handles_empty_db(tmp_path):
    """An Envelope Index with no messages must return None for newest fields."""
    import sqlite3
    mail_dir = tmp_path / "V10"
    db_path = mail_dir / "MailData" / "Envelope Index"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            subject INTEGER NOT NULL,
            sender INTEGER,
            date_sent INTEGER,
            date_received INTEGER,
            mailbox INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT, total_count INTEGER, unread_count INTEGER);
    """)
    conn.commit()
    conn.close()

    src = AppleMailSource(mail_base=mail_dir)
    snap = src.freshness_snapshot()
    assert snap == {
        "total": 0,
        "newest_date": None,
        "newest_subject": None,
        "newest_from": None,
    }


def test_freshness_snapshot_reflects_writer_commits(mail_fixture):
    """Each call opens a fresh read-only connection, so a new commit by an
    external writer becomes visible without restarting the server. This is
    the property `refresh_mail` relies on after nudging Mail.app."""
    import sqlite3
    src = AppleMailSource(mail_base=mail_fixture)
    before = src.freshness_snapshot()

    # Simulate Mail.app writing a new message (writer = different connection).
    db = mail_fixture / "MailData" / "Envelope Index"
    writer = sqlite3.connect(db)
    writer.execute(
        "INSERT INTO subjects(ROWID, subject) VALUES (?,?)",
        (99, "freshly arrived"),
    )
    writer.execute(
        "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, "
        "date_received, mailbox, read, flagged, deleted, conversation_id, "
        "global_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (999, 99, 1, 1, 1800000000, 1800000100, 1, 0, 0, 0, 8001, 5555),
    )
    writer.commit()
    writer.close()

    after = src.freshness_snapshot()
    assert after["total"] == before["total"] + 1
    assert after["newest_subject"] == "freshly arrived"
    assert datetime.fromisoformat(after["newest_date"]) == datetime.fromtimestamp(
        1800000000, tz=timezone.utc
    )
