"""FastMCP server exposing the EmailSource as MCP tools.

Run as: ``python -m email_mcp.server`` (stdio) — that's what Claude Code
launches per the README's ``~/.claude.json`` snippet.

Add ``--selftest`` to do a non-MCP smoke check that prints mailbox + latest
subject counts. Useful for verifying Full-Disk-Access on a new machine.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from .config import source_name
from .sender import SendError, reply_email, send_email
from .sources import get_source
from .sources.base import EmailSource, SearchQuery


# Lazy singleton — avoid touching ~/Library/Mail until a tool is actually called.
_SOURCE: EmailSource | None = None


def _source() -> EmailSource:
    global _SOURCE
    if _SOURCE is None:
        _SOURCE = get_source(source_name())
    return _SOURCE


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept ISO-8601 with or without timezone; assume UTC if naive.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"invalid ISO datetime: {s!r} ({e})") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses + datetimes to JSON-friendly types."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------- #
# Tool implementations (pure functions; wired into MCP below)            #
# ---------------------------------------------------------------------- #


def tool_search_emails(
    query: str = "",
    from_addr: str | None = None,
    to_addr: str | None = None,
    mailbox: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    has_attachment: bool | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    q = SearchQuery(
        query=query,
        from_addr=from_addr,
        to_addr=to_addr,
        mailbox=mailbox,
        account=account,
        before=_parse_dt(before),
        after=_parse_dt(after),
        has_attachment=has_attachment,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return [_to_jsonable(r) for r in _source().search(q)]


def tool_get_email(id: str) -> dict:
    return _to_jsonable(_source().get(id))


def tool_get_thread(thread_id: str) -> list[dict]:
    return [_to_jsonable(r) for r in _source().thread(thread_id)]


def tool_list_mailboxes() -> list[dict]:
    return [_to_jsonable(m) for m in _source().mailboxes()]


def tool_list_recent(
    mailbox: str | None = None,
    account: str | None = None,
    limit: int = 50,
) -> list[dict]:
    return [_to_jsonable(r) for r in _source().recent(mailbox, account, limit)]


def tool_get_attachment(id: str, attachment_id: str) -> dict:
    return _to_jsonable(_source().attachment(id, attachment_id))


# ---------------------------------------------------------------------- #
# refresh_mail — nudge Mail.app to fetch                                 #
# ---------------------------------------------------------------------- #


# AppleScript / osascript error codes we map to friendly diagnostics.
# Mail.app not installed (or AppleScript can't reach it).
_OSA_ERR_NO_APP = -1728
# Sending app is not authorised in Privacy & Security → Automation.
_OSA_ERR_NOT_AUTHORIZED = -1743


def _run_mail_check_for_new(timeout_seconds: float) -> dict:
    """Invoke `tell application "Mail" to check for new mail` via osascript.

    Returns a dict with at minimum {ok: bool, duration_ms: int, error?: str,
    error_code?: int}. Pure function over subprocess — mocked in tests.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "Mail" to check for new mail',
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": "osascript not found — this tool only works on macOS.",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": f"osascript timed out after {timeout_seconds:g}s.",
        }

    duration_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode == 0:
        return {"ok": True, "duration_ms": duration_ms}

    stderr = (proc.stderr or "").strip()
    code: int | None = None
    # osascript stderr looks like: "...: execution error: ... (-1743)"
    if "(-" in stderr and stderr.rstrip().endswith(")"):
        try:
            code = int(stderr.rsplit("(", 1)[1].rstrip(")"))
        except ValueError:
            code = None

    if code == _OSA_ERR_NOT_AUTHORIZED:
        msg = (
            "Mail.app automation is not authorised for this terminal. Grant it "
            "in System Settings → Privacy & Security → Automation, then retry."
        )
    elif code == _OSA_ERR_NO_APP:
        msg = "Mail.app is not installed or not reachable via AppleScript."
    else:
        msg = stderr or f"osascript failed with exit code {proc.returncode}."

    out: dict = {"ok": False, "duration_ms": duration_ms, "error": msg}
    if code is not None:
        out["error_code"] = code
    return out


