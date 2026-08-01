"""Triage tests: plan freeze/validation, batched-apply parsing, verification
against the real fixture Envelope Index, plan lifecycle, script rendering.

The osascript boundary is faked; the fake mutates the fixture sqlite DB
directly, so VERIFY logic runs for real against real SQL. Nothing touches
Mail.app."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from email_mcp import config, plans, triage
from email_mcp.plans import Plan, PlanAction, PlanMessage
from email_mcp.sources.apple_mail import AppleMailSource
from email_mcp.sources.base import SearchQuery

LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "0")


@pytest.fixture
def src(mail_fixture):
    return AppleMailSource(mail_base=mail_fixture)


@pytest.fixture
def db(mail_fixture):
    """Writable handle on the FIXTURE index (never the real one) so the
    fake actor can simulate Mail.app's mutations."""
    return sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")


class FakeOsa:
    """Stands in for triage._run_osascript. Pre-flight answered from
    `accounts`; everything else routed to the configurable handler."""

    def __init__(self):
        self.scripts: list[str] = []
        self.accounts = [IMAP_ACCT]
        self.preflight_rc = 0
        self.preflight_stderr = ""
        self.batch = lambda script: subprocess.CompletedProcess([], 0, "", "")

    def __call__(self, script: str, timeout: float):
        self.scripts.append(script)
        if "accountIds" in script:
            return subprocess.CompletedProcess(
                [], self.preflight_rc, "\n".join(self.accounts) + "\n",
                self.preflight_stderr,
            )
        return self.batch(script)

    @property
    def batch_scripts(self) -> list[str]:
        return [s for s in self.scripts if "accountIds" not in s]


@pytest.fixture
def fake_osa(monkeypatch):
    fake = FakeOsa()
    monkeypatch.setattr(triage, "_run_osascript", fake)
    return fake


def _plan(src, actions, **q):
    return triage.build_plan(src, SearchQuery(**q), actions)


def _add_mailbox(db, rowid, url):
    db.execute("INSERT INTO mailboxes(ROWID, url) VALUES (?,?)", (rowid, url))
    db.commit()


TRASH_MB = 50


def _seed_trash(db, *rows):
    """Add the LOCAL account's Trash mailbox, plus optional trashed copies.
    A trashed copy is exactly what Mail.app produces (field-observed
    2026-08-01): a FRESH rowid in the Trash mailbox with deleted still 0 —
    that column is Apple's purge flag, not mailbox membership."""
    _add_mailbox(db, TRASH_MB, f"local://{LOCAL_ACCT}/Trash")
    for rowid, subject, sender, conv, gmid in rows:
        db.execute(
            "INSERT INTO messages(ROWID, subject, sender, summary, date_sent,"
            " date_received, mailbox, read, flagged, deleted, conversation_id,"
            " global_message_id, flag_color)"
            " VALUES (?,?,?,?, 1714800000, 1714800100, ?, 1, 0, 0, ?, ?, NULL)",
            (rowid, subject, sender, subject, TRASH_MB, conv, gmid))
    db.commit()


# --------------------------------------------------------------------- #
# planning                                                              #
# --------------------------------------------------------------------- #


def test_plan_freezes_selection_and_file(src):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    assert plan.status == "draft" and len(plan.messages) == 1
    m = plan.messages[0]
    assert m.rowid == 100 and m.mailbox == "Inbox" and m.account == LOCAL_ACCT
    assert m.scheme == "local"
    assert m.pre == {"read": 0, "flagged": 0, "flag_color": -1}
    assert m.message_id_header == "i2c-2026-05-01@cern.ch"  # brackets stripped
    assert m.global_message_id == 9100

    on_disk = json.loads((config.plans_dir() / f"{plan.id}.json").read_text())
    assert on_disk["status"] == "draft" and on_disk["messages"][0]["rowid"] == 100
    ttl = (datetime.fromisoformat(plan.expires_at)
           - datetime.fromisoformat(plan.created_at)).total_seconds()
    assert ttl == config.triage_ttl_seconds()


def test_plan_rejects_empty_and_oversized(src, monkeypatch):
    with pytest.raises(triage.TriageError) as ei:
        _plan(src, [{"action": "mark_read"}], query="zzz-no-such-thing")
    assert ei.value.code == "empty_selection"
    assert list(config.plans_dir().glob("*.json")) == []

    monkeypatch.setenv("EMAIL_MCP_TRIAGE_MAX", "2")
    with pytest.raises(triage.TriageError) as ei:
        _plan(src, [{"action": "mark_read"}], limit=10)  # fixture has 4 msgs
    assert ei.value.code == "selection_too_large"


