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
    from email_mcp import bootstrap, server

    target = tmp_path / "state-never-created"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(target))
    monkeypatch.setattr(
        bootstrap, "_application",
        bootstrap.build_application(
            source=AppleMailSource(mail_base=mail_fixture)
        ),
    )

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


def test_parse_emlx_survives_a_header_that_crashes_cpython(tmp_path):
    """CPython's header parser ITSELF can raise on hostile values — a
    truncated never-closed Message-ID ("<[54f4…", no ">") IndexErrors in
    get_msg_id. Found on 8 live messages by RC P04 (2026-08-02): the fts
    index marked them permanently `error` and get_email crashed to the
    belt. The body must never be hostage to one malformed header."""
    from email_mcp.sources.apple_mail import _parse_emlx

    rfc822 = (b"From: a@example.org\r\n"
              b"To: b@example.org\r\n"
              b"Subject: hostile message-id\r\n"
              b"Message-ID: <[54f4c5ade13347b4ab85403832aac1ce-TRUNCATED\r\n"
              b"Content-Type: text/plain; charset=utf-8\r\n"
              b"\r\n"
              b"The body still matters.\r\n")
    emlx = tmp_path / "1.emlx"
    emlx.write_bytes(str(len(rfc822)).encode() + b"\n" + rfc822)
    parsed = _parse_emlx(emlx, 512 * 1024)
    assert "The body still matters." in parsed["body_text"]
    assert parsed["headers"]["Subject"] == "hostile message-id"
    assert "54f4c5ade" in parsed["headers"]["Message-ID"]  # raw, served


# --------------------------------------------------------------------- #
# attachments — the sender never names a path, attached mail stays mail #
# --------------------------------------------------------------------- #


def _replace_emlx(mail_fixture, rowid, msg):
    from conftest import _make_emlx
    next(mail_fixture.rglob(f"{rowid}.emlx")).write_bytes(_make_emlx(msg.as_bytes()))


def test_get_attachment_filename_never_names_a_path(
        mail_fixture, tmp_path, monkeypatch):
    """Codex storage_repro ATTACHMENT_ABSOLUTE_PATH (2026-09-12): the
    sender's MIME filename was joined onto the extraction dir, so an
    absolute name discarded the dir and overwrote the victim, and `../`
    escaped it. The local name is generated; the sender's stays in
    `blob.name` as display metadata."""
    from email.message import EmailMessage
    from pathlib import Path

    atts = tmp_path / "atts"
    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(atts))
    victim = tmp_path / "outside-extraction-root.txt"
    victim.write_text("original harmless fixture")
    src = AppleMailSource(mail_base=mail_fixture)
    cases = {
        str(victim): "outside-extraction-root.txt",
        "../../outside-extraction-root.txt": "outside-extraction-root.txt",
        "..": "attachment",
        ".hidden": "hidden",
    }
    for hostile, local in cases.items():
        msg = EmailMessage()
        msg.set_content("outer body")
        msg.add_attachment(b"replaced by attachment", maintype="application",
                           subtype="octet-stream", filename=hostile)
        _replace_emlx(mail_fixture, 101, msg)
        att = src.get("101").attachments[0]
        blob = src.attachment("101", att.attachment_id)
        out = Path(blob.path)
        assert out.is_relative_to(atts)
        assert out.name == local
        assert out.read_bytes() == b"replaced by attachment"
        assert blob.name == hostile
    assert victim.read_text() == "original harmless fixture"


def test_get_attachment_refuses_to_write_through_a_symlink(
        mail_fixture, tmp_path, monkeypatch):
    """A symlink planted at the (predictable) target path must not be
    followed: the write is refused and the link's target untouched."""
    from pathlib import Path

    import pytest

    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(tmp_path / "atts"))
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    src = AppleMailSource(mail_base=mail_fixture)
    att_id = src.get("101").attachments[0].attachment_id
    target = Path(src.attachment("101", att_id).path)
    target.unlink()
    target.symlink_to(victim)
    with pytest.raises(OSError):
        src.attachment("101", att_id)
    assert victim.read_text() == "untouched"


