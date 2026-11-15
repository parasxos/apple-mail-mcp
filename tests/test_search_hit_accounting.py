"""search_emails accounts for every body-index hit.

`fts.hits` counts the body-index matches for the query text before any
filter. Each hit must end in exactly one place: a result on this page
(`hits_returned`), a match on another page (`hits_other_pages`), or a
named drop (`hits_dropped`, reason -> count). Any drop, and any empty page
that had hits, sets `note`, so an empty list never reads as "no mail".

Synthetic data only: the shared fake Mail tree (tests/conftest.py) plus
the messages written below. Their bodies hold "harbor ledger", which no
subject, sender or snippet of the fixture holds, so every match below is
a body-index match.
"""
from __future__ import annotations

import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from email_mcp import bootstrap
from email_mcp.domain.mail import SearchQuery
from email_mcp.fts import FtsIndex
from email_mcp.server import tool_search_emails
from email_mcp.sources.apple_mail import AppleMailSource
from email_mcp.sources.apple_mail_paths import emlx_relpath_for_rowid

LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
INNER = "CCCCCCCC-0000-0000-0000-000000000003"
OLD = 1700000000      # 2023-11-14
RECENT = 1767225600   # 2026-01-01
SENDER, READER = 900, 901  # address rows of this file
TRASH = 40                 # mailbox row of this file
QUERY = "harbor ledger"


def _index(mail_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(mail_dir / "MailData" / "Envelope Index")


def _add(mail_dir: Path, rowid: int, *, date: int | None = None,
         read: int = 1) -> None:
    """One message in the local Inbox whose body holds the query words;
    a higher rowid is a newer message unless `date` says otherwise."""
    date = RECENT + rowid if date is None else date
    conn = _index(mail_dir)
    conn.execute(
        "INSERT OR IGNORE INTO addresses(ROWID, address, comment) "
        "VALUES (?, 'sender@example.org', 'Example Sender'), "
        "(?, 'reader@example.org', 'Example Reader')", (SENDER, READER))
    conn.execute("INSERT INTO subjects(ROWID, subject) VALUES (?, ?)",
                 (rowid, f"synthetic message {rowid}"))
    conn.execute(
        "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, "
        "date_received, mailbox, read, flagged, deleted, conversation_id, "
        "global_message_id, flag_color) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rowid, rowid, SENDER, None, date, date + 60, 1, read, 0, 0,
         8000 + rowid, 9000 + rowid, None),
    )
    conn.execute("INSERT INTO recipients(message, address, type, position) "
                 "VALUES (?, ?, 0, 0)", (rowid, READER))
    conn.commit()
    conn.close()
    rfc = textwrap.dedent(f"""\
        From: Example Sender <sender@example.org>
        To: Example Reader <reader@example.org>
        Subject: synthetic message {rowid}
        Content-Type: text/plain; charset=utf-8

        The harbor ledger for item {rowid} is attached in spirit.
    """).encode()
    framed = f"{len(rfc):<10}\n".encode() + rfc + b"<plist/>\n"
    path = (mail_dir / LOCAL_ACCT / "Inbox.mbox" / INNER / "Data"
            / emlx_relpath_for_rowid(rowid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(framed)


def _change(mail_dir: Path, sql: str, *args) -> None:
    """Mail changes its index after the body was indexed."""
    conn = _index(mail_dir)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _closes(fts: dict) -> bool:
    return fts["hits"] == (fts["hits_returned"] + fts["hits_other_pages"]
                           + sum(fts["hits_dropped"].values()))


@pytest.fixture
def src(mail_fixture, monkeypatch) -> AppleMailSource:
    s = AppleMailSource(mail_base=mail_fixture)
    monkeypatch.setattr(bootstrap, "_application",
                        bootstrap.build_application(source=s))
    return s


def test_hits_left_out_by_the_date_filter_are_counted_and_named(
        src, mail_fixture):
    """The reported case: a two-word query, an `after` date and limit 10
    answered hits 2, results [], note null."""
    _add(mail_fixture, 500, date=OLD)
    _add(mail_fixture, 501, date=OLD)
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query=QUERY, after="2025-06-01", limit=10)

    assert out["ok"] is True and out["results"] == []
    fts = out["fts"]
    assert fts["hits"] == 2
    assert fts["hits_returned"] == 0 and fts["hits_other_pages"] == 0
    assert fts["hits_dropped"] == {"after": 2}
    assert _closes(fts)
    assert "2 of 2" in out["note"] and "after" in out["note"]


def test_every_hit_is_returned_on_another_page_or_dropped(mail_fixture):
    for rowid in range(500, 507):
        _add(mail_fixture, rowid)
    _change(mail_fixture, "UPDATE messages SET date_sent=? WHERE ROWID=500",
            OLD)
    FtsIndex(mail_base=mail_fixture).build()
    _change(mail_fixture, "UPDATE messages SET deleted=1 WHERE ROWID=501")
    _change(mail_fixture, "DELETE FROM messages WHERE ROWID=502")
    _change(mail_fixture, "DELETE FROM subjects WHERE ROWID=503")
    src = AppleMailSource(mail_base=mail_fixture)

    page = src.search(SearchQuery(query=QUERY, limit=2))
    fts = src.fts_status()

    assert [ref.id for ref in page] == ["506", "505"]
    assert fts["hits"] == 7
    assert fts["hits_returned"] == 2
    assert fts["hits_other_pages"] == 2  # 504 and the old 500
    assert fts["hits_dropped"] == {
        "not_in_mail": 1, "incomplete_row": 1, "deleted": 1}
    assert _closes(fts)

    after = datetime(2025, 6, 1, tzinfo=timezone.utc)
    src.search(SearchQuery(query=QUERY, after=after, limit=10))
    fts = src.fts_status()
    assert fts["hits_returned"] == 3 and fts["hits_other_pages"] == 0
    assert fts["hits_dropped"] == {
        "not_in_mail": 1, "incomplete_row": 1, "deleted": 1, "after": 1}
    assert _closes(fts)


@pytest.mark.parametrize("reason, criteria", [
    ("from_addr", {"from_addr": "nobody@example.net"}),
    ("to_addr", {"to_addr": "nobody@example.net"}),
    ("mailbox", {"mailbox": "Archive"}),
    ("account", {"account": "DDDDDDDD-0000-0000-0000-000000000004"}),
    ("before", {"before": "2025-06-01"}),
    ("after", {"after": "2026-06-01"}),
    ("has_attachment", {"has_attachment": True}),
    ("unread_only", {"unread_only": True}),
])
def test_each_filter_names_the_hits_it_excludes(
        src, mail_fixture, reason, criteria):
    _add(mail_fixture, 500)
    FtsIndex(mail_base=mail_fixture).build()

    assert [r["id"] for r in tool_search_emails(query=QUERY)["results"]] \
        == ["500"]
    out = tool_search_emails(query=QUERY, **criteria)

    assert out["results"] == []
    assert out["fts"]["hits_dropped"] == {reason: 1}
    assert _closes(out["fts"])
    assert reason in out["note"]


def test_a_hit_failing_two_filters_is_counted_once(src, mail_fixture):
    _add(mail_fixture, 500)
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query=QUERY, after="2026-06-01",
                             to_addr="nobody@example.net")

    assert out["fts"]["hits_dropped"] == {"after": 1}  # cheapest check first
    assert _closes(out["fts"])