def test_plan_validates_actions(src):
    cases = [
        ([{"action": "explode"}], "invalid_action"),
        ([{"action": "flag", "color": 9}], "invalid_action"),
        ([{"action": "move_to"}], "invalid_action"),
        ([{"action": "mark_read"}, {"action": "mark_unread"}], "conflicting_actions"),
        ([{"action": "delete"}, {"action": "move_to", "mailbox": "X"}],
         "destructive_action"),  # delete segregated: refused before conflicts
        (None, "invalid_action"),
    ]
    for actions, code in cases:
        with pytest.raises(triage.TriageError) as ei:
            _plan(src, actions, unread_only=True)
        assert ei.value.code == code, actions
    # Legal combination: relocating action reordered last.
    parsed = triage._parse_actions(
        [{"action": "move_to", "mailbox": "X"}, {"action": "mark_read"}])
    assert [a.action for a in parsed] == ["mark_read", "move_to"]
    # The move_to+delete conflict still holds where delete IS allowed.
    with pytest.raises(triage.TriageError) as ei:
        triage._parse_actions(
            [{"action": "delete"}, {"action": "move_to", "mailbox": "X"}],
            allowed=triage.ACTIONS | triage.DESTRUCTIVE)
    assert ei.value.code == "conflicting_actions"


def test_delete_via_triage_plan_points_at_its_own_tool(src):
    with pytest.raises(triage.TriageError) as ei:
        _plan(src, [{"action": "delete"}], unread_only=True)
    assert ei.value.code == "destructive_action"
    assert "triage_plan_delete" in str(ei.value)
    assert list(config.plans_dir().glob("*.json")) == []  # nothing staged


def test_plan_rejects_unknown_mailbox_and_cross_account(src):
    with pytest.raises(triage.TriageError) as ei:
        _plan(src, [{"action": "move_to", "mailbox": "Nope"}], unread_only=True)
    assert ei.value.code == "unknown_mailbox"

    with pytest.raises(triage.TriageError) as ei:  # all 4 msgs span 2 accounts
        _plan(src, [{"action": "move_to", "mailbox": "Inbox"}], limit=10)
    assert ei.value.code == "cross_account"


def test_plan_rejects_noop_move(src):
    with pytest.raises(triage.TriageError) as ei:
        _plan(src, [{"action": "move_to", "mailbox": "Inbox"}],
              account=LOCAL_ACCT, limit=10)  # already all in Inbox
    assert ei.value.code == "noop_move"


# --------------------------------------------------------------------- #
# apply + verify                                                        #
# --------------------------------------------------------------------- #


