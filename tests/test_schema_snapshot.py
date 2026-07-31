"""Schema snapshots: the 20-tool wire surface, frozen and self-defending.

inputSchema — FROZEN at v0.10 (docs/v1-contract.md §8; inputs did not
change in v0.11). Descriptions are EXCLUDED from the snapshot: docstring
churn is allowed, schemas are not.

outputSchema — FROZEN at v0.11 (§8), right after the bare shapes took
their one allowed break into envelopes. FastMCP 1.27 derives outputSchema
from return annotations, and every tool is annotated `-> dict` (envelopes
are dynamic dicts), so the DECLARED outputSchema is None for all 20 tools
— there is no declared schema to freeze, and declaring one would mean
typed returns in server.py (a wire change of its own). The strongest
freeze available is therefore structural: call every tool_* wrapper —
ALL 20, mutating tools included — against the mail fixture under tmp
EMAIL_MCP_* state, faking exactly the seams the behavioral suites fake
(sender._deliver_bytes, triage._run_osascript, server.subprocess.run,
dispatcher._plist_path) so each envelope is built by PRODUCTION code,
and snapshot the SHAPE of each success envelope: recursive keys plus
scalar type names, values dropped, list elements union-merged so a shape
change in ANY element trips the freeze. The declared-None map is
snapshotted too, so a future typed return (which would put a real
outputSchema on the wire) also trips deliberately. Both freezes fail
loudly when their snapshot file is missing (audit finding F7) — never a
silent re-freeze.

Scope decisions (recorded, not implied):
- doctor is probed with `_CHECKS = ()`: the frozen surface is its
  envelope (ok / read_only / checks / the always-on ledger check), not
  the ten diagnostic checks — their inner keys vary with machine state
  by design (`fix` appears only on failure) and the contract names
  doctor a documented exception (§2).
- Success shapes only. Failure envelopes are governed by §2/§3 and
  pinned by the belt/code suites (test_wire_belts, test_triage,
  test_audit_hooks); per-item failure DATA inside ok envelopes (batch
  `errors[]`, apply `failures[]`/`pending[]`) IS frozen here.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from email_mcp import audit, config, dispatcher, doctor, sender, server, triage
from email_mcp.sources.apple_mail import AppleMailSource

# Captured before conftest's autouse guard patches it: the snapshot must be
# built by the REAL env-driven resolver, not by a session-pinned lambda.
_REAL_STATE_ROOT = config.state_root

SNAPSHOT = Path(__file__).parent / "snapshots" / "input_schemas.json"
OUTPUT_SNAPSHOT = Path(__file__).parent / "snapshots" / "output_schemas.json"


def _strip_descriptions(node):
    """Drop every `description` key, recursively — the snapshot freezes
    shapes (properties, types, defaults, required), never prose."""
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items()
                if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(x) for x in node]
    return node


def _current_schemas(monkeypatch) -> dict:
    """{tool name: inputSchema} for the full 20-tool surface."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    try:
        pairs = {t.name: t.inputSchema
                 for t in asyncio.run(mcp.list_tools())}
    except Exception:
        # FastMCP API drift fallback: enumerate the tool manager directly.
        pairs = {t.name: t.parameters
                 for t in mcp._tool_manager.list_tools()}
    return {name: _strip_descriptions(schema)
            for name, schema in sorted(pairs.items())}


def _render(schemas: dict) -> str:
    """Canonical byte form: sorted keys, 2-space indent, trailing newline —
    regenerating an unchanged surface is a byte-identical file."""
    return json.dumps(schemas, indent=2, sort_keys=True) + "\n"


