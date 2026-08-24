"""FTS body-index tests against the fake Mail fixture tree.

Fixture facts (tests/conftest.py): rowids 100/101/300 have .emlx bodies
("retracted" / "See attached" / HTML "Hello"+"Bye" with a script `noise()`),
rowid 200 has NO .emlx. The autouse state_dir_guard points
EMAIL_MCP_STATE_DIR at a per-test tmp root (the index lives in its fts/).
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from email_mcp import fts
from email_mcp.fts import FtsIndex, match_expr
from email_mcp.sources.apple_mail_paths import emlx_relpath_for_rowid

LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
INNER = "CCCCCCCC-0000-0000-0000-000000000003"


# --------------------------------------------------------------------- #
# helpers                                                               #
# --------------------------------------------------------------------- #


def _make_emlx(rfc822: bytes) -> bytes:
    plist = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict>\n'
        b"<key>flags</key><integer>0</integer>\n"
        b"</dict></plist>\n"
    )
    return f"{len(rfc822):<10}\n".encode() + rfc822 + plist


def _write_body(mail_dir: Path, rowid: int, body: str,
                partial: bool = False) -> None:
    """Drop a plain-text .emlx (or .partial.emlx) into the local Inbox tree."""
    rfc = textwrap.dedent(f"""\
        From: Someone <someone@example.com>
        To: Paris Moschovakos <paris.moschovakos@cern.ch>
        Subject: fixture message {rowid}
        Content-Type: text/plain; charset=utf-8

        {body}
    """).encode()
    rel = emlx_relpath_for_rowid(rowid)
    if partial:
        rel = rel.with_name(f"{rowid}.partial.emlx")
    path = mail_dir / LOCAL_ACCT / "Inbox.mbox" / INNER / "Data" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_emlx(rfc))


def _add_envelope_row(mail_dir: Path, rowid: int) -> None:
    """Insert a new message row (local Inbox, mailbox ROWID 1)."""
    conn = sqlite3.connect(mail_dir / "MailData" / "Envelope Index")
    conn.execute("INSERT INTO subjects(ROWID, subject) VALUES (?, ?)",
                 (rowid, f"fixture message {rowid}"))
    conn.execute(
        "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, "
        "date_received, mailbox, read, flagged, deleted, conversation_id, "
        "global_message_id, flag_color) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rowid, rowid, 1, None, 1714800000 + rowid, 1714800100 + rowid,
         1, 1, 0, 0, 8000 + rowid, 9000 + rowid, None),
    )
    conn.commit()
    conn.close()


def _fts_db() -> sqlite3.Connection:
    conn = sqlite3.connect(fts.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _doc_statuses() -> dict[int, str]:
    conn = _fts_db()
    try:
        return {
            int(r["rowid"]): r["status"]
            for r in conn.execute("SELECT rowid, status FROM docs")
        }
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# build                                                                 #
# --------------------------------------------------------------------- #


def test_build_indexes_bodies_but_not_missing_emlx(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    out = idx.build()
    assert out["indexed"] == 3
    assert out["missing"] == 1
    assert out["last_rowid"] == 300

    assert _doc_statuses() == {
        100: "indexed", 101: "indexed", 200: "missing", 300: "indexed",
    }
    conn = _fts_db()
    assert conn.execute("SELECT COUNT(*) FROM body_fts").fetchone()[0] == 3
    conn.close()

    # Body-only terms are now routable (never in subject/snippet).
    assert idx.rowids_matching("retracted") == [100]

    st = idx.status()
    assert st["state"] == "ready"
    assert st["docs"]["indexed"] == 3
    assert st["docs"]["missing"] == 1
    assert st["last_rowid"] == 300
    assert st["schema_version"] == 2
    assert st["built_at"] is not None


def test_html_body_indexed_stripped_in_fts_table(mail_fixture):
    FtsIndex(mail_base=mail_fixture).build()
    conn = _fts_db()
    body = conn.execute(
        "SELECT body FROM body_fts WHERE rowid = 300"
    ).fetchone()["body"]
    conn.close()
    assert "Hello" in body
    assert "Bye" in body
    assert "<p>" not in body and "<b>" not in body


def test_script_content_excluded_from_index(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert idx.rowids_matching("Hello") == [300]
    assert idx.rowids_matching("Bye") == [300]
    assert idx.rowids_matching("noise") == []


def test_doc_cap_honored(mail_fixture, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FTS_DOC_CAP", "200")
    body = "alphastart " + "filler " * 120 + "omegaend"
    _add_envelope_row(mail_fixture, 600)
    _write_body(mail_fixture, 600, body)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert idx.rowids_matching("alphastart") == [600]
    assert idx.rowids_matching("omegaend") == []
    conn = _fts_db()
    nbytes = conn.execute(
        "SELECT bytes FROM docs WHERE rowid = 600"
    ).fetchone()["bytes"]
    conn.close()
    assert nbytes < 300  # 200-char cap + truncation marker


# --------------------------------------------------------------------- #
# incremental                                                           #
# --------------------------------------------------------------------- #


def test_incremental_picks_up_new_row(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert idx.rowids_matching("quagga") == []

    _add_envelope_row(mail_fixture, 400)
    _write_body(mail_fixture, 400, "A quagga is not a zebra.")
    out = idx.incremental()
    assert out["indexed"] == 1
    assert out["last_rowid"] == 400
    assert idx.rowids_matching("quagga") == [400]


def test_miss_retry_upgrades_missing_to_indexed(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert _doc_statuses()[200] == "missing"

    # The .emlx materialises later (IMAP fetch completed)…
    _write_body(mail_fixture, 200, "The routine ops digest has resurfaced.")
    # …and the 1h backoff has elapsed.
    conn = _fts_db()
    conn.execute("UPDATE docs SET last_attempt = 0 WHERE rowid = 200")
    conn.commit()
    conn.close()

    out = idx.incremental()
    assert out["retried"] == 1
    assert _doc_statuses()[200] == "indexed"
    assert idx.rowids_matching("resurfaced") == [200]


def test_miss_retry_respects_backoff_and_attempt_cap(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _write_body(mail_fixture, 200, "now on disk")

    # Fresh miss (attempts=1, last_attempt=now) → not yet due.
    out = idx.incremental()
    assert out["retried"] == 0
    assert _doc_statuses()[200] == "missing"

    # Attempt cap: 6 strikes → never retried again, even when due.
    conn = _fts_db()
    conn.execute("UPDATE docs SET attempts = 6, last_attempt = 0 WHERE rowid = 200")
    conn.commit()
    conn.close()
    out = idx.incremental()
    assert out["retried"] == 0
    assert _doc_statuses()[200] == "missing"


def test_retry_serves_longest_waiting_doc_first(mail_fixture):
    """Under a quota, retries go to the doc that has waited longest since
    its last attempt — NOT ascending rowid, which let ~95k storeless
    low-rowid docs permanently starve a recent message whose body
    materialised late (RC P04, live 2026-08-03)."""
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    conn = _fts_db()
    conn.execute("UPDATE docs SET status='missing', attempts=1, "
                 "last_attempt=1000 WHERE rowid = 100")
    conn.execute("UPDATE docs SET status='missing', attempts=1, "
                 "last_attempt=0 WHERE rowid = 200")
    conn.commit()
    conn.close()

    out = idx.incremental(max_docs=1)  # quota for exactly one retry
    assert out["retried"] == 1
    conn = _fts_db()
    la = dict(conn.execute(
        "SELECT rowid, last_attempt FROM docs WHERE rowid IN (100, 200)"))
    conn.close()
    assert la[200] > 1000   # the longest-waiting doc was re-statted…
    assert la[100] == 1000  # …the fresher one stayed queued behind it


def test_incremental_skips_on_busy_writer(mail_fixture, monkeypatch):
    monkeypatch.setattr(fts, "_BUSY_TIMEOUT_MS", 100)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _add_envelope_row(mail_fixture, 700)
    _write_body(mail_fixture, 700, "contended write")

    blocker = sqlite3.connect(fts.db_path())
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    try:
        out = idx.incremental()
        assert out == {"skipped": "busy"}
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # Next pass (lock released) catches up.
    out = idx.incremental()
    assert out["indexed"] == 1


# --------------------------------------------------------------------- #
# partial + reconcile                                                   #
# --------------------------------------------------------------------- #


def test_partial_emlx_indexed_with_partial_status(mail_fixture):
    _add_envelope_row(mail_fixture, 500)
    _write_body(mail_fixture, 500, "halfway fetched parturient body",
                partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert _doc_statuses()[500] == "partial"
    assert idx.rowids_matching("parturient") == [500]
    assert idx.status()["docs"]["partial"] == 1


def test_reconcile_removes_vanished_rowid(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert idx.rowids_matching("retracted") == [100]

    env = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    env.execute("DELETE FROM messages WHERE ROWID = 100")
    env.commit()
    env.close()

    out = idx.reconcile()
    assert out["removed"] == 1
    assert 100 not in _doc_statuses()
    assert idx.rowids_matching("retracted") == []
    assert idx.status()["last_reconcile_at"] is not None


# --------------------------------------------------------------------- #
# match_expr / injection                                                #
# --------------------------------------------------------------------- #


def test_match_expr_quotes_every_token():
    assert match_expr("I2C disclosure") == '"I2C" AND "disclosure"'
    assert match_expr('drop "table users' ) == '"drop" AND "table" AND "users"'
    assert match_expr("***") == ""
    assert match_expr("") == ""
    assert match_expr(None) == ""  # type: ignore[arg-type]


def test_hostile_fts_syntax_never_raises(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    hostile = [
        '"retracted" OR',
        "NEAR(",
        "a AND (",
        '"" "',
        "-foo",
        "col : val",
        "*",
        "body_fts MATCH x",
        "x NOT y",
        "((((",
        "a^2 + {b}",
        "'; DROP TABLE docs; --",
    ]
    for q in hostile:
        idx.rowids_matching(q)  # must never raise
    # AND-of-terms semantics: all tokens must hit the same body.
    assert idx.rowids_matching("disclosure retracted") == [100]
    assert idx.rowids_matching("disclosure banana") == []


# --------------------------------------------------------------------- #
# read-path purity                                                      #
# --------------------------------------------------------------------- #


def test_status_and_matching_on_absent_index_create_nothing(
        monkeypatch, tmp_path, capsys):
    target = tmp_path / "never-created"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(target))

    idx = FtsIndex()
    st = idx.status()
    assert st["state"] == "absent"
    assert st["remedy"] == "python -m email_mcp.fts --build"
    assert idx.rowids_matching("anything") == []
    assert not target.exists()

    # The CLI --status path is equally pure.
    assert fts.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "absent" in out
    assert "--build" in out
    assert not target.exists()


def test_corrupt_db_degrades_matching_to_no_hits(mail_fixture):
    """A corrupt fts.db raises plain sqlite3.DatabaseError ("file is not
    a database"), which the old OperationalError-only catch let escape —
    turning a body search into a coded failure instead of the snippet-only
    degrade the docstring promises (RC failure matrix FM5). status() must
    keep reporting the corruption so doctor can offer the rebuild."""
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    fts.db_path().write_text("garbage where an SQLite header should be")

    assert idx.rowids_matching("retracted") == []
    st = idx.status()
    assert st["state"] == "error"


# --------------------------------------------------------------------- #
# backfill — server-side bodies for local holes (body-gap fix 2026-08-06)
# --------------------------------------------------------------------- #

EWS_ACCT = "EEEEEEEE-0000-0000-0000-000000000005"
EWS_INNER = "FFFFFFFF-0000-0000-0000-000000000006"
EWS_MBOX_ROWID = 40


class _Ident:
    name = "main"
    executor = "graph"
    drafts = "graph"
    from_addr = "someone@cern.ch"


class _ImapIdent:
    name = "gmail"
    executor = "launchd"
    drafts = "none"
    from_addr = "someone@gmail.com"
    imap = {"host": "imap.example.test", "keychain": "k"}


IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"
IMAP_INNER = "CCCCCCCC-0000-0000-0000-000000000003"


def _write_imap_partial(mail_dir: Path, rowid: int,
                        message_id: str | None) -> None:
    """A headers-only .partial.emlx in the fixture's Gmail-style imap
    account tree (mailbox rowid 2 — [Gmail]/All Mail)."""
    mid = f"Message-ID: {message_id}\n" if message_id else ""
    rfc = (f"From: a@gmail.com\nTo: b@gmail.com\nSubject: g {rowid}\n{mid}"
           f"Content-Type: text/plain; charset=utf-8\n\n").encode()
    rel = emlx_relpath_for_rowid(rowid).with_name(f"{rowid}.partial.emlx")
    path = (mail_dir / IMAP_ACCT / "[Gmail].mbox" / "All Mail.mbox"
            / IMAP_INNER / "Data" / rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_emlx(rfc))


def _add_ews_mailbox(mail_dir: Path) -> None:
    conn = sqlite3.connect(mail_dir / "MailData" / "Envelope Index")
    conn.execute(
        "INSERT INTO mailboxes(ROWID, url, total_count, unread_count) "
        "VALUES (?,?,?,?)",
        (EWS_MBOX_ROWID, f"ews://{EWS_ACCT}/Inbox", 9, 0))
    conn.commit()
    conn.close()


def _add_envelope_row_in(mail_dir: Path, rowid: int, mailbox: int,
                         remote_id: str | None = None) -> None:
    conn = sqlite3.connect(mail_dir / "MailData" / "Envelope Index")
    conn.execute("INSERT INTO subjects(ROWID, subject) VALUES (?, ?)",
                 (rowid, f"fixture message {rowid}"))
    conn.execute(
        "INSERT INTO messages(ROWID, subject, sender, summary, date_sent, "
        "date_received, mailbox, read, flagged, deleted, conversation_id, "
        "global_message_id, flag_color, remote_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rowid, rowid, 1, None, 1714800000 + rowid, 1714800100 + rowid,
         mailbox, 1, 0, 0, 8000 + rowid, 9000 + rowid, None, remote_id),
    )
    conn.commit()
    conn.close()


def _write_ews_partial(mail_dir: Path, rowid: int,
                       message_id: str | None) -> None:
    """A headers-only .partial.emlx in the EWS account tree — exactly the
    file Mail leaves when it has not downloaded a body."""
    mid = f"Message-ID: {message_id}\n" if message_id else ""
    rfc = (f"From: a@cern.ch\nTo: b@cern.ch\nSubject: p {rowid}\n{mid}"
           f"Content-Type: text/plain; charset=utf-8\n\n").encode()
    rel = emlx_relpath_for_rowid(rowid).with_name(f"{rowid}.partial.emlx")
    path = mail_dir / EWS_ACCT / "Inbox.mbox" / EWS_INNER / "Data" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_emlx(rfc))


def _doc_sources() -> dict[int, str]:
    conn = _fts_db()
    try:
        return {
            int(r["rowid"]): r["source"]
            for r in conn.execute("SELECT rowid, source FROM docs")
        }
    finally:
        conn.close()


def test_backfill_fetches_exchange_bodies_and_search_covers_them(
    mail_fixture, monkeypatch,
):
    """The whole point: a body Mail never downloaded becomes searchable,
    with provenance recorded, keyed by the Message-ID from the partial
    file's own headers."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 500, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 500, "<lid-500@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert _doc_statuses()[500] == "partial"

    asked = []

    def fake_fetch(self, ident, message_id):
        asked.append((ident.name, message_id))
        return {"contentType": "html", "content": "<p>secret&nbsp;plan</p>"}

    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", fake_fetch)
    out = idx.backfill()
    assert out["candidates"] == 1
    assert out["backfilled"] == 1
    assert asked == [("main", "<lid-500@cern.ch>")]
    assert _doc_statuses()[500] == "indexed"
    assert _doc_sources()[500] == "graph"
    assert idx.rowids_matching("secret") == [500]
    assert idx.backfilled_text(500).startswith("secret")
    st = idx.status()
    assert st["docs"]["backfilled"] == 1
    assert st["last_backfill_at"] is not None if "last_backfill_at" in st \
        else True


def test_backfill_confirmed_miss_is_never_asked_again(mail_fixture,
                                                      monkeypatch):
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 501, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 501, "<gone-501@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    calls = []
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: calls.append(mid) or None)
    out = idx.backfill()
    assert out["misses"] == 1
    assert _doc_sources()[501] == "graph_miss"
    out = idx.backfill()
    assert out["candidates"] == 0          # stamped, not re-asked
    assert len(calls) == 1