def test_apply_mark_read_end_to_end(src, db, fake_osa):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)

    def batch(script):
        db.execute("UPDATE messages SET read=1 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied"
    assert res["acted"] == 1 and res["verified"] == 1
    assert res["failures"] == [] and res["pending"] == []
    stored = plans.load(plan.id)
    assert stored.status == "applied" and stored.result["verified"] == 1
    # local:// message → app-level specifier, no account qualifier
    assert "of account id" not in fake_osa.batch_scripts[0]


def test_apply_move_verifies_same_rowid(src, db, fake_osa):
    _add_mailbox(db, 3, f"local://{LOCAL_ACCT}/Filed")
    plan = _plan(src, [{"action": "move_to", "mailbox": "Filed"}],
                 unread_only=True)

    def batch(script):
        db.execute("UPDATE messages SET mailbox=3 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["verified"] == 1 and res["pending"] == []


def test_apply_move_verifies_reinsert(src, db, fake_osa):
    _add_mailbox(db, 3, f"local://{LOCAL_ACCT}/Filed")
    plan = _plan(src, [{"action": "move_to", "mailbox": "Filed"}],
                 unread_only=True)

    def batch(script):
        db.execute("DELETE FROM messages WHERE ROWID=100")
        db.execute(
            "INSERT INTO messages(ROWID, subject, sender, summary, date_sent,"
            " date_received, mailbox, read, flagged, deleted, conversation_id,"
            " global_message_id, flag_color)"
            " VALUES (9999, 1, 1, 1, 1714600000, 1714600100, 3, 0, 0, 0, 7001,"
            " 9100, NULL)")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["verified"] == 1 and res["pending"] == []


def test_move_to_unsynced_mailbox_falls_back_to_mail_probe(src, db, fake_osa):
    """A freshly created mailbox may exist in Mail but not yet in the
    Envelope Index (observed live) — plan via the AppleScript existence
    probe, verify as 'left the source mailbox'."""
    probe_scripts = []
    real = fake_osa.batch

    def router(script):
        if "exists mailbox" in script and "«class mssg»" not in script:
            probe_scripts.append(script)
            return subprocess.CompletedProcess([], 0, "YES\n", "")
        db.execute("UPDATE messages SET mailbox=999 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = router

    plan = _plan(src, [{"action": "move_to", "mailbox": "BrandNew"}],
                 unread_only=True)
    assert plan.target["mailbox_rowid"] is None
    assert len(probe_scripts) == 1

    res = triage.apply_plan(src, plan.id)
    assert res["verified"] == 1 and res["pending"] == []


def test_apply_delete_plan_verifies_via_deleted_flag(src, db, fake_osa):
    """build_delete_plan's output applies through the UNCHANGED apply_plan."""
    plan = triage.build_delete_plan(src, SearchQuery(unread_only=True))
    assert [a.action for a in plan.actions] == ["delete"]

    def batch(script):
        db.execute("UPDATE messages SET deleted=1 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied" and res["verified"] == 1
    assert "delete msgRef" in fake_osa.batch_scripts[0]
    assert src.search(SearchQuery(unread_only=True)) == []  # gone from search


def test_delete_plan_cross_account_rejected(src):
    with pytest.raises(triage.TriageError) as ei:  # all 4 msgs span 2 accounts
        triage.build_delete_plan(src, SearchQuery(limit=10))
    assert ei.value.code == "cross_account"
    assert "account=" in str(ei.value)
    assert list(config.plans_dir().glob("*.json")) == []  # nothing staged


def test_delete_plan_cap_names_its_env_knob(src, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_DELETE_MAX", "2")
    with pytest.raises(triage.TriageError) as ei:
        triage.build_delete_plan(  # LOCAL account holds 3 messages
            src, SearchQuery(account=LOCAL_ACCT, limit=10))
    assert ei.value.code == "selection_too_large"
    assert "EMAIL_MCP_TRIAGE_DELETE_MAX" in str(ei.value)
    assert list(config.plans_dir().glob("*.json")) == []  # nothing staged


def test_delete_plan_never_selects_the_trash(src, db):
    """The mutation pin: this dies against a selection that filters only on
    deleted=0 (the field failure of 2026-08-01 — count=43 every round)."""
    _seed_trash(db, (8300, 2, 3, 7002, 9101))  # trashed ops-bot copy
    plan = triage.build_delete_plan(
        src, SearchQuery(from_addr="ops-bot", limit=10))
    assert {m.rowid for m in plan.messages} == {101, 200}  # 8300 excluded


def test_delete_plan_converges_after_apply(src, db, fake_osa):
    """plan → apply (Mail.app trashes: fresh rowid in Trash, deleted=0) →
    re-plan the same sender: count drops by acted, trashed copies are never
    re-selected, and the last round converges to empty_selection."""
    _seed_trash(db)

    def trash(old_rowid, new_rowid, subject, conv, gmid):
        db.execute("DELETE FROM messages WHERE ROWID=?", (old_rowid,))
        db.execute(
            "INSERT INTO messages(ROWID, subject, sender, summary, date_sent,"
            " date_received, mailbox, read, flagged, deleted, conversation_id,"
            " global_message_id, flag_color)"
            " VALUES (?,?, 3, ?, 1714800000, 1714800100, ?, 1, 0, 0, ?, ?, NULL)",
            (new_rowid, subject, subject, TRASH_MB, conv, gmid))
        db.commit()
        return subprocess.CompletedProcess([], 0, f"OK {old_rowid}\n", "")

    plan = triage.build_delete_plan(
        src, SearchQuery(from_addr="ops-bot", limit=1))
    assert [m.rowid for m in plan.messages] == [101]  # newest of the two
    fake_osa.batch = lambda s: trash(101, 8101, 2, 7002, 9101)
    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied" and res["verified"] == 1

    # Round 2: count dropped by acted; the fresh Trash rowid is invisible.
    plan2 = triage.build_delete_plan(
        src, SearchQuery(from_addr="ops-bot", limit=10))
    assert [m.rowid for m in plan2.messages] == [200]
    fake_osa.batch = lambda s: trash(200, 8200, 3, 7001, 9200)
    assert triage.apply_plan(src, plan2.id)["verified"] == 1

    # Round 3: converged — nothing left to delete, ever.
    with pytest.raises(triage.TriageError) as ei:
        triage.build_delete_plan(src, SearchQuery(from_addr="ops-bot", limit=10))
    assert ei.value.code == "empty_selection"


def test_plain_search_still_finds_trashed_mail(src, db):
    """Only DELETE plans exclude the Trash — users legitimately search it."""
    _seed_trash(db, (8300, 2, 3, 7002, 9101))
    hits = src.search(SearchQuery(from_addr="ops-bot", limit=10))
    by_id = {r.id: r for r in hits}
    assert "8300" in by_id
    assert by_id["8300"].mailbox == "Trash"


def test_apply_partial_failure_reported(src, db, fake_osa):
    plan = _plan(src, [{"action": "mark_unread"}], account=LOCAL_ACCT, limit=10)
    assert len(plan.messages) == 3  # 100, 101, 200

    def batch(script):
        db.execute("UPDATE messages SET read=0 WHERE ROWID IN (100, 200)")
        db.commit()
        return subprocess.CompletedProcess(
            [], 0, "OK 100\nERR 101 applescript -1728 can't get message\nOK 200\n", "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied"
    assert res["acted"] == 2 and res["verified"] == 2
    assert res["failures"] == [
        {"id": "101", "code": "applescript",
         "detail": "applescript -1728 can't get message"}]


def test_apply_verify_pending_when_no_writethrough(src, fake_osa):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    fake_osa.batch = lambda s: subprocess.CompletedProcess([], 0, "OK 100\n", "")

    res = triage.apply_plan(src, plan.id)
    assert res["acted"] == 1 and res["verified"] == 0
    assert res["verify_polls"] == config.triage_verify_polls()
    (p,) = res["pending"]
    assert p["id"] == "100" and p["expected"] == {"read": 1}
    assert p["observed"]["read"] == 0


def test_apply_timeout_rescued_by_verify(src, db, fake_osa):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)

    def batch(script):
        db.execute("UPDATE messages SET read=1 WHERE ROWID=100")
        db.commit()
        raise subprocess.TimeoutExpired(["osascript"], 60)
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied"
    assert res["verified"] == 1 and res["acted"] == 1
    assert res["failures"] == []  # rescued: mutation landed before the kill


def test_apply_expired_double_apply_and_claim(src, db, fake_osa, monkeypatch):
    # Expired plan
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_TTL", "-5")
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan.id)
    assert ei.value.code == "plan_expired"
    assert plans.load(plan.id).status == "expired"
    monkeypatch.delenv("EMAIL_MCP_TRIAGE_TTL")

    # Double apply
    plan2 = _plan(src, [{"action": "mark_read"}], unread_only=True)

    def batch(script):
        db.execute("UPDATE messages SET read=1 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch
    assert triage.apply_plan(src, plan2.id)["status"] == "applied"
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan2.id)
    assert ei.value.code == "plan_already_applied"

    # Claim race
    db.execute("UPDATE messages SET read=0 WHERE ROWID=100")
    db.commit()
    plan3 = _plan(src, [{"action": "mark_read"}], unread_only=True)
    assert plans.claim(plan3.id) is not None  # someone else grabbed it
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan3.id)
    assert ei.value.code == "plan_claimed"

    # Unknown id
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, "nope-123")
    assert ei.value.code == "plan_not_found"


# --------------------------------------------------------------------- #
# chunked apply                                                         #
# --------------------------------------------------------------------- #


def _seed_bulk(db, n, start=1000):
    """N unread ops-bot messages in the LOCAL Inbox (rowids start…), each
    with a Message-ID header so rendered blocks carry the mid guard."""
    for i in range(n):
        rid, gmid = start + i, 9500 + i
        db.execute(
            "INSERT INTO messages(ROWID, subject, sender, summary, date_sent,"
            " date_received, mailbox, read, flagged, deleted, conversation_id,"
            " global_message_id, flag_color)"
            " VALUES (?, 3, 3, 3, ?, ?, 1, 0, 0, 0, ?, ?, NULL)",
            (rid, 1714810000 + i, 1714810100 + i, 8000 + i, gmid))
        db.execute(
            "INSERT INTO message_global_data(ROWID, message_id_header)"
            " VALUES (?, ?)", (gmid, f"<bulk-{i}@cern.ch>"))
    db.commit()
    return set(range(start, start + n))


def _script_ids(script):
    return [int(x) for x in re.findall(r"«class mssg» id (\d+)", script)]


def _bulk_plan(src, db, n):
    seeded = _seed_bulk(db, n)
    plan = _plan(src, [{"action": "mark_read"}], from_addr="ops-bot",
                 unread_only=True, limit=50)
    assert {m.rowid for m in plan.messages} == seeded
    return plan


def _ok_and_mutate(db):
    """A chunk handler that acts on exactly the ids in the script."""
    def batch(script):
        ids = _script_ids(script)
        db.executemany("UPDATE messages SET read=1 WHERE ROWID=?",
                       [(i,) for i in ids])
        db.commit()
        return subprocess.CompletedProcess(
            [], 0, "".join(f"OK {i}\n" for i in ids), "")
    return batch


def test_apply_chunks_the_batch(src, db, fake_osa):
    """12 messages → two sub-scripts of 10 + 2; every planned id appears
    in exactly one chunk and the envelope reads as one batch."""
    plan = _bulk_plan(src, db, 12)
    fake_osa.batch = _ok_and_mutate(db)

    res = triage.apply_plan(src, plan.id)
    sizes = [len(_script_ids(s)) for s in fake_osa.batch_scripts]
    assert sizes == [10, 2]
    seen = [i for s in fake_osa.batch_scripts for i in _script_ids(s)]
    assert sorted(seen) == sorted(m.rowid for m in plan.messages)
    assert res["status"] == "applied" and res["planned"] == 12
    assert res["acted"] == 12 and res["verified"] == 12
    assert res["failures"] == [] and res["pending"] == []


def test_chunk_timeout_banks_earlier_chunks_acted(src, db, fake_osa):
    """The banking pin: chunk 1's OKs survive a chunk-2 kill even before
    write-through confirms them — a monolithic batch would have relabelled
    every planned id batch_timeout and reported acted from verify alone."""
    plan = _bulk_plan(src, db, 12)
    calls = {"n": 0}

    def batch(script):
        calls["n"] += 1
        if calls["n"] == 1:  # chunk 1 acts; the index has not caught up
            ids = _script_ids(script)
            return subprocess.CompletedProcess(
                [], 0, "".join(f"OK {i}\n" for i in ids), "")
        raise subprocess.TimeoutExpired(["osascript"], 60)
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied"
    assert res["acted"] == 10 and res["verified"] == 0
    assert {int(f["id"]) for f in res["failures"]} \
        == {m.rowid for m in plan.messages[10:]}
    assert {f["code"] for f in res["failures"]} == {"batch_timeout"}
    assert len(res["pending"]) == 12  # acted + killed, none index-confirmed
    stored = plans.load(plan.id)
    assert stored.status == "applied" and stored.result["acted"] == 10


def test_accounting_accumulates_across_chunks(src, db, fake_osa):
    """Per-chunk partial failures sum into one cumulative envelope."""
    plan = _bulk_plan(src, db, 12)

    def batch(script):
        ids = _script_ids(script)
        bad, good = ids[0], ids[1:]
        db.executemany("UPDATE messages SET read=1 WHERE ROWID=?",
                       [(i,) for i in good])
        db.commit()
        out = "".join(f"OK {i}\n" for i in good)
        out += f"ERR {bad} applescript -1728 nope\n"
        return subprocess.CompletedProcess([], 0, out, "")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["acted"] == 10 and res["verified"] == 10
    assert sorted(int(f["id"]) for f in res["failures"]) \
        == sorted([plan.messages[0].rowid, plan.messages[10].rowid])
    assert {f["code"] for f in res["failures"]} == {"applescript"}


def test_timeout_detail_names_the_escape_hatches(src, fake_osa):
    def batch(script):
        raise subprocess.TimeoutExpired(["osascript"], 60)
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    (f,) = res["failures"]
    assert f["code"] == "batch_timeout"
    assert "EMAIL_MCP_TRIAGE_TIMEOUT" in f["detail"]
    assert "EMAIL_MCP_TRIAGE_DELETE_MAX" in f["detail"]
    assert res["status"] == "failed"  # nothing acted, nothing verified


def test_kill_stops_the_batch_and_reports_the_rest(src, db, fake_osa):
    """23 messages, kill on chunk 2: chunk 3 is never attempted and says
    so — a distinct code from the killed chunk's batch_timeout."""
    plan = _bulk_plan(src, db, 23)
    calls = {"n": 0}

    def batch(script):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok_and_mutate(db)(script)
        raise subprocess.TimeoutExpired(["osascript"], 60)
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert len(fake_osa.batch_scripts) == 2  # chunk 3 never rendered
    rows = [m.rowid for m in plan.messages]
    by_code: dict[str, set[int]] = {}
    for f in res["failures"]:
        by_code.setdefault(f["code"], set()).add(int(f["id"]))
    assert by_code["batch_timeout"] == set(rows[10:20])
    assert by_code["not_attempted"] == set(rows[20:])
    assert all("re-plan" in f["detail"] for f in res["failures"]
               if f["code"] == "not_attempted")
    assert res["planned"] == 23
    assert res["acted"] == 10 and res["verified"] == 10


def test_late_wholesale_chunk_failure_keeps_banked_progress(src, db, fake_osa):
    """A wholesale script failure AFTER mutations landed must not raise
    the banked work away: it becomes item data, not a plan-level error."""
    plan = _bulk_plan(src, db, 12)
    calls = {"n": 0}

    def batch(script):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok_and_mutate(db)(script)
        return subprocess.CompletedProcess(
            [], 1, "", "execution error: Mail got an error. (-609)")
    fake_osa.batch = batch

    res = triage.apply_plan(src, plan.id)
    assert res["status"] == "applied" and res["acted"] == 10
    assert {int(f["id"]) for f in res["failures"]} \
        == {m.rowid for m in plan.messages[10:]}
    assert all(f["code"] == "applescript" and "wholesale" in f["detail"]
               for f in res["failures"])


def test_first_chunk_wholesale_failure_still_raises(src, fake_osa):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    fake_osa.batch = lambda s: subprocess.CompletedProcess(
        [], 1, "", "execution error: boom (-609)")
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan.id)
    assert ei.value.code == "script_error"
    assert plans.load(plan.id).status == "failed"


class _LazySnapshotSource:
    """Snapshot resolves only from the ready_at-th call on — models Mail
    draining a killed chunk's queued work after the process died."""

    def __init__(self, ready_at):
        self.calls = 0
        self.ready_at = ready_at

    def triage_snapshot(self, rowids):
        self.calls += 1
        read = 1 if self.calls >= self.ready_at else 0
        return {rid: {"read": read, "flagged": 0, "flag_color": -1,
                      "mailbox_rowid": 1, "deleted": 0} for rid in rowids}


def test_postkill_verify_window_scales_with_chunk_budget(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "0.001")
    plan = _synthetic_plan([_msg(11, "local", "Inbox")],
                           [PlanAction("mark_read")])
    # Default window (3 polls) gives up before the drain completes…
    slow = _LazySnapshotSource(ready_at=10)
    ver = triage._verify(slow, plan, {11})
    assert ver["verified"] == [] and len(ver["pending"]) == 1
    # …the drain-sized window keeps polling and catches the late landing.
    slow = _LazySnapshotSource(ready_at=10)
    ver = triage._verify(slow, plan, {11}, window_s=0.05)
    assert ver["verified"] == [11] and ver["pending"] == []


# --------------------------------------------------------------------- #
# script rendering                                                      #
# --------------------------------------------------------------------- #


def _synthetic_plan(messages, actions, target=None):
    now = plans.utcnow()
    return Plan(
        id=plans.new_id(now), created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(seconds=600)),
        status="draft", query={}, actions=actions, target=target,
        messages=messages, summary="synthetic",
    )


