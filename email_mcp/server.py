"""FastMCP server exposing the EmailSource as MCP tools.

Run as: ``python -m email_mcp.server`` (stdio) — that's what Claude Code
launches per the README's ``~/.claude.json`` snippet.

Add ``--selftest`` to do a non-MCP smoke check that prints mailbox + latest
subject counts. Useful for verifying Full-Disk-Access on a new machine.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from .config import source_name
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
# MCP wiring                                                             #
# ---------------------------------------------------------------------- #


def _build_mcp_server():  # pragma: no cover — exercised by integration only
    """Build the FastMCP Server with all six tools registered."""
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="email_mcp.server")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Print a smoke-check summary against the real Mail dir and exit.",
    )
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    # MCP stdio server — blocks until the client disconnects.
    mcp = _build_mcp_server()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