def test_backfill_stamps_unaskable_docs_once(
    mail_fixture, monkeypatch,
):
    """Gmail partials are not Graph's to answer, and a partial with no
    Message-ID has no join key ever — both are stamped 'graph_none'
    ONCE (local evidence, independent of identities), so no later pass
    re-walks the estate to re-conclude it."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 502, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 502, None)      # no Message-ID
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    # A non-Exchange partial: hand-write the ledger row (the imap tree
    # layout is irrelevant to the gate — the mailbox URL decides).
    _add_envelope_row_in(mail_fixture, 503, 2)       # imap:// mailbox
    conn = _fts_db()
    conn.execute("INSERT INTO docs(rowid, status, attempts, last_attempt, "
                 "bytes, source) VALUES (503, 'partial', 1, 0, 0, 'local')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: pytest.fail("no fetch for these"))
    out = idx.backfill()
    # 2 = the imap partial (503, no imap identity configured) + the
    # fixture's local-mailbox missing doc (200), now also weighed by
    # the storeless class.
    assert out["no_lane"] == 2
    assert out["no_message_id"] == 1
    assert _doc_sources()[502] == "graph_none"
    assert _doc_sources()[503] == "graph_none"
    # Concluded once: the next pass derives no work from them.
    out = idx.backfill()
    assert out["no_lane"] == 0
    assert out["no_message_id"] == 0


def test_backfill_imap_lane_fetches_gmail_bodies(mail_fixture, monkeypatch):
    """The Gmail 15k-partials case (2026-08-24): an imap:// partial is
    recovered through a declared [name.imap] identity, keyed by the
    same Message-ID, indexed with source='imap', and served by
    backfilled_text exactly like a graph hit."""
    _add_envelope_row_in(mail_fixture, 520, 2)       # [Gmail]/All Mail
    _write_imap_partial(mail_fixture, 520, "<gm-520@mail.gmail.com>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert _doc_statuses()[520] == "partial"

    asked = []

    def fake_fetch(self, ident, message_id):
        asked.append((ident.name, message_id))
        return {"contentType": "html", "content": "<p>gmail&nbsp;truth</p>"}

    monkeypatch.setattr(FtsIndex, "_graph_identities", lambda self: [])
    monkeypatch.setattr(FtsIndex, "_imap_identities",
                        lambda self: [_ImapIdent()])
    monkeypatch.setattr(FtsIndex, "_fetch_imap_body", fake_fetch)
    out = idx.backfill()
    assert out["backfilled"] == 1
    assert asked == [("gmail", "<gm-520@mail.gmail.com>")]
    assert _doc_statuses()[520] == "indexed"
    assert _doc_sources()[520] == "imap"
    assert idx.rowids_matching("gmail truth") == [520]
    assert idx.backfilled_text(520).startswith("gmail")
    assert idx.status()["docs"]["backfilled"] == 1


def test_backfill_imap_confirmed_miss_is_never_asked_again(mail_fixture,
                                                           monkeypatch):
    _add_envelope_row_in(mail_fixture, 521, 2)
    _write_imap_partial(mail_fixture, 521, "<gone-521@mail.gmail.com>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    calls = []
    monkeypatch.setattr(FtsIndex, "_graph_identities", lambda self: [])
    monkeypatch.setattr(FtsIndex, "_imap_identities",
                        lambda self: [_ImapIdent()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_imap_body",
        lambda self, ident, mid: calls.append(mid) or None)
    out = idx.backfill()
    assert out["misses"] == 1
    assert _doc_sources()[521] == "imap_miss"
    out = idx.backfill()
    assert out["candidates"] == 0          # stamped, not re-asked
    assert len(calls) == 1


def test_new_imap_lane_revokes_graph_none_and_recovers(mail_fixture,
                                                       monkeypatch):
    """Yesterday's "no lane can ask" must not outlive today's lanes: a
    doc stamped graph_none under a graph-only setup is revoked and
    FETCHED once an imap identity appears (the live estate's 14,916
    Gmail partials were stamped exactly this way)."""
    _add_envelope_row_in(mail_fixture, 522, 2)
    _write_imap_partial(mail_fixture, 522, "<back-522@mail.gmail.com>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: pytest.fail("not graph's to answer"))
    idx.backfill()                          # graph-only: no lane for 522
    assert _doc_sources()[522] == "graph_none"

    monkeypatch.setattr(FtsIndex, "_imap_identities",
                        lambda self: [_ImapIdent()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_imap_body",
        lambda self, ident, mid: {"contentType": "text",
                                  "content": "revoked and recovered"})
    out = idx.backfill()
    assert out["backfilled"] == 1
    assert _doc_sources()[522] == "imap"
    assert idx.backfilled_text(522) == "revoked and recovered"


def test_crawler_never_clobbers_an_imap_body(mail_fixture, monkeypatch):
    """The partial file is still on disk after an imap backfill; a
    recrawl of that rowid must keep the server body, exactly as it
    does for graph."""
    _add_envelope_row_in(mail_fixture, 523, 2)
    _write_imap_partial(mail_fixture, 523, "<keep-523@mail.gmail.com>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities", lambda self: [])
    monkeypatch.setattr(FtsIndex, "_imap_identities",
                        lambda self: [_ImapIdent()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_imap_body",
        lambda self, ident, mid: {"contentType": "text",
                                  "content": "server body stays"})
    idx.backfill()
    assert _doc_sources()[523] == "imap"
    conn = idx._open_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        url = f"imap://{IMAP_ACCT}/%5BGmail%5D/All%20Mail"
        assert idx._index_one(conn.cursor(), 523, url) == "indexed"
        conn.commit()
    finally:
        conn.close()
    assert _doc_sources()[523] == "imap"
    assert idx.backfilled_text(523) == "server body stays"


def test_backfill_without_any_identity_is_a_soft_skip(mail_fixture,
                                                      monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    monkeypatch.setattr(FtsIndex, "_graph_identities", lambda self: [])
    monkeypatch.setattr(FtsIndex, "_imap_identities", lambda self: [])
    assert idx.backfill() == {"candidates": 0, "backfilled": 0,
                              "misses": 0, "no_message_id": 0,
                              "no_remote_id": 0,
                              "no_lane": 0,
                              "deferred": 0,
                              "skipped": "no_backfill_identity"}


def test_backfill_recovers_storeless_exchange_docs(mail_fixture,
                                                   monkeypatch):
    """The 97% case (live CERN account, 2026-08-06): Envelope rows with
    NO file at all. The EWS remote_id is translated to a Graph REST id
    in bulk, the body fetched directly, and search covers it."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 510, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-510")
    _add_envelope_row_in(mail_fixture, 511, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-511")
    _add_envelope_row_in(mail_fixture, 512, EWS_MBOX_ROWID)  # no remote_id
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    assert _doc_statuses()[510] == "missing"

    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    translated = []

    def fake_translate(self, ident, ews_ids):
        translated.append(sorted(ews_ids))
        return {"AAMkAD-510": "REST-510"}     # 511 untranslatable

    def fake_fetch_by_id(self, ident, rest_id):
        assert rest_id == "REST-510"
        return {"contentType": "text", "content": "storeless treasure"}

    monkeypatch.setattr(FtsIndex, "_translate_ews_ids", fake_translate)
    monkeypatch.setattr(FtsIndex, "_fetch_remote_body_by_id",
                        fake_fetch_by_id)
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: pytest.fail("no partials in this test"))
    out = idx.backfill()
    assert translated == [["AAMkAD-510", "AAMkAD-511"]]
    assert out["backfilled"] == 1
    assert out["misses"] == 1                 # untranslatable → stamped
    assert out["no_remote_id"] == 1           # 512: counted, NOT stamped
    assert _doc_statuses()[510] == "indexed"
    assert _doc_sources()[510] == "graph"
    assert _doc_sources()[511] == "graph_miss"
    assert _doc_sources()[512] == "local"
    assert idx.rowids_matching("treasure") == [510]
    assert idx.backfilled_text(510) == "storeless treasure"

    # Converged: the second pass has only the un-keyed doc left over.
    out = idx.backfill()
    assert out["candidates"] == 0 and out["no_remote_id"] == 1


