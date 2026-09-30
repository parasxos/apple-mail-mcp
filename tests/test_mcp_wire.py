"""The MCP protocol itself, spoken to a real subprocess over real pipes.

Every other test in this suite reaches into the process it is running in:
it imports `server`, calls `_build_mcp_server()`, and inspects Python
objects. That proves the tools exist; it proves nothing about whether the
shipped binary ever *serves* them. Deleting `mcp.run()` from
`server.main()` leaves the whole in-process suite green — including the
two subprocess tests in test_cli.py, which assert only `returncode == 0`
and an empty stdout, a bar that a server doing nothing at all clears.

So this module is the only place that launches `email-mcp` the way
~/.claude.json launches it (bare, no arguments — the compatibility promise
of contract §7), performs a genuine `initialize` handshake, and asserts
against what comes back over the wire: the frozen twenty-one tools, the eleven
read-side tools under EMAIL_MCP_READ_ONLY=1, the §2 envelope on a real
tool call, and the purity of stdout, which *is* the transport.

Cost: each test spawns and handshakes one server, ~0.4s. Slow relative to
the rest of the suite, cheap relative to shipping a server that does not
serve — so these run by default, with no opt-out marker.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from jsonschema import Draft202012Validator
from tests._mcp_sdk import sdk_attr

READ_ONLY_TOOLS = {
    "search_emails", "get_email", "get_emails_batch", "get_thread",
    "list_mailboxes", "list_recent", "get_attachment", "refresh_mail",
    "list_scheduled", "doctor", "audit",
}
MUTATING_TOOLS = {
    "send_email",
    "create_draft", "reply_email", "schedule_email", "cancel_scheduled",
    "triage_plan", "triage_plan_delete", "triage_apply", "mailbox_create",
    "mailbox_delete",
}
ALL_TOOLS = READ_ONLY_TOOLS | MUTATING_TOOLS

# Generous: a cold interpreter import of the whole package on a loaded CI
# box is the slow part, and a hang here must still surface as a failure
# rather than an eternal test run.
WIRE_TIMEOUT = 90.0

# On-disk state the child would otherwise write under the real ~/.email-mcp.
# The child is a genuine second process, so the suite's autouse monkeypatch
# guards do not reach it — every path has to be pinned through the env.
# ONE root since v0.11 (spool/plans/graph/fts/audit derive from it) plus
# the attachment scratch, which is deliberately NOT under the root.
_STATE_DIRS = ("STATE_DIR", "ATTACH_DIR")


def _server_command() -> list[str]:
    """The command a client actually registers: the bare console script
    sitting next to the interpreter under test. Falls back to the module
    entry point when the package was not pip-installed (fresh checkout)."""
    script = Path(sys.executable).with_name("email-mcp")
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "email_mcp.cli"]


def _server_env(tmp_path: Path, mail_dir: Path, **extra: str) -> dict[str, str]:
    """A hermetic environment for the child: fixture mail store, throwaway
    HOME, and every writable path redirected into tmp. Inherited
    EMAIL_MCP_* variables are dropped wholesale so a developer's shell
    cannot point the child at the real ~/Library/Mail."""
    home = tmp_path / "wire-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMAIL_MCP_")}
    env["HOME"] = str(home)
    env["EMAIL_MCP_MAIL_DIR"] = str(mail_dir)
    for var in _STATE_DIRS:
        d = tmp_path / "wire-state" / var.lower()
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)  # what the created-only rule guarantees for ours
        env[f"EMAIL_MCP_{var}"] = str(d)
    env.update(extra)
    return env


def _talk(env: dict[str, str], body):
    """Spawn the server, complete the MCP handshake, hand the live session
    to `body`, and return its result. stdio_client owns the child and
    terminates it on the way out, so no orphan survives a failure."""
    cmd = _server_command()

    async def _go():
        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                return await body(session, init)

    return asyncio.run(asyncio.wait_for(_go(), WIRE_TIMEOUT))


def _envelope(result) -> dict:
    """Unwrap a tool result into the contract envelope it carries."""
    assert result.content, "tool result carried no content"
    payload = result.content[0]
    assert payload.type == "text", f"unexpected content type {payload.type!r}"
    envelope = json.loads(payload.text)
    structured = sdk_attr(result, "structuredContent", "structured_content")
    assert structured == envelope
    return envelope


# --------------------------------------------------------------------- #
# handshake + registration surface, over the wire                        #
# --------------------------------------------------------------------- #


def test_bare_invocation_completes_a_real_initialize_handshake(
    tmp_path, mail_fixture,
):
    """`email-mcp` with no arguments must answer `initialize` — the whole
    of contract §7's compatibility promise to the existing registration."""
    async def body(session, init):
        return init

    init = _talk(_server_env(tmp_path, mail_fixture), body)
    assert sdk_attr(init, "serverInfo", "server_info").name == "apple-mail"
    assert sdk_attr(init, "protocolVersion", "protocol_version")
    assert init.capabilities.tools is not None


