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


def test_backfill_skips_non_exchange_and_headerless_partials(
    mail_fixture, monkeypatch,
):
    """Gmail partials are not Graph's to answer, and a partial with no
    Message-ID has no join key ever — both must shrink the pass instead
    of erroring or looping."""
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
    # 2 = the imap partial (503) + the fixture's local-mailbox missing
    # doc (200), now also weighed by the storeless class.
    assert out["skipped_non_exchange"] == 2
    assert out["no_message_id"] == 1
    assert _doc_sources()[502] == "graph_miss"
    assert _doc_sources()[503] == "local"            # untouched


def test_backfill_without_graph_identity_is_a_soft_skip(mail_fixture,
                                                        monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    monkeypatch.setattr(FtsIndex, "_graph_identities", lambda self: [])
    assert idx.backfill() == {"candidates": 0, "backfilled": 0,
                              "misses": 0, "no_message_id": 0,
                              "no_remote_id": 0,
                              "skipped_non_exchange": 0,
                              "skipped": "no_graph_identity"}


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


def test_backfill_aborts_the_pass_on_graph_trouble(mail_fixture,
                                                   monkeypatch):
    """Throttle or outage: defer, never guess — the stamped state is
    untouched so the next nightly resumes exactly here."""
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
    assert "aborted" in out
    assert out["backfilled"] == 0
    assert _doc_sources()[504] == "local"            # still a candidate


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
        lambda self, max_docs=None, deadline=None:
            {"skipped": "no_graph_identity"})
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
        fts.db_path().name + ".pre-rebuild").exists()


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