def test_backfill_storeless_404_is_a_confirmed_miss(mail_fixture,
                                                    monkeypatch):
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 513, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-513")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, "_translate_ews_ids",
                        lambda self, ident, ids: {"AAMkAD-513": "REST-513"})
    monkeypatch.setattr(FtsIndex, "_fetch_remote_body_by_id",
                        lambda self, ident, rest: None)     # 404
    out = idx.backfill()
    assert out["misses"] == 1
    assert _doc_sources()[513] == "graph_miss"


def test_retry_missing_never_unstamps_a_graph_miss(mail_fixture,
                                                   monkeypatch):
    """graph_miss rows keep status 'missing', so _retry_missing re-stats
    them forever — the re-stat must not reset the stamp to 'local' or
    every sync would re-ask Graph for mail Exchange confirmed gone."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 514, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-514")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, "_translate_ews_ids",
                        lambda self, ident, ids: {})
    idx.backfill()
    assert _doc_sources()[514] == "graph_miss"

    conn = _fts_db()
    conn.execute("UPDATE docs SET last_attempt = 0 WHERE rowid = 514")
    conn.commit()
    conn.close()
    idx.incremental()                          # runs _retry_missing
    assert _doc_sources()[514] == "graph_miss"  # stamp survived
    assert _doc_statuses()[514] == "missing"


def test_backfill_graph_trouble_defers_never_stamps(mail_fixture,
                                                    monkeypatch):
    """Throttle or outage: the doc is DEFERRED untouched — absence of
    evidence must never read as evidence of absence — and the trouble
    becomes visible state (meta last_backfill_error), not a log line.
    A later clean pass clears the note."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 504, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 504, "<t-504@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])

    def boom(self, ident, mid):
        raise RuntimeError("HTTP 429 — deferring")

    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", boom)
    out = idx.backfill()
    assert out["deferred"] == 1
    assert out["backfilled"] == 0
    assert out["identity_errors"] == {"main": 1}
    assert _doc_sources()[504] == "local"            # still a candidate
    assert "429" in idx.status()["last_backfill_error"]

    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: {"contentType": "text",
                                  "content": "late but here"})
    out = idx.backfill()
    assert out["backfilled"] == 1
    assert idx.status()["last_backfill_error"] is None


