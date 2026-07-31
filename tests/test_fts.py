"""FTS body-index tests against the fake Mail fixture tree.

Fixture facts (tests/conftest.py): rowids 100/101/300 have .emlx bodies
("retracted" / "See attached" / HTML "Hello"+"Bye" with a script `noise()`),
rowid 200 has NO .emlx. The autouse fts_dir_guard derives <root>/fts
at a per-test tmp dir.
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
    assert st["schema_version"] == 1
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
    root = tmp_path / "never-created"
    target = root / "fts"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))

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