def _msg(rowid, scheme, mailbox, mid="some-id@x", account=IMAP_ACCT):
    return PlanMessage(
        rowid=rowid, account=account, scheme=scheme, mailbox=mailbox,
        mailbox_rowid=1, subject="s", from_addr="a@b", date="2026-07-28",
        unread=True, message_id_header=mid, global_message_id=1,
        pre={"read": 0, "flagged": 0, "flag_color": -1},
    )


def test_script_render_escaping_and_structure():
    target = {"account": IMAP_ACCT, "mailbox": "Filed/Sub",
              "mailbox_rowid": 9, "url": f"imap://{IMAP_ACCT}/Filed/Sub"}
    plan = _synthetic_plan(
        [_msg(11, "ews", 'We"ird\\Box'), _msg(22, "local", "Inbox", mid="")],
        [PlanAction("flag", color=2), PlanAction("move_to", mailbox="Filed/Sub")],
        target,
    )
    script = triage._render_script(plan, plan.messages)
    # escaping: backslash then quote
    assert 'mailbox "We\\"ird\\\\Box" of account id' in script
    # keyed specifier, per-message try blocks
    assert "«class mssg» id 11" in script and script.count("on error") == 2
    # mid recheck present for 11, absent for the header-less 22
    assert 'does not contain "some-id@x"' in script
    assert script.count("does not contain") == 1
    # move rendered after flag
    flag_pos = script.index("set flag index")
    move_pos = script.index("move msgRef")
    assert flag_pos < move_pos
    # local message: bare mailbox specifier
    assert 'id 22 of mailbox "Inbox"\n' in script.replace("  ", "") or \
        '«class mssg» id 22 of mailbox "Inbox"' in script

    with pytest.raises(triage.TriageError) as ei:
        triage._as_literal("bad\nname")
    assert ei.value.code == "invalid_name"