def tool_refresh_mail(wait_seconds: float = 5.0, timeout_seconds: float = 30.0) -> dict:
    """Ask Mail.app to fetch new mail, then report what changed.

    Mail.app does the IMAP/OAuth work; we just nudge it. Returns a snapshot
    of the Envelope Index before and after so the caller can see how many
    new messages landed.
    """
    # Clamp into sane ranges so a misbehaving caller can't pin us forever.
    wait_seconds = max(0.0, min(60.0, float(wait_seconds)))
    timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))

    src = _source()
    snap_before = getattr(src, "freshness_snapshot", lambda: {})()

    result = _run_mail_check_for_new(timeout_seconds)

    if result["ok"] and wait_seconds > 0:
        time.sleep(wait_seconds)

    snap_after = getattr(src, "freshness_snapshot", lambda: {})()

    new_messages: int | None = None
    if snap_before and snap_after:
        b = snap_before.get("total")
        a = snap_after.get("total")
        if isinstance(a, int) and isinstance(b, int):
            new_messages = max(0, a - b)

    return {
        "ok": result["ok"],
        "applescript_duration_ms": result.get("duration_ms"),
        "waited_seconds": wait_seconds if result["ok"] else 0,
        "before": snap_before or None,
        "after": snap_after or None,
        "new_messages": new_messages,
        "error": result.get("error"),
        "error_code": result.get("error_code"),
    }


# ---------------------------------------------------------------------- #
# send_email / reply_email — the only write path                        #
# ---------------------------------------------------------------------- #


