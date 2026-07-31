"""Build an in-test Apple Mail fixture: an Envelope Index DB + a few .emlx files."""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


def _make_emlx(rfc822: bytes, flags: int = 0) -> bytes:
    """Wrap an RFC 822 message in Apple's .emlx framing (length prefix + plist)."""
    plist = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict>\n'
        b"<key>flags</key><integer>" + str(flags).encode() + b"</integer>\n"
        b"</dict></plist>\n"
    )
    return f"{len(rfc822):<10}\n".encode() + rfc822 + plist


def _write(p: Path, body: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)


def _build_envelope_index(db_path: Path) -> None:
    """Minimal Envelope Index reproducing only the columns the source touches.

    Schema mirrors what we probed on the live install — extra columns omitted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(textwrap.dedent("""
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            subject INTEGER NOT NULL,
            sender INTEGER,
            summary INTEGER,
            date_sent INTEGER,
            date_received INTEGER,
            mailbox INTEGER NOT NULL,
            flags INTEGER NOT NULL DEFAULT 0,
            read INTEGER NOT NULL DEFAULT 0,
            flagged INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            conversation_id INTEGER NOT NULL DEFAULT 0,
            global_message_id INTEGER,
            flag_color INTEGER
        );
        CREATE TABLE message_global_data (
            ROWID INTEGER PRIMARY KEY,
            message_id_header TEXT
        );
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE summaries (ROWID INTEGER PRIMARY KEY, summary TEXT);
        CREATE TABLE addresses (
            ROWID INTEGER PRIMARY KEY,
            address TEXT,
            comment TEXT
        );
        CREATE TABLE mailboxes (
            ROWID INTEGER PRIMARY KEY,
            url TEXT,
            total_count INTEGER DEFAULT 0,
            unread_count INTEGER DEFAULT 0
        );
        CREATE TABLE recipients (
            ROWID INTEGER PRIMARY KEY,
            message INTEGER,
            address INTEGER,
            type INTEGER,
            position INTEGER
        );
        CREATE TABLE attachments (
            ROWID INTEGER PRIMARY KEY,
            message INTEGER,
            attachment_id TEXT,
            name TEXT
        );
    """))

    # Two accounts: one local, one IMAP-like.
    LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
    IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"

    cur.executemany(
        "INSERT INTO mailboxes(ROWID, url, total_count, unread_count) VALUES (?,?,?,?)",
        [
            (1, f"local://{LOCAL_ACCT}/Inbox", 3, 1),
            (2, f"imap://{IMAP_ACCT}/%5BGmail%5D/All%20Mail", 1, 0),
        ],
    )

    # Subjects + summaries + senders/recipients
    cur.executemany(
        "INSERT INTO subjects(ROWID, subject) VALUES (?,?)",
        [
            (1, "I2C disclosure on April 20"),
            (2, "EMCI production update"),
            (3, "Weekly DCS ops digest"),
            (4, "All-mail archived note"),
        ],
    )
    cur.executemany(
        "INSERT INTO summaries(ROWID, summary) VALUES (?,?)",
        [
            (1, "Snippet of the I2C email body"),
            (2, "Production figures attached"),
            (3, "Routine ops digest"),
            (4, "Archived item"),
        ],
    )
    cur.executemany(
        "INSERT INTO addresses(ROWID, address, comment) VALUES (?,?,?)",
        [
            (1, "stefan.schlenker@cern.ch", "Stefan Schlenker"),
            (2, "paris.moschovakos@cern.ch", "Paris Moschovakos"),
            (3, "ops-bot@cern.ch", "DCS Ops"),
            (4, "noreply@example.com", "Example"),
        ],
    )

    # Messages: ROWID 100, 101 in Inbox (one with attachment, one unread),
    #           ROWID 200 in Inbox (no .emlx on disk — simulates IMAP-only),
    #           ROWID 300 in [Gmail]/All Mail (HTML-only body).
    cur.executemany(
        "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, date_received, mailbox, read, flagged, deleted, conversation_id, global_message_id, flag_color) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (100, 1, 1, 1, 1714600000, 1714600100, 1, 0, 0, 0, 7001, 9100, None),
            (101, 2, 3, 2, 1714700000, 1714700100, 1, 1, 0, 0, 7002, 9101, None),
            (200, 3, 3, 3, 1714500000, 1714500100, 1, 1, 0, 0, 7001, 9200, None),  # same convo as 100
            (300, 4, 4, 4, 1714400000, 1714400100, 2, 1, 0, 0, 7003, 9300, None),
        ],
    )
    # Global data: RFC Message-ID headers (9200 has none — exercises the
    # triage skip-recheck path for the rare header-less rows).
    cur.executemany(
        "INSERT INTO message_global_data(ROWID, message_id_header) VALUES (?,?)",
        [
            (9100, "<i2c-2026-05-01@cern.ch>"),
            (9101, "<emci-update@cern.ch>"),
            (9200, ""),
            (9300, "<archived@example.com>"),
        ],
    )
    # Recipients: 100 To paris, 101 To paris + Cc ops, 300 To paris
    cur.executemany(
        "INSERT INTO recipients(message, address, type, position) VALUES (?,?,?,?)",
        [
            (100, 2, 0, 0),
            (101, 2, 0, 0),
            (101, 3, 1, 1),
            (300, 2, 0, 0),
        ],
    )
    # Attachments: 101 has one
    cur.execute(
        "INSERT INTO attachments(message, attachment_id, name) VALUES (?,?,?)",
        (101, "1.2", "production.csv"),
    )

    conn.commit()
    conn.close()


def _build_emlx_tree(mail_dir: Path) -> None:
    """Write the inner UUID dir + Data/<shard>/Messages/<rowid>.emlx for each
    message that should have a body on disk."""
    LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
    IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"
    INNER = "CCCCCCCC-0000-0000-0000-000000000003"

    def shard(rowid: int) -> Path:
        s = str(rowid)
        if len(s) <= 3:
            return Path("Messages") / f"{rowid}.emlx"
        return Path(*reversed(s[:-3])) / "Messages" / f"{rowid}.emlx"

    # Message 100: plain-text body in local Inbox
    msg100 = textwrap.dedent("""\
        From: Stefan Schlenker <stefan.schlenker@cern.ch>
        To: Paris Moschovakos <paris.moschovakos@cern.ch>
        Subject: I2C disclosure on April 20
        Date: Fri, 01 May 2026 23:07:00 +0200
        Message-ID: <i2c-2026-05-01@cern.ch>
        Content-Type: text/plain; charset=utf-8

        Paris,

        The I2C disclosure on April 20 should be retracted.

        Stefan
    """).encode()
    _write(
        mail_dir / LOCAL_ACCT / "Inbox.mbox" / INNER / "Data" / shard(100),
        _make_emlx(msg100),
    )

    # Message 101: multipart with attachment
    msg101 = textwrap.dedent("""\
        From: DCS Ops <ops-bot@cern.ch>
        To: Paris Moschovakos <paris.moschovakos@cern.ch>
        Cc: DCS Ops <ops-bot@cern.ch>
        Subject: EMCI production update
        Date: Sat, 02 May 2026 09:00:00 +0200
        Message-ID: <emci-update@cern.ch>
        MIME-Version: 1.0
        Content-Type: multipart/mixed; boundary="BOUND"

        --BOUND
        Content-Type: text/plain; charset=utf-8

        See attached production figures.
        --BOUND
        Content-Type: text/csv; name="production.csv"
        Content-Disposition: attachment; filename="production.csv"

        date,units
        2026-04-09,168
        --BOUND--
    """).encode()
    _write(
        mail_dir / LOCAL_ACCT / "Inbox.mbox" / INNER / "Data" / shard(101),
        _make_emlx(msg101),
    )

    # Message 200 deliberately has NO .emlx — simulates an IMAP-only message
    # without a local copy. The source must fall back to the snippet.

    # Message 300: HTML-only body, in nested [Gmail]/All Mail
    msg300 = textwrap.dedent("""\
        From: Example <noreply@example.com>
        To: Paris Moschovakos <paris.moschovakos@cern.ch>
        Subject: All-mail archived note
        Date: Sun, 03 May 2026 12:00:00 +0200
        Message-ID: <archived@example.com>
        Content-Type: text/html; charset=utf-8

        <html><body><p>Hello <b>Paris</b>.</p><script>noise()</script><p>Bye.</p></body></html>
    """).encode()
    nested = mail_dir / IMAP_ACCT / "[Gmail].mbox" / "All Mail.mbox" / INNER / "Data" / shard(300)
    _write(nested, _make_emlx(msg300))


@pytest.fixture
def mail_fixture(tmp_path: Path) -> Path:
    """Materialise a complete fake Mail V10 directory tree + Envelope Index."""
    mail_dir = tmp_path / "V10"
    _build_envelope_index(mail_dir / "MailData" / "Envelope Index")
    _build_emlx_tree(mail_dir)
    return mail_dir


@pytest.fixture
def audit_dir_guard(state_root_guard) -> Path:
    """The ledger directory under the pinned root. Derived, not pinned
    separately — one root is the whole point."""
    return state_root_guard / "audit"


@pytest.fixture
def fts_dir_guard(state_root_guard) -> Path:
    """The FTS index directory under the pinned root."""
    return state_root_guard / "fts"


@pytest.fixture(autouse=True)
def audit_process_guard():
    """Restore audit's process tag around EVERY test.

    ``audit.set_process`` is module-global, and anything that runs
    ``repairs.run_fixes()`` or a lifecycle command sets it to "cli" and
    leaves it there. The next test to assert ``src == "server"`` then fails
    — but only in some orderings, which is how it stayed hidden: in the
    repo the modules that set it happen to sort after the modules that
    assert it. Restoring here makes the suite order-independent.
    """
    from email_mcp import audit

    before = audit._PROCESS
    yield
    audit.set_process(before)


@pytest.fixture(autouse=True)
def state_root_guard(tmp_path_factory, monkeypatch) -> Path:
    """Point the ENTIRE state tree at a per-test tmp root for EVERY test.

    One guard replaces the old per-directory fts/audit pins: since v0.11
    every managed directory derives from a single root, so isolating the
    root isolates the spool, plans, graph, index and ledger together, and
    nothing in the suite can touch (or create) the developer's own
    ~/.email-mcp.

    Belt on top of the env pin: several test modules wipe every EMAIL_MCP_*
    variable in their own autouse fixtures, which run AFTER this one — the
    env pin alone would not survive them, and any mutation-path emit would
    land in the REAL ledger. So the resolver is pinned too.
    tests/test_config_state_dirs.py shadows this fixture by name on purpose:
    its tests exercise the real env-driven resolution inside a fake HOME.
    """
    from email_mcp import config

    d = tmp_path_factory.mktemp("state-root")
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(d))
    monkeypatch.setattr(config, "state_root", lambda create=True: d)
    return d