def test_input_schemas_match_snapshot(monkeypatch):
    current = _current_schemas(monkeypatch)
    assert len(current) == 20
    if not SNAPSHOT.exists():
        # The freeze must be self-defending: a deleted snapshot fails loudly
        # instead of silently re-freezing whatever the code now emits
        # (audit finding F7). Restore it from git, or regenerate DELIBERATELY:
        #   python -c "from tests.test_schema_snapshot import *; \
        #              SNAPSHOT.write_text(_render(_current_schemas(...)))"
        pytest.fail(
            "tests/snapshots/input_schemas.json is missing — the inputSchema "
            "freeze (contract §8) cannot be verified. Restore it via "
            "`git checkout tests/snapshots/` or regenerate deliberately."
        )
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == frozen, (
        "inputSchema drift against tests/snapshots/input_schemas.json — "
        "tool inputs are FROZEN at v0.10 (docs/v1-contract.md §8). If the "
        "change is genuinely intended, regenerate the snapshot and say so "
        "explicitly in the change description."
    )


def test_snapshot_regeneration_is_byte_stable(monkeypatch):
    first = _render(_current_schemas(monkeypatch))
    second = _render(_current_schemas(monkeypatch))
    assert first == second  # two builds, identical bytes
    assert first == SNAPSHOT.read_text(encoding="utf-8")


# --------------------------------------------------------------------- #
# outputSchema freeze (v0.11)                                           #
# --------------------------------------------------------------------- #


def _shape(node, depth: int = 0):
    """Structure without values: dict -> {key: shape}, scalar -> type
    name, list -> ONE union-merged element shape (optional keys and type
    variants from ANY element are visible — no element hides drift behind
    element 0). The depth cap is a safety valve only: 8 exceeds the
    deepest real envelope, so "..." never appears in a healthy snapshot
    (at the previous cap of 4 it truncated real structure)."""
    if depth > 8:
        return "..."
    if isinstance(node, dict):
        return {k: _shape(v, depth + 1) for k, v in sorted(node.items())}
    if isinstance(node, list):
        if not node:
            return []
        merged = _shape(node[0], depth + 1)
        for item in node[1:]:
            merged = _merge(merged, _shape(item, depth + 1))
        return [merged]
    if node is None:
        return "null"
    # Wire type, not implementation class: bool before int (a subclass),
    # then the JSON scalar families — a str-subclass header object (e.g.
    # email.headerregistry's private _MessageIDHeader, seen on
    # send_email.message_id) serializes as a plain JSON string; freezing
    # its class name would pin a stdlib detail, not the wire.
    if isinstance(node, bool):
        return "bool"
    if isinstance(node, str):
        return "str"
    if isinstance(node, int):
        return "int"
    if isinstance(node, float):
        return "float"
    return type(node).__name__


def _merge(a, b):
    """Union of two element shapes. Dicts merge key-wise (a key present in
    either survives), lists merge element shapes ([] yields to the
    non-empty side), scalar disagreements become a sorted "a|b" marker —
    deterministic and order-independent throughout."""
    if a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: (_merge(a[k], b[k]) if k in a and k in b
                    else (a[k] if k in a else b[k]))
                for k in sorted(a.keys() | b.keys())}
    if isinstance(a, list) and isinstance(b, list):
        if not a or not b:
            return a or b
        return [_merge(a[0], b[0])]
    if isinstance(a, str) and isinstance(b, str):
        return "|".join(sorted(set(a.split("|")) | set(b.split("|"))))
    return "mixed"  # dict-vs-scalar heterogeneity: loud and deterministic