def test_delete_planning_names_hits_in_trash(mail_fixture):
    _add(mail_fixture, 500)
    FtsIndex(mail_base=mail_fixture).build()
    _change(mail_fixture, "INSERT INTO mailboxes(ROWID, url) VALUES (?, ?)",
            TRASH, f"local://{LOCAL_ACCT}/Trash")
    _change(mail_fixture, "UPDATE messages SET mailbox=? WHERE ROWID=500",
            TRASH)
    src = AppleMailSource(mail_base=mail_fixture)

    assert src.search(SearchQuery(query=QUERY, exclude_trash=True)) == []
    assert src.fts_status()["hits_dropped"] == {"trash": 1}


def test_an_empty_later_page_says_its_hits_are_on_other_pages(
        src, mail_fixture):
    _add(mail_fixture, 500)
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query=QUERY, offset=5)

    assert out["results"] == []
    assert out["fts"]["hits_other_pages"] == 1
    assert out["fts"]["hits_dropped"] == {}
    assert "other pages" in out["note"]


def test_a_page_that_only_paginates_its_hits_carries_no_note(
        src, mail_fixture):
    for rowid in (500, 501, 502):
        _add(mail_fixture, rowid)
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query=QUERY, limit=1)

    assert [r["id"] for r in out["results"]] == ["502"]
    assert out["results"][0]["body_match"] is True
    assert out["fts"]["hits_returned"] == 1
    assert out["fts"]["hits_other_pages"] == 2
    assert out["fts"]["hits_dropped"] == {}
    assert out["note"] is None


def test_a_search_without_hits_has_nothing_to_account_for(src):
    out = tool_search_emails(query="zzz-no-such-thing")
    assert out["results"] == [] and out["note"] is None
    fts = out["fts"]
    assert fts["hits"] == 0 and fts["hits_dropped"] == {}
    assert _closes(fts)


def test_ghost_mailbox_note_and_hit_note_are_both_given(src, mail_fixture):
    _add(mail_fixture, 500)
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query=QUERY, mailbox="Promo")

    assert out["results"] == []
    assert out["fts"]["hits_dropped"] == {"mailbox": 1}
    assert "[Gmail]/All Mail" in out["note"]
    assert "mailbox filter" in out["note"]


def test_a_failed_accounting_is_named_not_hidden(mail_fixture, monkeypatch):
    for rowid in (500, 501):
        _add(mail_fixture, rowid)
    FtsIndex(mail_base=mail_fixture).build()
    src = AppleMailSource(mail_base=mail_fixture)

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(src, "_first_failed_filter", broken)
    page = src.search(SearchQuery(query=QUERY, limit=1))

    assert [ref.id for ref in page] == ["501"]  # the search itself stands
    fts = src.fts_status()
    assert fts["hits_returned"] == 1
    assert fts["hits_dropped"] == {"unclassified": 1}
    assert _closes(fts)