def test_tools_list_over_the_wire_is_exactly_the_frozen_twenty_one(
    tmp_path, mail_fixture,
):
    async def body(session, init):
        return {t.name for t in (await session.list_tools()).tools}

    names = _talk(_server_env(tmp_path, mail_fixture), body)
    assert names == ALL_TOOLS
    assert len(names) == 21


def test_read_only_wire_surface_is_exactly_the_eleven_read_tools(
    tmp_path, mail_fixture,
):
    """The lexical gate has to hold in the shipped process, not just in an
    in-process rebuild: a read-only registration is a trust boundary."""
    async def body(session, init):
        return {t.name for t in (await session.list_tools()).tools}

    env = _server_env(tmp_path, mail_fixture, EMAIL_MCP_READ_ONLY="1")
    names = _talk(env, body)
    assert names == READ_ONLY_TOOLS
    assert not names & MUTATING_TOOLS


@pytest.mark.parametrize("mode", ["auto", "2026-07-28"])
@pytest.mark.parametrize("read_only", [False, True])
def test_latest_protocol_discovers_and_reads_over_stdio(
    tmp_path, mail_fixture, mode, read_only,
):
    cmd = _server_command()
    env = _server_env(
        tmp_path, mail_fixture, EMAIL_MCP_READ_ONLY=str(int(read_only)))

    async def talk():
        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        async with Client(params, mode=mode) as client:
            assert client.protocol_version == "2026-07-28"
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == (READ_ONLY_TOOLS if read_only else ALL_TOOLS)
            result = await client.call_tool("list_mailboxes", {})
            payload = _envelope(result)
            Draft202012Validator(tools["list_mailboxes"].output_schema).validate(payload)
            assert payload["ok"] is True
            assert len(payload["mailboxes"]) == 3

    asyncio.run(asyncio.wait_for(talk(), WIRE_TIMEOUT))


# --------------------------------------------------------------------- #
# tool calls, over the wire                                              #
# --------------------------------------------------------------------- #


def test_read_tool_call_returns_the_contract_envelope_with_fixture_data(
    tmp_path, mail_fixture,
):
    """A round trip that has to reach the mail store and come back: an
    ok-envelope alone could be faked by a stub, the fixture mailboxes
    could not."""
    async def body(session, init):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        result = await session.call_tool("list_mailboxes", {})
        schema = sdk_attr(
            tools["list_mailboxes"], "outputSchema", "output_schema",
        )
        return result, schema

    result, schema = _talk(_server_env(tmp_path, mail_fixture), body)
    assert sdk_attr(result, "isError", "is_error") is False
    envelope = _envelope(result)
    Draft202012Validator(schema).validate(envelope)
    assert envelope["ok"] is True
    paths = {m["path"] for m in envelope["mailboxes"]}
    assert "local://AAAAAAAA-0000-0000-0000-000000000001/Inbox" in paths


def test_tool_failure_crosses_the_wire_as_an_envelope_not_an_exception(
    tmp_path, mail_fixture,
):
    """§7: no exception crosses the wire. A bad id is a normal result
    carrying `ok: false` — not a JSON-RPC error, not a traceback."""
    async def body(session, init):
        return await session.call_tool("get_email", {"id": "no-such-id"})

    result = _talk(_server_env(tmp_path, mail_fixture), body)
    assert sdk_attr(result, "isError", "is_error") is False
    envelope = _envelope(result)
    assert envelope["ok"] is False
    assert envelope["code"] == "invalid_input"
    assert "Traceback" not in envelope["error"]