def test_backfill_retires_a_dead_identity_and_aborts(mail_fixture,
                                                     monkeypatch):
    """An identity that keeps erroring and has answered NOTHING is dead
    for the pass after _IDENT_FAIL_FAST sightings (revoked token, not a
    poisoned doc); with no identity left the pass aborts — every doc
    still unstamped, nothing burned on a hopeless night."""
    _add_ews_mailbox(mail_fixture)
    for rid in range(530, 535):
        _add_envelope_row_in(mail_fixture, rid, EWS_MBOX_ROWID)
        _write_ews_partial(mail_fixture, rid, f"<t-{rid}@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    calls = []

    def boom(self, ident, mid):
        calls.append(mid)
        raise RuntimeError("invalid_grant: token revoked")

    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", boom)
    out = idx.backfill()
    assert out["aborted"].startswith("every backfill identity failing")
    assert len(calls) == fts._IDENT_FAIL_FAST        # not one per doc
    assert out["deferred"] == fts._IDENT_FAIL_FAST
    assert all(_doc_sources()[rid] == "local" for rid in range(530, 535))


def _mk_ident(name: str) -> _Ident:
    i = _Ident()
    i.name = name
    return i


def test_backfill_second_identity_serves_when_the_first_fails(
    mail_fixture, monkeypatch,
):
    """One broken identity must not hide the others (the transports-check
    rule, applied to backfill): the healthy mailbox still answers, and
    the broken one's trouble is recorded."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 535, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 535, "<t-535@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_mk_ident("broken"), _mk_ident("ok")])

    def fetch(self, ident, mid):
        if ident.name == "broken":
            raise RuntimeError("invalid_grant: token revoked")
        return {"contentType": "text", "content": "second lane answers"}

    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", fetch)
    out = idx.backfill()
    assert out["backfilled"] == 1
    assert out["identity_errors"] == {"broken": 1}
    assert "aborted" not in out
    assert _doc_sources()[535] == "graph"


def test_backfill_miss_needs_every_identity_to_answer(
    mail_fixture, monkeypatch,
):
    """A graph_miss stamp requires EVERY identity to have answered: when
    one errored, 'the others did not have it' is absence of evidence,
    not evidence of absence — the doc defers."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 536, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 536, "<t-536@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_mk_ident("broken"), _mk_ident("ok")])

    def fetch(self, ident, mid):
        if ident.name == "broken":
            raise RuntimeError("HTTP 503")
        return None                                  # confirmed empty here

    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", fetch)
    out = idx.backfill()
    assert out["misses"] == 0
    assert out["deferred"] == 1
    assert _doc_sources()[536] == "local"


