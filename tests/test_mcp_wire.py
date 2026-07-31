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
against what comes back over the wire: the frozen twenty tools, the eleven
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_ONLY_TOOLS = {
    "search_emails", "get_email", "get_emails_batch", "get_thread",
    "list_mailboxes", "list_recent", "get_attachment", "refresh_mail",
    "list_scheduled", "doctor", "audit",
}
MUTATING_TOOLS = {
    "send_email", "reply_email", "schedule_email", "cancel_scheduled",
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
_STATE_DIRS = (
    "FTS_DIR", "AUDIT_DIR", "SPOOL_DIR", "PLANS_DIR", "GRAPH_DIR",
    "ATTACH_DIR",
)


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
        d.chmod(0o700)  # what config.audit_dir guarantees
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
    return json.loads(payload.text)


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
    assert init.serverInfo.name == "apple-mail"
    assert init.protocolVersion  # negotiated, not assumed
    assert init.capabilities.tools is not None


def test_tools_list_over_the_wire_is_exactly_the_frozen_twenty(
    tmp_path, mail_fixture,
):
    async def body(session, init):
        return {t.name for t in (await session.list_tools()).tools}

    names = _talk(_server_env(tmp_path, mail_fixture), body)
    assert names == ALL_TOOLS
    assert len(names) == 20


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
        return await session.call_tool("list_mailboxes", {})

    result = _talk(_server_env(tmp_path, mail_fixture), body)
    assert result.isError is False
    envelope = _envelope(result)
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
    assert result.isError is False
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