# --------------------------------------------------------------------- #
# stdout purity — raw pipes, nothing between us and the bytes            #
# --------------------------------------------------------------------- #


class _RawWire:
    """A hand-rolled MCP stdio client: newline-delimited JSON-RPC, spoken
    straight onto the pipes.

    ClientSession is the better tool for everything above, but it *consumes*
    stdout, so it can never testify about what else the server wrote there.
    This client keeps every byte, which is the only way to catch the stray
    print that corrupts the transport."""

    def __init__(self, env: dict[str, str]):
        self.proc = subprocess.Popen(
            _server_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._lines: queue.Queue = queue.Queue()
        self._pump = threading.Thread(target=self._read_stdout, daemon=True)
        self._pump.start()
        # Drained, never asserted on: an unread stderr pipe would deadlock
        # the child the moment its buffer filled.
        threading.Thread(target=self.proc.stderr.read, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF sentinel

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def next_line(self, timeout: float = WIRE_TIMEOUT) -> bytes:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"server wrote nothing to stdout within {timeout}s — "
                f"it is not serving the MCP protocol"
            ) from None
        if line is None:
            raise AssertionError(
                "server closed stdout without answering — it exited instead "
                "of serving the MCP protocol"
            )
        return line

    def drain(self, timeout: float = WIRE_TIMEOUT) -> list[bytes]:
        """Everything still on stdout up to EOF. Call after closing stdin."""
        rest: list[bytes] = []
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError("server never closed stdout") from None
            if line is None:
                return rest
            rest.append(line)

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)


def test_stdout_carries_only_valid_jsonrpc_framing(tmp_path, mail_fixture):
    """stdout is the transport: one stray byte — a banner, a warning, a
    forgotten print — desynchronises every client on the wire."""
    wire = _RawWire(_server_env(tmp_path, mail_fixture))
    try:
        wire.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test_mcp_wire", "version": "0"},
            },
        })
        first = wire.next_line()
        wire.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                   "params": {}})
        second = wire.next_line()
        wire.proc.stdin.close()
        lines = [first, second, *wire.drain()]
    finally:
        wire.close()

    for line in lines:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            raise AssertionError(
                f"non-JSON-RPC bytes on the MCP transport: {line[:120]!r}"
            ) from None
        assert message["jsonrpc"] == "2.0"
        assert "result" in message or "error" in message or "method" in message

    handshake = json.loads(first)
    assert handshake["id"] == 1
    assert handshake["result"]["serverInfo"]["name"] == "apple-mail"

    listing = json.loads(second)
    assert listing["id"] == 2
    assert {t["name"] for t in listing["result"]["tools"]} == ALL_TOOLS


# --------------------------------------------------------------------- #
# server survival — the §5 page cap, proven over the wire                #
# --------------------------------------------------------------------- #


def test_over_cap_page_cannot_kill_the_server(tmp_path, mail_fixture):
    """The first-user case (2026-08-01): a 20,000-row page request must
    come back as a coded reject — and the server must still be alive to
    answer the next call on the same session. Before the §5 page cap this
    pulled the whole corpus into one envelope, crashed the process, and
    took every tool down with it."""
    async def body(session, init):
        first = await session.call_tool("search_emails", {"limit": 20000})
        second = await session.call_tool("list_mailboxes", {})
        return first, second

    first, second = _talk(_server_env(tmp_path, mail_fixture), body)
    assert sdk_attr(first, "isError", "is_error") is False
    reject = _envelope(first)
    assert reject["ok"] is False and reject["code"] == "invalid_input"
    assert "500" in reject["error"]
    alive = _envelope(second)
    assert alive["ok"] is True  # the server survived to answer again