def test_parse_batch_output_shapes():
    out = triage._parse_batch_output(
        "OK 1\nERR 2 mid_mismatch <other@x>\nERR 3 applescript -1728 nope\ngarbage\n",
        [1, 2, 3, 4],
    )
    assert out[1] == ("ok", "")
    assert out[2][0] == "mid_mismatch"
    assert out[3][0] == "applescript"
    assert out[4][0] == "no_result"


# --------------------------------------------------------------------- #
# mailbox_create + pre-flight                                           #
# --------------------------------------------------------------------- #


def test_mailbox_create_validation_and_idempotency(src, db, fake_osa):
    with pytest.raises(triage.TriageError) as ei:
        triage.create_mailbox(src, "NOT-AN-ACCOUNT", "X")
    assert ei.value.code == "unknown_account"
    assert fake_osa.scripts == []

    res = triage.create_mailbox(src, LOCAL_ACCT, "Inbox")
    assert res["existed"] is True and res["index_verified"] is True
    assert fake_osa.scripts == []  # idempotent: no osascript at all

    def batch(script):
        _add_mailbox(db, 7, f"local://{LOCAL_ACCT}/Filed2")
        return subprocess.CompletedProcess([], 0, "OK\n", "")
    fake_osa.batch = batch
    res = triage.create_mailbox(src, LOCAL_ACCT, "Filed2")
    assert res["applescript"] == "OK" and res["index_verified"] is True
    assert "account id" not in fake_osa.scripts[-1]  # local → app-level