def test_changing_the_identity_set_revokes_graph_miss_stamps(
    mail_fixture, monkeypatch,
):
    """A graph_miss means 'absent from every mailbox ASKED' — it is
    scoped to the identity set. A second Exchange account added later
    must get its question, or its bodies are lost forever."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 537, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 537, "<t-537@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_mk_ident("work")])
    monkeypatch.setattr(FtsIndex, "_fetch_remote_body",
                        lambda self, ident, mid: None)   # work: not mine
    assert idx.backfill()["misses"] == 1
    assert _doc_sources()[537] == "graph_miss"

    monkeypatch.setattr(
        FtsIndex, "_graph_identities",
        lambda self: [_mk_ident("work"), _mk_ident("personal")])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: (
            {"contentType": "text", "content": "the personal mailbox has it"}
            if ident.name == "personal" else None))
    out = idx.backfill()
    assert out["backfilled"] == 1
    assert _doc_sources()[537] == "graph"


def test_backfill_reads_a_raw_8bit_or_folded_message_id(
    mail_fixture, monkeypatch,
):
    """An unencoded 8-bit byte makes stdlib hand back a Header object
    (not str), and folding can leave whitespace inside the angle-addr:
    both must still yield the join key. Stamping such a doc graph_miss
    would lose a recoverable body forever without asking Graph."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 538, EWS_MBOX_ROWID)
    rfc = (b"From: a@cern.ch\nTo: b@cern.ch\nSubject: p 538\n"
           b"Message-ID: <weird-\xe9-538@\n cern.ch>\n"
           b"Content-Type: text/plain; charset=utf-8\n\n")
    rel = emlx_relpath_for_rowid(538).with_name("538.partial.emlx")
    path = mail_fixture / EWS_ACCT / "Inbox.mbox" / EWS_INNER / "Data" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_emlx(rfc))
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    asked = []
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])

    def fetch(self, ident, mid):
        asked.append(mid)
        return {"contentType": "text", "content": "recovered after all"}

    monkeypatch.setattr(FtsIndex, "_fetch_remote_body", fetch)
    out = idx.backfill()
    assert out["no_message_id"] == 0
    assert out["backfilled"] == 1
    assert len(asked) == 1
    assert asked[0].startswith("<weird-") and asked[0].endswith("@cern.ch>")
    assert not any(c.isspace() for c in asked[0])