def _declared_output_schemas(monkeypatch) -> dict:
    """{tool name: declared outputSchema}. Every tool is annotated `-> dict`,
    so FastMCP declares nothing today — freezing the None map means a future
    typed return (a real wire schema) trips this test deliberately."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    try:
        tools = asyncio.run(mcp.list_tools())
    except Exception:
        tools = mcp._tool_manager.list_tools()
    return {t.name: getattr(t, "outputSchema", None) for t in sorted(
        tools, key=lambda t: t.name)}


_LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"
_IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"


def _all_tool_shapes(monkeypatch, tmp_path, mail_fixture) -> dict:
    """Success-envelope shapes for the FULL 20-tool surface.

    Fresh state dirs per call — the byte-stability test runs this twice,
    and pass two must see the same empty ledger/spool/plans/fts as pass
    one. Only the byte/osascript/launchd boundaries are stubbed (the
    exact seams the behavioral suites stub): compose, spool, plan build,
    apply and verify all run for real, so every frozen shape is built by
    production code."""
    base = Path(tempfile.mkdtemp(dir=tmp_path))
    for key in list(os.environ):
        if key.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(key, raising=False)
    state = base / "state"
    for key, value in {
        "EMAIL_MCP_MAIL_DIR": str(mail_fixture),
        # ONE root: spool, plans, fts and the ledger all derive from it.
        "EMAIL_MCP_STATE_DIR": str(state),
        "EMAIL_MCP_ATTACH_DIR": str(base / "attach"),
        "EMAIL_MCP_IDENTITIES": str(base / "none.toml"),
        "EMAIL_MCP_FROM_ADDR": "paris@example.org",
        "EMAIL_MCP_FROM_NAME": "Paris",
        "EMAIL_MCP_SEND_ALLOW_ALL": "1",
    }.items():
        monkeypatch.setenv(key, value)
    audit_dir = state / "audit"
    # Undo the conftest guard's patched resolver: it pins ONE root for the
    # whole session, and this builder needs a fresh empty tree per call
    # (the byte-stability test runs it twice). Restoring the real function
    # makes every leaf derive from EMAIL_MCP_STATE_DIR set just above.
    monkeypatch.setattr(config, "state_root", _REAL_STATE_ROOT)
    monkeypatch.setattr(server, "_SOURCE",
                        AppleMailSource(mail_base=mail_fixture),
                        raising=False)
    # Transport seam (the sender-suite fake): compose runs, bytes go nowhere.
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", lambda raw: None)
    # launchd pinned ABSENT: schedule_email's `warning` key is deterministic.
    monkeypatch.setattr(dispatcher, "_plist_path",
                        lambda: base / "launchd.plist")
    # osascript seam (the triage-suite fake, minimal): account pre-flight,
    # one mailbox that verifiably deletes, "OK 100" for the apply batch.
    box = {"exists": True}

    def _osa(script: str, timeout: float):
        def done(stdout: str):
            return subprocess.CompletedProcess([], 0, stdout, "")
        if "accountIds" in script:
            return done(f"{_LOCAL_ACCT}\n{_IMAP_ACCT}\n")
        if "count of messages" in script:
            return done("0\n")
        if "exists" in script:
            return done(("YES" if box["exists"] else "NO") + "\n")
        if "delete" in script:
            box["exists"] = False
            return done("OK\n")
        return done("OK 100\n")

    monkeypatch.setattr(triage, "_run_osascript", _osa)

    shapes: dict[str, dict] = {}

    def take(name: str, result: dict) -> dict:
        assert isinstance(result, dict) and result.get("ok") is True, (
            f"{name} did not return a success envelope: {result!r}")
        shapes[name] = _shape(result)
        return result

    # doctor FIRST — the ledger is still empty, so the audit check's
    # last_event is "null" on every pass. _CHECKS narrowed to (): the
    # envelope-only freeze decision in the module docstring.
    monkeypatch.setattr(doctor, "_CHECKS", ())
    take("doctor", server.tool_doctor())

    # Read surface. 101 carries the attachment; 999 does not exist, so
    # the batch freezes the per-id errors[] element {id, code, error} too.
    att_id = AppleMailSource(mail_base=mail_fixture) \
        .get("101").attachments[0].attachment_id
    take("search_emails", server.tool_search_emails(query="I2C"))
    take("get_email", server.tool_get_email("101"))
    take("get_emails_batch",
         server.tool_get_emails_batch(["100", "101", "999"]))
    take("get_thread", server.tool_get_thread("7001"))
    take("list_mailboxes", server.tool_list_mailboxes())
    take("list_recent", server.tool_list_recent(limit=2))
    take("get_attachment", server.tool_get_attachment("101", att_id))
    ok_osa = subprocess.CompletedProcess(["osascript"], 0, "", "")
    with mock.patch.object(server.subprocess, "run", return_value=ok_osa):
        take("refresh_mail", server.tool_refresh_mail(wait_seconds=0))

    # Transmission: real compose + spool, faked byte delivery.
    take("send_email", server.tool_send_email(
        to="paris@example.org", subject="s", body="b"))
    take("reply_email", server.tool_reply_email(
        id="100", body="r", reply_all=True))
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    take("schedule_email", server.tool_schedule_email(
        to="paris@example.org", subject="later", body="b", send_at=later))
    doomed = server.tool_schedule_email(
        to="paris@example.org", subject="doomed", body="b", send_at=later)
    take("cancel_scheduled", server.tool_cancel_scheduled(doomed["id"]))
    # After the two schedules: pending[] and cancelled[] each freeze a
    # spool-entry element shape.
    take("list_scheduled", server.tool_list_scheduled())

    # Triage + mailboxes. The fake osascript never writes the index, so
    # the apply result exercises the verify-pending path: pending[]'s
    # {id, expected, observed} element shape is frozen too.
    plan = take("triage_plan", server.tool_triage_plan(
        unread_only=True, actions=[{"action": "mark_read"}]))
    take("triage_apply", server.tool_triage_apply(plan["plan_id"]))
    take("triage_plan_delete",
         server.tool_triage_plan_delete(account=_LOCAL_ACCT))
    # Idempotent create — triage.py's "same shape everywhere" return.
    take("mailbox_create",
         server.tool_mailbox_create(account=_LOCAL_ACCT, path="Inbox"))
    take("mailbox_delete",
         server.tool_mailbox_delete(account=_LOCAL_ACCT, path="Filed"))

    # The ledger tool LAST, right after a seeded emit: events[0] is this
    # deliver event on every pass, whatever the probes above recorded.
    audit.emit("deliver", outcome="sent", operation_id="snap-1",
               spool_id="snap-1", tool="schedule_email")
    take("audit", server.tool_audit(limit=1))

    assert len(shapes) == 20
    return {name: shapes[name] for name in sorted(shapes)}


def test_output_surface_matches_snapshot(monkeypatch, tmp_path, mail_fixture):
    declared = _declared_output_schemas(monkeypatch)
    shapes = _all_tool_shapes(monkeypatch, tmp_path, mail_fixture)
    current = {"declared": declared, "success_shapes": shapes}
    assert len(declared) == 20
    if not OUTPUT_SNAPSHOT.exists():
        pytest.fail(
            "tests/snapshots/output_schemas.json is missing — the outputSchema "
            "freeze (contract §8) cannot be verified. Restore it via "
            "`git checkout tests/snapshots/` or regenerate deliberately."
        )
    frozen = json.loads(OUTPUT_SNAPSHOT.read_text(encoding="utf-8"))
    assert current == frozen, (
        "output surface drift against tests/snapshots/output_schemas.json — "
        "tool RETURNS are FROZEN at v0.11 (docs/v1-contract.md §8); a "
        "breaking change is v2. Regenerate only for an intended additive "
        "change and say so explicitly."
    )


def test_output_snapshot_is_byte_stable(monkeypatch, tmp_path, mail_fixture):
    first = _render({
        "declared": _declared_output_schemas(monkeypatch),
        "success_shapes": _all_tool_shapes(monkeypatch, tmp_path, mail_fixture),
    })
    second = _render({
        "declared": _declared_output_schemas(monkeypatch),
        "success_shapes": _all_tool_shapes(monkeypatch, tmp_path, mail_fixture),
    })
    assert first == second  # two full probe passes, identical bytes
    assert first == OUTPUT_SNAPSHOT.read_text(encoding="utf-8")