def test_mailbox_delete_idempotent_and_guards(src, db, fake_osa):
    with pytest.raises(triage.TriageError) as ei:
        triage.delete_mailbox(src, "NOT-AN-ACCOUNT", "X")
    assert ei.value.code == "unknown_account"

    # Absent already → idempotent success, no delete verb ever issued.
    def probe_no(script):
        return subprocess.CompletedProcess([], 0, "NO\n", "")
    fake_osa.batch = probe_no
    res = triage.delete_mailbox(src, LOCAL_ACCT, "Ghost")
    assert res["ok"] is True and res["existed"] is False and res["deleted"] is False
    assert all("delete" not in s for s in fake_osa.batch_scripts)

    # Non-empty → refused before any delete.
    calls = []

    def router_nonempty(script):
        calls.append(script)
        if "exists" in script:
            return subprocess.CompletedProcess([], 0, "YES\n", "")
        if "count of messages" in script:
            return subprocess.CompletedProcess([], 0, "3\n", "")
        raise AssertionError("delete must not be reached")
    fake_osa.batch = router_nonempty
    with pytest.raises(triage.TriageError) as ei:
        triage.delete_mailbox(src, LOCAL_ACCT, "Inbox")
    assert ei.value.code == "not_empty" and "3 message" in str(ei.value)