def test_attached_message_is_an_attachment_not_body(
        mail_fixture, tmp_path, monkeypatch):
    """Codex storage_repro RFC822_ATTACHMENT (2026-09-12): a message/rfc822
    part is multipart under Python's model, so the walkers descended into
    it — the attached mail vanished from the list and its text merged
    into the enclosing body. It is one attachment, materialised as .eml."""
    import email
    import email.policy
    from email.message import EmailMessage
    from pathlib import Path

    monkeypatch.setenv("EMAIL_MCP_ATTACH_DIR", str(tmp_path / "atts"))
    outer = EmailMessage()
    outer.set_content("outer body only")
    inner = EmailMessage()
    inner["Subject"] = "attached message"
    inner.set_content("attached body should be downloadable")
    outer.add_attachment(inner, filename="forwarded.eml")
    _replace_emlx(mail_fixture, 101, outer)

    src = AppleMailSource(mail_base=mail_fixture)
    m = src.get("101")
    assert [(a.name, a.mime) for a in m.attachments] == [
        ("forwarded.eml", "message/rfc822")]
    assert "outer body only" in m.body_text
    assert "attached body should be downloadable" not in m.body_text

    blob = src.attachment("101", m.attachments[0].attachment_id)
    assert blob.name == "forwarded.eml"
    assert blob.mime == "message/rfc822"
    saved = email.message_from_bytes(Path(blob.path).read_bytes(),
                                     policy=email.policy.default)
    assert saved["Subject"] == "attached message"
    assert "attached body should be downloadable" in saved.get_content()

    # Without a filename the attached message still gets an .eml name.
    outer = EmailMessage()
    outer.set_content("outer body only")
    outer.add_attachment(inner)
    _replace_emlx(mail_fixture, 101, outer)
    m = src.get("101")
    assert [(a.name, a.mime) for a in m.attachments] == [
        ("attachment-1.eml", "message/rfc822")]
    assert "attached body should be downloadable" not in m.body_text


def test_source_usable_from_any_thread(mail_fixture):
    """The MCP SDK runs sync tool handlers on anyio's worker pool, whose
    threads are pruned after 10 s idle and respawned under load. A single
    cached sqlite3 connection is bound to the thread that opened it, so
    the first call from a different worker used to raise ProgrammingError
    ("SQLite objects created in a thread can only be used in that same
    thread") and every later call kept failing until restart. Reads must
    succeed from whichever thread happens to serve the request."""
    import threading

    src = AppleMailSource(mail_base=mail_fixture)
    # Warm the creator thread's connection first, as the server does.
    assert [r.id for r in src.recent(None, None, limit=10)] == ["101", "100", "200", "300"]

    results: list[list[str]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append([r.id for r in src.recent(None, None, limit=10)])
            results.append([r.id for r in src.search(SearchQuery(query="I2C"))])
            results.append(sorted(b.name for b in src.mailboxes()))
        except BaseException as exc:  # noqa: BLE001 — surface everything
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert results.count(["101", "100", "200", "300"]) == 3
    assert results.count(["100"]) == 3
    # And the creator thread still works after the others have run.
    assert [r.id for r in src.search(SearchQuery(query="I2C"))] == ["100"]


def _open_fds() -> int:
    import os
    for d in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(d))
        except OSError:
            continue
    pytest.skip("no per-process fd listing on this platform")


def test_retired_workers_close_their_connections_without_gc(mail_fixture):
    """anyio prunes idle workers; each one's Envelope Index connection
    must go with it AT ONCE. A sqlite3.Connection carries reference
    cycles, so a bare thread-local reference would leave the descriptor
    to the cyclic collector — retired workers' handles lingering, and
    under a low fd limit "unable to open database file". The slot owns
    the connection and closes it deterministically; measured here with
    cyclic GC disabled, so nothing but refcounting can be doing it."""
    import gc
    import threading

    src = AppleMailSource(mail_base=mail_fixture)
    assert [r.id for r in src.recent(None, None, limit=1)] == ["101"]

    gc.disable()
    try:
        gc.collect()
        baseline = _open_fds()
        workers = [threading.Thread(
            target=lambda: src.recent(None, None, limit=1)) for _ in range(20)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=10)
        assert _open_fds() == baseline
    finally:
        gc.enable()
    # And the creator thread's own connection is untouched.
    assert [r.id for r in src.recent(None, None, limit=1)] == ["101"]