def test_unreadable_partial_defers_instead_of_stamping(
    mail_fixture, monkeypatch,
):
    """A partial file that cannot be read right now (Mail rewriting it,
    momentary TCC denial) is absence of evidence: defer, never stamp."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 539, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 539, "<t-539@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: pytest.fail("unreadable file, no lookup"))
    monkeypatch.setattr(
        "email_mcp.fts.FtsIndex._message_id_from_partial",
        lambda self, rid, url: (_ for _ in ()).throw(OSError("mid-write")))
    out = idx.backfill()
    assert out["deferred"] == 1
    assert out["no_message_id"] == 0
    assert _doc_sources()[539] == "local"            # still a candidate


def test_stamped_docs_age_out_of_the_retry_queue(mail_fixture, monkeypatch):
    """A stamped doc's re-stat RECORDS the attempt: frozen bookkeeping
    kept stamped docs permanently 'due' at the HEAD of the retry queue,
    starving every genuinely late-materializing body (P04, again)."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 545, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-545")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, "_translate_ews_ids",
                        lambda self, ident, ids: {})
    idx.backfill()
    assert _doc_sources()[545] == "graph_miss"

    conn = _fts_db()
    conn.execute("UPDATE docs SET last_attempt = 0, attempts = 1 "
                 "WHERE rowid = 545")
    conn.commit()
    conn.close()
    idx.incremental()                          # runs _retry_missing
    conn = _fts_db()
    row = conn.execute("SELECT attempts, last_attempt, source FROM docs "
                       "WHERE rowid = 545").fetchone()
    conn.close()
    assert row["source"] == "graph_miss"       # the stamp survived
    assert row["attempts"] == 2                # ...and the attempt counted
    assert row["last_attempt"] > 0