def test_mailbox_delete_swallows_false_error_and_verifies(src, db, fake_osa):
    """The fleet-discovered wart: Mail's delete verb returns -10000 even on
    success. Outcome must come from the re-probe, not the verb's reply."""
    state = {"exists": True}

    def router(script):
        if "count of messages" in script:
            return subprocess.CompletedProcess([], 0, "0\n", "")
        if "exists" in script:
            return subprocess.CompletedProcess(
                [], 0, ("YES" if state["exists"] else "NO") + "\n", "")
        if "delete" in script:
            state["exists"] = False  # deletion works...
            return subprocess.CompletedProcess(  # ...but the verb lies
                [], 1, "", "execution error: AppleEvent handler failed. (-10000)")
        raise AssertionError(f"unexpected script: {script[:80]}")
    fake_osa.batch = router

    res = triage.delete_mailbox(src, LOCAL_ACCT, "Doomed")
    assert res["ok"] is True and res["deleted"] is True
    assert "-10000" in res["warning"] and "verifiably gone" in res["warning"]


def test_mailbox_delete_honest_failure_when_survives(src, db, fake_osa):
    """Phantom-Exchange-folder case: verb errors, UI path runs without
    effect, box survives → honest delete_failed."""
    def router(script):
        if "System Events" in script:
            return subprocess.CompletedProcess([], 0, "NO_SHEET\n", "")
        if "count of messages" in script:
            return subprocess.CompletedProcess([], 0, "0\n", "")
        if "exists" in script:
            return subprocess.CompletedProcess([], 0, "YES\n", "")
        if "delete" in script:
            return subprocess.CompletedProcess(
                [], 1, "", "execution error: AppleEvent handler failed. (-10000)")
        raise AssertionError("unexpected script")
    fake_osa.batch = router

    res = triage.delete_mailbox(src, LOCAL_ACCT, "Phantom")
    assert res["ok"] is False and res["deleted"] is False
    assert res["code"] == "delete_failed" and "phantom" in res["error"].lower()