def test_concurrent_searches_keep_their_own_fts_report(mail_fixture):
    """Two workers search at once; the application reads the index
    report (fts hits, body_match) right after search() on its own
    thread. That report used to be one attribute on the shared source:
    worker A's search returns, worker B's search overwrites the stash,
    A's response then says hits=0 / body_match=False for a hit it did
    find. Deterministic interleaving via events, asserted on the
    application response — the thing the client sees."""
    import threading

    from email_mcp.application.reads import ReadUseCases
    from email_mcp.fts import FtsIndex

    FtsIndex(mail_base=mail_fixture).build()
    src = AppleMailSource(mail_base=mail_fixture)

    class Provider:
        def get(self):
            return src

    class Classifier:
        @staticmethod
        def classify(error):
            return "internal_error"

    reads = ReadUseCases(source=Provider(), refresh=object(),
                         classifier=Classifier())

    a_searched = threading.Event()   # A's source.search() has returned
    b_finished = threading.Event()   # B's whole search_emails() is done
    real_search = src.search

    def interleaved_search(q):
        hits = real_search(q)
        if q.query == "retracted":
            a_searched.set()         # ...now let B run to completion
            assert b_finished.wait(10)
        return hits

    src.search = interleaved_search  # instance seam, this test only
    pages: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker(name, query, gate=None):
        try:
            if gate is not None:
                assert gate.wait(10)
            pages[name] = reads.search_emails(query=query, limit=10)
        except BaseException as exc:  # noqa: BLE001 — surface everything
            errors.append(exc)
        finally:
            if name == "B":
                b_finished.set()

    a = threading.Thread(target=worker, args=("A", "retracted"))
    b = threading.Thread(target=worker, args=("B", "doesnotexistunique", a_searched))
    a.start()
    b.start()
    a.join(timeout=15)
    b.join(timeout=15)

    assert errors == []
    page_a, page_b = pages["A"], pages["B"]
    assert [h.id for h in page_a.results] == ["100"]
    assert page_a.fts["hits"] == 1 and page_a.results[0].body_match is True
    assert page_b.results == [] and page_b.fts["hits"] == 0


def test_source_disposed_from_another_thread_closes_quietly(mail_fixture):
    """The teardown boundary: when the source (and with it the thread-
    local) is released on one thread while a worker that used it is
    still alive but idle, that worker's slot is destroyed on the
    releasing thread — its connection is closed cross-thread. That must
    neither raise an unraisable ProgrammingError in __del__ nor leave
    the descriptor open. (Per-worker connections are opened with
    check_same_thread=False precisely for this: the slot already makes
    cross-thread USE impossible, so the flag only permits teardown.)"""
    import gc
    import sys
    import threading

    holder = {"src": AppleMailSource(mail_base=mail_fixture)}
    used = threading.Event()
    release = threading.Event()

    def worker():
        holder["src"].recent(None, None, limit=1)
        used.set()          # holds no reference to the source from here
        release.wait(10)    # ...but stays alive, idle

    t = threading.Thread(target=worker)
    t.start()
    assert used.wait(10)

    unraisable: list = []
    hook, sys.unraisablehook = sys.unraisablehook, unraisable.append
    try:
        gc.collect()
        baseline = _open_fds()
        del holder["src"]           # last reference: source torn down here,
        gc.collect()                # on the main thread, worker still alive
        assert _open_fds() < baseline
        assert unraisable == [], [u.exc_value for u in unraisable]
    finally:
        sys.unraisablehook = hook
        release.set()
        t.join(timeout=10)