# --------------------------------------------------------------------- #
# schedule_email threading, over the wire                                #
# --------------------------------------------------------------------- #
#
# The Python-level tests call tool_schedule_email directly. Over stdio the
# SDK builds the tool's input schema from the *registered* function, and
# silently drops any argument that function does not declare. server.py
# wraps schedule_email in `_schedule_for_mcp`; when that wrapper lagged the
# tool's signature, a client could pass `in_reply_to` and get `ok: true`
# back for a mail that was frozen without a single threading header. Only
# a real tools/call can catch that class of drift.


def _schedule_env(tmp_path, mail_fixture) -> dict[str, str]:
    return _server_env(
        tmp_path, mail_fixture,
        EMAIL_MCP_FROM_ADDR="probe@example.invalid",
        EMAIL_MCP_FROM_NAME="Fixture Sender",
        EMAIL_MCP_IDENTITIES=str(tmp_path / "absent-identities.toml"),
        EMAIL_MCP_SEND_ALLOW_ALL="0",
    )


def _frozen_headers(env: dict[str, str], spool_id: str) -> dict[str, str | None]:
    import email
    import email.policy

    state = Path(env["EMAIL_MCP_STATE_DIR"])
    frozen = list(state.rglob(f"{spool_id}.eml"))
    assert len(frozen) == 1, f"expected one frozen .eml for {spool_id}, got {frozen}"
    msg = email.message_from_bytes(frozen[0].read_bytes(), policy=email.policy.default)
    return {
        name: (str(msg[name]) if msg[name] else None)
        for name in ("In-Reply-To", "References")
    }


def _tomorrow() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def test_schedule_email_threading_arguments_survive_the_wire(
    tmp_path, mail_fixture,
):
    """§ tools/call: `in_reply_to` and `references` are declared on the wire
    schema as optional strings, and a threaded schedule freezes both
    headers verbatim (long Message-IDs intact, never RFC 2047 encoded).
    A schedule without them is unchanged: no threading headers at all."""
    env = _schedule_env(tmp_path, mail_fixture)
    parent = "<" + "p" * 80 + "@mail.example.invalid>"   # past the 78-col fold
    root = "<root@example.invalid>"
    base = {
        "to": "probe@example.invalid", "subject": "Re: fixture topic",
        "body": "fixture body", "send_at": _tomorrow(),
    }

    async def body(session, init):
        tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = sdk_attr(tools["schedule_email"], "inputSchema", "input_schema")
        plain = _envelope(await session.call_tool("schedule_email", base))
        threaded = _envelope(await session.call_tool(
            "schedule_email",
            base | {"in_reply_to": parent, "references": root},
        ))
        return schema, plain, threaded

    schema, plain, threaded = _talk(env, body)

    props = schema["properties"]
    for name in ("in_reply_to", "references"):
        assert name in props, f"{name} missing from the wire schema"
        assert props[name].get("type") == "string"
        assert name not in schema.get("required", [])

    assert plain["ok"] is True and threaded["ok"] is True
    assert _frozen_headers(env, plain["id"]) == {
        "In-Reply-To": None, "References": None,
    }
    assert _frozen_headers(env, threaded["id"]) == {
        "In-Reply-To": parent, "References": f"{root} {parent}",
    }


def test_schedule_email_rejects_header_injection_in_threading_over_the_wire(
    tmp_path, mail_fixture,
):
    """A CR/LF smuggled into `in_reply_to` is refused as an envelope
    (`header_injection`) and nothing is frozen."""
    env = _schedule_env(tmp_path, mail_fixture)
    hostile = "<parent@example.invalid>\r\nBcc: attack@example.invalid"

    async def body(session, init):
        return await session.call_tool("schedule_email", {
            "to": "probe@example.invalid", "subject": "Re: fixture topic",
            "body": "fixture body", "send_at": _tomorrow(),
            "in_reply_to": hostile,
        })

    result = _talk(env, body)
    assert sdk_attr(result, "isError", "is_error") is False
    envelope = _envelope(result)
    assert envelope["ok"] is False
    assert envelope["code"] == "header_injection"
    state = Path(env["EMAIL_MCP_STATE_DIR"])
    assert list(state.rglob("*.eml")) == []