def test_partial_recrawl_never_clobbers_a_graph_body(mail_fixture,
                                                     monkeypatch):
    """Local truth wins but never regresses: re-indexing the still-partial
    file keeps the graph body; the full .emlx arriving takes over and
    resets provenance to local."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 505, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 505, "<keep-505@cern.ch>")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body",
        lambda self, ident, mid: {"contentType": "text",
                                  "content": "remote body wins"})
    idx.backfill()
    assert idx.rowids_matching("remote") == [505]

    url = f"ews://{EWS_ACCT}/Inbox"
    conn = idx._open_rw()
    try:
        assert idx._begin_immediate(conn)
        assert idx._index_one(conn.cursor(), 505, url) == "indexed"
        conn.commit()
    finally:
        conn.close()
    assert _doc_sources()[505] == "graph"            # partial did not clobber
    assert idx.rowids_matching("remote") == [505]

    # The real body lands: local truth takes over.
    rel = emlx_relpath_for_rowid(505)
    data = mail_fixture / EWS_ACCT / "Inbox.mbox" / EWS_INNER / "Data"
    (data / rel.with_name("505.partial.emlx")).unlink()
    rfc = (b"From: a@cern.ch\nSubject: p 505\n"
           b"Content-Type: text/plain\n\nlocal body lands")
    (data / rel).write_bytes(_make_emlx(rfc))
    conn = idx._open_rw()
    try:
        assert idx._begin_immediate(conn)
        assert idx._index_one(conn.cursor(), 505, url) == "indexed"
        conn.commit()
    finally:
        conn.close()
    assert _doc_sources()[505] == "local"
    assert idx.rowids_matching("local") == [505]
    assert idx.backfilled_text(505) is None          # provenance moved on


def test_v1_db_migrates_in_place_to_v2(mail_fixture):
    """An existing index gains docs.source without a rebuild; every
    pre-migration row reads 'local' — exactly where its text came from."""
    from email_mcp import state

    path = state.State.resolve().adopt().fts / "fts.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE docs(rowid INTEGER PRIMARY KEY, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt REAL NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX docs_status ON docs(status);
        CREATE VIRTUAL TABLE body_fts USING fts5(
            body, tokenize = 'unicode61 remove_diacritics 2');
        INSERT INTO meta VALUES('schema_version', '1');
        INSERT INTO docs(rowid, status, attempts, last_attempt, bytes)
            VALUES (100, 'indexed', 1, 0, 10);
    """)
    conn.commit()
    conn.close()
    idx = FtsIndex(mail_base=mail_fixture)
    idx.incremental()
    st = idx.status()
    assert st["schema_version"] == 2
    assert _doc_sources()[100] == "local"


