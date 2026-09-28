"""Exercise reviewed exclusions through stdio, storage, verification and audit."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import textwrap

from mcp import Client, StdioServerParameters

from tests.test_mcp_wire import _envelope, _server_env, WIRE_TIMEOUT


FIXTURE_SERVER = textwrap.dedent(r'''
    import json
    import os
    import re
    import sqlite3
    import subprocess
    from pathlib import Path
    from email_mcp import server, triage

    def fixture_actor(script, timeout):
        if "accountIds" in script:
            return subprocess.CompletedProcess([], 0,
                "BBBBBBBB-0000-0000-0000-000000000002\n", "")
        ids = [int(value) for value in re.findall(r"«class mssg» id (\d+)", script)]
        assert ids and "set flag index of msgRef to 1" in script
        database = Path(os.environ["EMAIL_MCP_MAIL_DIR"]) / "MailData" / "Envelope Index"
        with sqlite3.connect(database) as conn:
            conn.executemany("UPDATE messages SET flagged=1,flag_color=1 WHERE ROWID=?",
                             [(value,) for value in ids])
        conn.close()
        with open(os.environ["FIXTURE_ACTOR_LOG"], "a") as log:
            log.write(json.dumps(ids) + "\n")
        return subprocess.CompletedProcess([], 0,
            "".join(f"OK {value}\n" for value in ids), "")

    triage._run_osascript = fixture_actor
    raise SystemExit(server.main([]))
''')


def test_stdio_exclusions_preserve_reviewed_plan_and_audit(tmp_path, mail_fixture):
    actor_log = tmp_path / "actor.jsonl"
    env = _server_env(
        tmp_path, mail_fixture, EMAIL_MCP_READ_ONLY="0",
        EMAIL_MCP_TRIAGE_VERIFY_INTERVAL="0", FIXTURE_ACTOR_LOG=str(actor_log),
    )

    async def workflow():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", FIXTURE_SERVER], env=env,
        )
        async with Client(params) as client:
            async def call(name, arguments):
                return _envelope(await client.call_tool(name, arguments))

            plan = await call("triage_plan", {"actions": [{"action": "flag", "color": 1}]})
            assert plan["ok"] is True and plan["count"] == 4
            plan_id = plan["plan_id"]
            invalid = await call("triage_apply", {
                "plan_id": plan_id, "exclude_ids": ["999999"],
            })
            assert invalid["ok"] is False and invalid["code"] == "invalid_exclusion"
            assert not actor_log.exists()
            result = await call("triage_apply", {
                "plan_id": plan_id, "exclude_ids": ["100", "100"],
            })
            assert result["ok"] is True
            assert result["planned"] == 4
            assert result["selected"] == result["acted"] == result["verified"] == 3
            assert result["excluded"] == ["100"]
            assert result["failures"] == result["pending"] == []
            repeated = await call("triage_apply", {"plan_id": plan_id})
            assert repeated["code"] == "plan_already_applied"
            ledger = await call("audit", {"operation_id": plan_id})
            terminal = [event for event in ledger["events"] if event["event"] == "plan_finish"]
            assert len(terminal) == 1
            assert terminal[0]["detail"]["selected"] == 3
            assert terminal[0]["detail"]["excluded"] == ["100"]
            return plan_id

    plan_id = asyncio.run(asyncio.wait_for(workflow(), WIRE_TIMEOUT))
    calls = [json.loads(line) for line in actor_log.read_text().splitlines()]
    assert sorted(value for call in calls for value in call) == [101, 200, 300]
    with sqlite3.connect(mail_fixture / "MailData" / "Envelope Index") as conn:
        assert dict(conn.execute("SELECT ROWID,flagged FROM messages")) == {
            100: 0, 101: 1, 200: 1, 300: 1,
        }
    conn.close()
    stored = json.loads((Path(env["EMAIL_MCP_STATE_DIR"]) / "plans" / f"{plan_id}.json").read_text())
    assert len(stored["messages"]) == 4
    assert stored["excluded_ids"] == ["100"]