def tool_send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict:
    """Compose + send. Returns {ok, message_id, to, cc, bcc, ...} or a
    structured {ok: false, error} for caller-fixable failures."""
    try:
        res = send_email(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        return _to_jsonable(res)
    except SendError as e:
        return {"ok": False, "error": str(e)}


def tool_reply_email(
    id: str,
    body: str,
    reply_all: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    include_history: bool = True,
) -> dict:
    try:
        res = reply_email(
            _source(), id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
            include_history=include_history,
        )
        return _to_jsonable(res)
    except SendError as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------- #
# MCP wiring                                                             #
# ---------------------------------------------------------------------- #


def _build_mcp_server():  # pragma: no cover — exercised by integration only
    """Build the FastMCP Server with all nine tools registered."""
    from mcp.server.fastmcp import FastMCP  # type: ignore

    mcp = FastMCP("apple-mail")

    @mcp.tool()
    def search_emails(
        query: str = "",
        from_addr: str | None = None,
        to_addr: str | None = None,
        mailbox: str | None = None,
        account: str | None = None,
        before: str | None = None,
        after: str | None = None,
        has_attachment: bool | None = None,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search emails. `query` matches subject, sender name/address, and the
        stored snippet. All other filters are AND-combined. Returns up to 200."""
        return tool_search_emails(
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
            limit=limit, offset=offset,
        )

    @mcp.tool()
    def get_email(id: str) -> dict:
        """Get full headers, body (text + HTML), and attachment list for one
        message by its envelope id."""
        return tool_get_email(id)

    @mcp.tool()
    def get_thread(thread_id: str) -> list[dict]:
        """Get every message in a conversation, oldest first."""
        return tool_get_thread(thread_id)

    @mcp.tool()
    def list_mailboxes() -> list[dict]:
        """List all known mailboxes across all accounts, with message counts."""
        return tool_list_mailboxes()

    @mcp.tool()
    def list_recent(
        mailbox: str | None = None,
        account: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List the newest messages, optionally scoped to a mailbox/account."""
        return tool_list_recent(mailbox=mailbox, account=account, limit=limit)

    @mcp.tool()
    def get_attachment(id: str, attachment_id: str) -> dict:
        """Materialise an attachment to a tmp file and return its path. The
        caller (Claude) can then `Read` the file. Bytes are never inlined."""
        return tool_get_attachment(id, attachment_id)

    @mcp.tool()
    def refresh_mail(wait_seconds: float = 5.0, timeout_seconds: float = 30.0) -> dict:
        """Nudge Mail.app to fetch new mail, then report what changed.

        Call this when freshness matters — before answering "anything new from
        X?" or before pulling recent context. Returns before/after snapshots
        of the Envelope Index (total + newest message) so the caller can see
        how many messages arrived. The MCP itself stays read-only on disk;
        Mail.app does the IMAP work.

        Requires Automation permission for the terminal app running Claude
        Code: System Settings → Privacy & Security → Automation → <terminal>
        → Mail. On first call you'll see the macOS permission prompt; until
        granted, `ok` is false with a clear error.
        """
        return tool_refresh_mail(
            wait_seconds=wait_seconds, timeout_seconds=timeout_seconds
        )

    @mcp.tool()
    def send_email(
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        """Send an email. Composition and delivery are handled internally —
        never send mail any other way (Mail.app's scripted compose corrupts
        the body into a collapsed quote).

        `to`/`cc`/`bcc` are comma-separated address strings ("Name <a@b>" or
        "a@b"). `body` is plain text; blank lines become paragraphs. Replies
        should use `reply_email` instead so threading headers are set.

        Safety: while the allowlist guard is active (default), recipients are
        restricted to Paris's own address — a returned {ok: false, error}
        naming a blocked address means the guard fired, not a delivery
        failure. A Bcc-to-self is added automatically for a Sent record.
        Returns {ok, message_id, to, cc, bcc, subject} on success.
        """
        return tool_send_email(to=to, subject=subject, body=body, cc=cc, bcc=bcc)

    @mcp.tool()
    def reply_email(
        id: str,
        body: str,
        reply_all: bool = False,
        cc: str | None = None,
        bcc: str | None = None,
        include_history: bool = True,
    ) -> dict:
        """Reply to message `id` (an envelope id from search/get), threading
        correctly via In-Reply-To / References and an "Re:" subject.

        The original message is quoted below `body` (attribution line +
        `>`-prefixed plain text / HTML blockquote), like a normal client's
        Reply; set include_history=False for a bare reply.

        Defaults to replying to the original sender only; set reply_all=True
        to also Cc the original To+Cc (minus your own address). Same delivery
        and allowlist safety as send_email. Returns the same shape.
        """
        return tool_reply_email(
            id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
            include_history=include_history,
        )

    return mcp


def _selftest() -> int:
    src = _source()
    mboxes = src.mailboxes()
    recent = src.recent(None, None, 1)
    out = {
        "mailboxes": len(mboxes),
        "newest_subject": recent[0].subject if recent else None,
        "newest_from": recent[0].from_addr if recent else None,
        "newest_date": recent[0].date.isoformat() if recent else None,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _refresh_test(wait_seconds: float = 5.0) -> int:
    """End-to-end exercise of refresh_mail against the real Mail.app."""
    result = tool_refresh_mail(wait_seconds=wait_seconds)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def _send_test(to: str, subject: str, body: str) -> int:
    """End-to-end exercise of send_email against the real delivery path."""
    result = tool_send_email(to=to, subject=subject, body=body)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="email_mcp.server")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Print a smoke-check summary against the real Mail dir and exit.",
    )
    parser.add_argument(
        "--refresh-test",
        action="store_true",
        help="Call refresh_mail() against the real Mail.app and print the result.",
    )
    parser.add_argument(
        "--refresh-wait",
        type=float,
        default=5.0,
        help="Seconds to wait after nudging Mail.app (default 5).",
    )
    parser.add_argument(
        "--send-test",
        metavar="TO",
        help="Send a test email to TO via the real delivery path and print the "
             "result. Subject/body are canned unless --subject/--body given.",
    )
    parser.add_argument("--subject", default="email-mcp send self-test")
    parser.add_argument(
        "--body",
        default="This is a send_email self-test.\n\nSecond paragraph.\n\nParis",
    )
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    if args.refresh_test:
        return _refresh_test(wait_seconds=args.refresh_wait)
    if args.send_test:
        return _send_test(args.send_test, args.subject, args.body)
    # MCP stdio server — blocks until the client disconnects.
    mcp = _build_mcp_server()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
