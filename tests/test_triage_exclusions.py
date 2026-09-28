"""Selection narrowing preserves reviewed identities, counts, and single-use plans."""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict

import pytest

from email_mcp import bootstrap, codes, config, plans, server, triage
from email_mcp.sources.base import SearchQuery
from tests.test_triage import (
    IMAP_ACCT, LOCAL_ACCT, _add_mailbox, _bulk_plan, _clean_env, _ok_and_mutate,
    _plan, _script_ids, _seed_bulk, db, fake_osa, src,
)


def _finish_event(plan_id):
    records = [json.loads(line) for path in config.audit_dir().glob("*.jsonl")
               for line in path.read_text().splitlines()]
    return [r for r in records if r.get("plan_id") == plan_id
            and r["event"] == "plan_finish"]


def test_mcp_apply_narrows_frozen_plan_and_records_selection(src, db, fake_osa, monkeypatch):
    plan = _bulk_plan(src, db, 40)
    original = asdict(plan)
    excluded = [str(plan.messages[1].rowid), str(plan.messages[-1].rowid)]
    fake_osa.batch = _ok_and_mutate(db)
    monkeypatch.setattr(bootstrap, "_application", bootstrap.build_application(source=src))
    result = server.tool_triage_apply(plan.id, exclude_ids=excluded[::-1] + excluded)
    assert result["ok"] is True
    assert (result["planned"], result["selected"], result["acted"], result["verified"]) == (40, 38, 38, 38)
    assert result["excluded"] == excluded
    assert result["failures"] == result["pending"] == []
    scripted = [str(rid) for script in fake_osa.batch_scripts for rid in _script_ids(script)]
    assert scripted == [str(m.rowid) for m in plan.messages if str(m.rowid) not in excluded]
    stored = asdict(plans.load(plan.id))
    for key in ("messages", "actions", "target", "query", "summary", "expires_at", "created_at"):
        assert stored[key] == original[key]
    assert stored["excluded_ids"] == excluded
    assert stored["result"]["excluded"] == excluded
    events = _finish_event(plan.id)
    assert len(events) == 1
    assert {k: events[0]["detail"][k] for k in ("planned", "selected", "excluded")} == {
        "planned": 40, "selected": 38, "excluded": excluded,
    }
    with pytest.raises(triage.TriageError, match="applied") as error:
        triage.apply_plan(src, plan.id, [])
    assert error.value.code == "plan_already_applied"


@pytest.mark.parametrize("excluded,code", [
    (["999999"], "invalid_exclusion"), (["0100"], "invalid_exclusion"),
    ([100], "invalid_exclusion"), ([None], "invalid_exclusion"),
    ("100", "invalid_exclusion"), ({"100": True}, "invalid_exclusion"),
    (["100"], "empty_selection"),
])
def test_invalid_exclusions_leave_plan_usable(src, db, fake_osa, excluded, code):
    plan = _plan(src, [{"action": "mark_read"}], unread_only=True)
    path = config.plans_dir() / f"{plan.id}.json"
    original = path.read_bytes()
    with pytest.raises(triage.TriageError) as error:
        triage.apply_plan(src, plan.id, excluded)
    assert error.value.code == code and error.value.operation_id == plan.id
    assert error.value.code in codes.TRIAGE_CODES
    assert path.read_bytes() == original
    assert not path.with_suffix(".json.applying").exists()
    assert fake_osa.scripts == [] and _finish_event(plan.id) == []
    fake_osa.batch = _ok_and_mutate(db)
    assert triage.apply_plan(src, plan.id)["verified"] == 1


def test_exclusion_can_remove_unavailable_account_from_preflight(src, db, fake_osa):
    plan = _plan(src, [{"action": "mark_read"}], limit=10)
    excluded = [str(m.rowid) for m in plan.messages if m.account == IMAP_ACCT]
    assert excluded
    fake_osa.accounts = []
    fake_osa.batch = _ok_and_mutate(db)
    result = triage.apply_plan(src, plan.id, excluded)
    assert result["selected"] == result["verified"] == 3