def test_mailbox_delete_ui_escalation_succeeds(src, db, fake_osa):
    """The live-measured reality: verb -10000 with NO effect; Mail's own
    Delete Mailbox menu (UI tier) is what actually removes it."""
    state = {"exists": True}

    def router(script):
        if "System Events" in script:
            state["exists"] = False
            return subprocess.CompletedProcess([], 0, "DELETED\n", "")
        if "count of messages" in script:
            return subprocess.CompletedProcess([], 0, "0\n", "")
        if "exists" in script:
            return subprocess.CompletedProcess(
                [], 0, ("YES" if state["exists"] else "NO") + "\n", "")
        if "delete" in script:
            return subprocess.CompletedProcess(  # verb lies AND does nothing
                [], 1, "", "execution error: AppleEvent handler failed. (-10000)")
        raise AssertionError("unexpected script")
    fake_osa.batch = router

    res = triage.delete_mailbox(src, LOCAL_ACCT, "Stubborn")
    assert res["ok"] is True and res["deleted"] is True
    assert res["method"] == "ui" and "Delete Mailbox menu" in res["warning"]


def test_mailbox_delete_accessibility_denied(src, db, fake_osa):
    def router(script):
        if "System Events" in script:
            return subprocess.CompletedProcess(
                [], 1, "", "execution error: osascript is not allowed assistive access. (-1719)")
        if "count of messages" in script:
            return subprocess.CompletedProcess([], 0, "0\n", "")
        if "exists" in script:
            return subprocess.CompletedProcess([], 0, "YES\n", "")
        if "delete" in script:
            return subprocess.CompletedProcess([], 1, "", "(-10000)")
        raise AssertionError("unexpected script")
    fake_osa.batch = router

    res = triage.delete_mailbox(src, LOCAL_ACCT, "Blocked")
    assert res["ok"] is False and res["code"] == "accessibility_denied"
    assert "Accessibility" in res["error"]


def test_mailbox_create_reports_mail_verified(src, db, fake_osa):
    def batch(script):
        _add_mailbox(db, 8, f"local://{LOCAL_ACCT}/Filed3")
        return subprocess.CompletedProcess([], 0, "OK\n", "")
    fake_osa.batch = batch
    res = triage.create_mailbox(src, LOCAL_ACCT, "Filed3")
    assert res["mail_verified"] is True
    # existed path carries the same keys
    res2 = triage.create_mailbox(src, LOCAL_ACCT, "Filed3")
    assert res2["existed"] is True and res2["mail_verified"] is True
    assert "warning" in res2


def test_preflight_account_check_and_automation_denied(src, db, fake_osa):
    # Plan on the imap message; pre-flight does not list its account.
    plan = _plan(src, [{"action": "mark_read"}], account=IMAP_ACCT)
    fake_osa.accounts = []
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan.id)
    assert ei.value.code == "account_unresolvable"
    assert plans.load(plan.id).status == "failed"
    assert fake_osa.batch_scripts == []  # aborted before any mutation

    # Automation denied maps from osascript -1743
    plan2 = _plan(src, [{"action": "mark_read"}], unread_only=True)
    fake_osa.preflight_rc = 1
    fake_osa.preflight_stderr = "execution error: Not authorised (-1743)"
    with pytest.raises(triage.TriageError) as ei:
        triage.apply_plan(src, plan2.id)
    assert ei.value.code == "automation_denied"

    # Local-only plan sails through an empty pre-flight account list.
    db.execute("UPDATE messages SET read=0 WHERE ROWID=100")
    db.commit()
    fake_osa.preflight_rc = 0
    fake_osa.preflight_stderr = ""
    fake_osa.accounts = []
    plan3 = _plan(src, [{"action": "mark_read"}], unread_only=True)

    def batch(script):
        db.execute("UPDATE messages SET read=1 WHERE ROWID=100")
        db.commit()
        return subprocess.CompletedProcess([], 0, "OK 100\n", "")
    fake_osa.batch = batch
    assert triage.apply_plan(src, plan3.id)["verified"] == 1