def test_sync_folds_a_backfill_pass(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    monkeypatch.setattr(
        FtsIndex, "backfill",
        lambda self, max_docs=None: {"skipped": "no_graph_identity"})
    out = fts._sync(idx, None)
    assert out["backfill"] == {"skipped": "no_graph_identity"}


# --------------------------------------------------------------------- #
# rebuild — server-fetched bodies survive (2026-08-07)                  #
# --------------------------------------------------------------------- #


def _backfill_one(mail_fixture, monkeypatch, rowid: int, text: str) -> FtsIndex:
    """Seed one storeless EWS doc and backfill it with `text`."""
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, rowid, EWS_MBOX_ROWID,
                         remote_id=f"AAMkAD-{rowid}")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(
        FtsIndex, "_translate_ews_ids",
        lambda self, ident, ids: {i: f"REST-{i}" for i in ids})
    monkeypatch.setattr(
        FtsIndex, "_fetch_remote_body_by_id",
        lambda self, ident, rest: {"contentType": "text", "content": text})
    assert idx.backfill()["backfilled"] == 1
    return idx


def test_rebuild_carries_graph_bodies_across(mail_fixture, monkeypatch):
    """Graph rows are primary data, not derived state: a rebuild must
    not cost an overnight of re-fetching what the index already holds."""
    idx = _backfill_one(mail_fixture, monkeypatch, 520, "salvage me")
    out = idx.rebuild()
    assert out["salvaged"] == 1
    assert "salvage_skipped" not in out
    assert _doc_statuses()[520] == "indexed"
    assert _doc_sources()[520] == "graph"
    assert idx.rowids_matching("salvage") == [520]
    assert idx.backfilled_text(520) == "salvage me"
    assert not fts.db_path().with_name(
        fts.db_path().name + ".rebuild").exists()


def test_rebuild_prefers_a_fresh_local_body_over_the_old_graph_one(
    mail_fixture, monkeypatch,
):
    """The full .emlx landed since the backfill: local truth wins the
    rebuild, and provenance returns to 'local'."""
    idx = _backfill_one(mail_fixture, monkeypatch, 521, "old server text")
    rel = emlx_relpath_for_rowid(521)
    path = (mail_fixture / EWS_ACCT / "Inbox.mbox" / EWS_INNER / "Data"
            / rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    rfc = (b"From: a@cern.ch\nSubject: p 521\n"
           b"Content-Type: text/plain\n\nfresh local body")
    path.write_bytes(_make_emlx(rfc))
    out = idx.rebuild()
    assert out["salvaged"] == 0
    assert _doc_sources()[521] == "local"
    assert idx.rowids_matching("fresh") == [521]
    assert idx.backfilled_text(521) is None


def test_rebuild_carries_graph_miss_stamps(mail_fixture, monkeypatch):
    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 522, EWS_MBOX_ROWID,
                         remote_id="AAMkAD-522")
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, "_graph_identities",
                        lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, "_translate_ews_ids",
                        lambda self, ident, ids: {})
    assert idx.backfill()["misses"] == 1
    idx.rebuild()
    assert _doc_sources()[522] == "graph_miss"   # not re-asked next pass
    assert idx.backfill()["candidates"] == 0


def test_rebuild_of_a_corrupt_db_skips_salvage_and_stays_clean(
    mail_fixture,
):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    fts.db_path().write_text("garbage where an SQLite header should be")
    out = idx.rebuild()
    assert "salvage_skipped" in out
    assert out["indexed"] == 3                   # the rebuild itself worked
    assert idx.status()["state"] == "ready"


def test_interrupted_rebuild_leaves_the_live_index_untouched(
    mail_fixture, monkeypatch,
):
    """The live db is never the casualty: a rebuild that dies at ANY
    point leaves it serving — and the NEXT rebuild still salvages every
    graph body (an interrupted-then-retried rebuild used to clobber its
    own salvage source and destroy them all)."""
    idx = _backfill_one(mail_fixture, monkeypatch, 523, "survives crashes")
    real_build = FtsIndex.build
    armed = {"on": True}

    def dying_build(self, limit=None):
        out = real_build(self, limit=limit)
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("power cut")
        return out

    monkeypatch.setattr(FtsIndex, "build", dying_build)
    with pytest.raises(RuntimeError):
        idx.rebuild()
    assert idx.backfilled_text(523) == "survives crashes"   # live intact
    assert idx.rowids_matching("survives") == [523]

    out = idx.rebuild()
    assert out["salvaged"] == 1
    assert idx.backfilled_text(523) == "survives crashes"
    assert not fts.db_path().with_name(
        fts.db_path().name + ".rebuild").exists()


def test_rebuild_promote_defers_to_a_busy_writer(mail_fixture, monkeypatch):
    """A writer mid-commit must not have its transaction swapped out
    from under it: the promote treats busy exactly like every other
    writer here — skip, report, leave the live index untouched."""
    idx = _backfill_one(mail_fixture, monkeypatch, 524, "busy text")
    monkeypatch.setattr(fts, "_BUSY_TIMEOUT_MS", 50)
    blocker = sqlite3.connect(fts.db_path())
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    try:
        out = idx.rebuild()
    finally:
        blocker.rollback()
        blocker.close()
    assert out["skipped"] == "busy"
    assert idx.backfilled_text(524) == "busy text"          # live intact

    out = idx.rebuild()                        # writer gone: promote lands
    assert "skipped" not in out
    assert out["salvaged"] == 1
    assert idx.backfilled_text(524) == "busy text"