@pytest.mark.parametrize("failure", ["timeout", "script"])
def test_early_failure_keeps_accepted_exclusions(src, db, fake_osa, monkeypatch, failure):
    plan = _bulk_plan(src, db, 3)
    excluded = [str(plan.messages[0].rowid)]
    if failure == "timeout":
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired("fixture", 15)
        monkeypatch.setattr(triage, "_run_osascript", fail)
        with pytest.raises(triage.TriageError):
            triage.apply_plan(src, plan.id, excluded)
    else:
        fake_osa.preflight_rc = 1
        fake_osa.preflight_stderr = "fixture error"
        with pytest.raises(triage.TriageError):
            triage.apply_plan(src, plan.id, excluded)
    stored = plans.load(plan.id)
    assert stored.status == "failed" and stored.excluded_ids == excluded
    assert stored.result["planned"] == 3 and stored.result["selected"] == 2
    assert stored.result["excluded"] == _finish_event(plan.id)[0]["detail"]["excluded"] == excluded


def test_exclusions_preserve_timeout_counts_and_unattempted_tail(src, db, fake_osa):
    plan = _bulk_plan(src, db, 25)
    excluded = [str(plan.messages[0].rowid), str(plan.messages[-1].rowid)]
    calls = 0
    ok = _ok_and_mutate(db)

    def batch(script):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ok(script)
        raise subprocess.TimeoutExpired("fixture", 150)

    fake_osa.batch = batch
    result = triage.apply_plan(src, plan.id, excluded)
    assert (result["planned"], result["selected"], result["acted"], result["verified"]) == (25, 23, 10, 10)
    assert [f["code"] for f in result["failures"]].count("batch_timeout") == 10
    assert [f["code"] for f in result["failures"]].count("not_attempted") == 3
    assert not set(excluded) & {x["id"] for x in result["failures"] + result["pending"]}


def test_local_move_exclusions_are_absent_from_every_chunk(src, db, fake_osa):
    _add_mailbox(db, 3, f"local://{LOCAL_ACCT}/Filed")
    _seed_bulk(db, 52)
    plan = _plan(src, [{"action": "move_to", "mailbox": "Filed"}],
                 from_addr="ops-bot", unread_only=True, limit=52)
    excluded = [str(m.rowid) for m in plan.messages[:2]]

    def batch(script):
        ids = _script_ids(script)
        assert not set(excluded) & {str(i) for i in ids}
        if "move (every" in script:
            assert f"if (count of moveIds) is {len(ids)}" in script
        db.executemany("UPDATE messages SET mailbox=3 WHERE ROWID=?", [(i,) for i in ids])
        db.commit()
        return subprocess.CompletedProcess([], 0, "".join(f"OK {i}\n" for i in ids), "")

    fake_osa.batch = batch
    result = triage.apply_plan(src, plan.id, excluded)
    assert result["selected"] == result["verified"] == 50
    assert sum(len(_script_ids(s)) for s in fake_osa.batch_scripts) == 50
    assert all(src.triage_snapshot([int(i)])[int(i)]["mailbox_rowid"] == 1 for i in excluded)


def test_delete_exclusions_leave_unselected_mail_in_place(src, db, fake_osa):
    plan = triage.build_delete_plan(src, SearchQuery(from_addr="ops-bot@cern.ch"))
    excluded = [str(plan.messages[0].rowid)]

    def batch(script):
        ids = _script_ids(script)
        assert "delete msgRef" in script and len(ids) == 1
        db.execute("UPDATE messages SET deleted=1 WHERE ROWID=?", (ids[0],))
        db.commit()
        return subprocess.CompletedProcess([], 0, f"OK {ids[0]}\n", "")

    fake_osa.batch = batch
    result = triage.apply_plan(src, plan.id, excluded)
    assert result["planned"] == 2 and result["selected"] == result["verified"] == 1
    assert src.triage_snapshot([int(excluded[0])])[int(excluded[0])]["deleted"] == 0
