"""FastMCP server exposing the EmailSource as MCP tools.

Run as: ``python -m email_mcp.server`` (stdio) — that's what Claude Code
launches per the README's ``~/.claude.json`` snippet.

Add ``--selftest`` to do a non-MCP smoke check that prints mailbox + latest
subject counts. Useful for verifying Full-Disk-Access on a new machine.
``--doctor`` runs the full environment diagnosis (email_mcp.doctor).
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
from .sender import SendError, reply_email, schedule_email, send_email
from .triage import (
    TriageError, apply_plan, build_delete_plan, build_plan, create_mailbox,
    delete_mailbox,
)
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
) -> dict:
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
    src = _source()
    results = [_to_jsonable(r) for r in src.search(q)]

    # Index health rides along with every search (honest degradation) —
    # sources without a body index simply lack the fts_status attribute.
    fts = dict(getattr(src, "fts_status", lambda: None)() or {
        "state": "unavailable", "hits": 0, "hits_capped": False,
    })
    hit_ids = {str(r) for r in fts.pop("rowids", [])}

    # body_match: the hit came in via the body index and the query is not
    # visible in what the caller already sees (subject/from/snippet).
    ql = query.lower()
    for r in results:
        visible = bool(ql) and (
            ql in r["subject"].lower()
            or ql in r["from_addr"].lower()
            or ql in r["snippet"].lower()
        )
        r["body_match"] = r["id"] in hit_ids and not visible

    return {"ok": True, "fts": fts, "results": results}


# Payload views for get_email / get_emails_batch, smallest first.
_VIEWS = ("minimal", "metadata", "full")
_BATCH_MAX_IDS = 50


def _shape_email(msg: Any, view: str) -> dict:
    """Size a full Email to the requested view.

    full     — the complete shape (ref, headers, bodies, attachments, flags)
    metadata — everything except the bodies
    minimal  — id/subject/from/date skeleton (triage_plan's message shape)
    """
    full = _to_jsonable(msg)
    if view == "full":
        return full
    if view == "metadata":
        return {k: v for k, v in full.items()
                if k not in ("body_text", "body_html")}
    ref = full["ref"]
    return {
        "id": ref["id"], "subject": ref["subject"],
        "from_addr": ref["from_addr"], "date": ref["date"],
        "mailbox": ref["mailbox"], "unread": ref["unread"],
    }


def _bad_view(view: str) -> dict:
    return {"ok": False,
            "error": f"unknown view {view!r} (want one of {_VIEWS})"}


def tool_get_email(id: str, view: str = "full") -> dict:
    if view not in _VIEWS:
        return _bad_view(view)
    return _shape_email(_source().get(id), view)


def tool_get_emails_batch(ids: list[str], view: str = "full") -> dict:
    if view not in _VIEWS:
        return _bad_view(view)
    if len(ids) > _BATCH_MAX_IDS:
        return {"ok": False,
                "error": f"{len(ids)} ids exceeds the batch cap of "
                         f"{_BATCH_MAX_IDS} — split the request"}
    src = _source()
    emails: list[dict] = []
    errors: list[dict] = []
    for id in ids:
        try:
            emails.append(_shape_email(src.get(str(id)), view))
        except (ValueError, LookupError) as e:
            errors.append({"id": str(id), "error": str(e)})
    return {"ok": True, "view": view, "emails": emails, "errors": errors}


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
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> dict:
    """Compose + send. Returns {ok, message_id, to, cc, bcc, ...} or a
    structured {ok: false, error} for caller-fixable failures."""
    try:
        res = send_email(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc,
            attachments=attachments, from_identity=from_identity,
        )
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
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> dict:
    try:
        res = reply_email(
            _source(), id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
            include_history=include_history, attachments=attachments,
            from_identity=from_identity,
        )
        return _to_jsonable(res)
    except SendError as e:
        return {"ok": False, "error": str(e)}


def tool_schedule_email(
    to: str,
    subject: str,
    body: str,
    send_at: str,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> dict:
    try:
        entry = schedule_email(
            to=to, subject=subject, body=body, send_at=send_at,
            cc=cc, bcc=bcc, attachments=attachments,
            from_identity=from_identity,
        )
        return {"ok": True, **_to_jsonable(entry)}
    except SendError as e:
        return {"ok": False, "error": str(e)}


def tool_list_scheduled(state: str | None = None, limit: int = 50) -> dict:
    from . import spool
    from .dispatcher import LAUNCHD_LABEL, _plist_path

    states = [state] if state else list(spool.STATES)
    if state and state not in spool.STATES:
        return {"ok": False,
                "error": f"unknown state {state!r} (want one of {spool.STATES})"}
    out = {s: [_to_jsonable(e) for e in spool.entries(s)][-limit:] for s in states}
    return {
        "ok": True,
        "dispatcher_installed": _plist_path().exists(),
        "dispatcher_label": LAUNCHD_LABEL,
        **out,
    }


def _triage_err(e: TriageError) -> dict:
    return {"ok": False, "code": e.code, "error": str(e)}


def _plan_payload(plan) -> dict:
    """The staged-plan response shape shared by triage_plan and
    triage_plan_delete."""
    return {
        "ok": True,
        "plan_id": plan.id,
        "count": len(plan.messages),
        "expires_at": plan.expires_at,
        "summary": plan.summary,
        "actions": [_to_jsonable(a) for a in plan.actions],
        "messages": [
            {"id": str(m.rowid), "subject": m.subject, "from_addr": m.from_addr,
             "date": m.date, "mailbox": m.mailbox, "unread": m.unread}
            for m in plan.messages
        ],
    }


def tool_triage_plan(
    query: str = "",
    from_addr: str | None = None,
    to_addr: str | None = None,
    mailbox: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    has_attachment: bool | None = None,
    unread_only: bool = False,
    limit: int = 0,
    actions: list[dict] | None = None,
) -> dict:
    from .config import triage_max_messages

    cap = triage_max_messages()
    q = SearchQuery(
        query=query, from_addr=from_addr, to_addr=to_addr,
        mailbox=mailbox, account=account,
        before=_parse_dt(before), after=_parse_dt(after),
        has_attachment=has_attachment, unread_only=unread_only,
        limit=limit if 0 < limit <= cap else cap + 1,  # +1 exposes over-cap
        offset=0,  # plans must be stable selections — no paging
    )
    try:
        plan = build_plan(_source(), q, actions)
    except TriageError as e:
        return _triage_err(e)
    return _plan_payload(plan)


def tool_triage_plan_delete(
    query: str = "",
    from_addr: str | None = None,
    to_addr: str | None = None,
    mailbox: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    has_attachment: bool | None = None,
    unread_only: bool = False,
    limit: int = 0,
) -> dict:
    from .triage import delete_max

    cap = delete_max()
    q = SearchQuery(
        query=query, from_addr=from_addr, to_addr=to_addr,
        mailbox=mailbox, account=account,
        before=_parse_dt(before), after=_parse_dt(after),
        has_attachment=has_attachment, unread_only=unread_only,
        limit=limit if 0 < limit <= cap else cap + 1,  # +1 exposes over-cap
        offset=0,  # plans must be stable selections — no paging
    )
    try:
        plan = build_delete_plan(_source(), q)
    except TriageError as e:
        return _triage_err(e)
    return _plan_payload(plan)


def tool_triage_apply(plan_id: str) -> dict:
    try:
        return apply_plan(_source(), plan_id)
    except TriageError as e:
        return _triage_err(e)


def tool_mailbox_create(account: str, path: str) -> dict:
    try:
        return create_mailbox(_source(), account, path)
    except TriageError as e:
        return _triage_err(e)


def tool_mailbox_delete(account: str, path: str) -> dict:
    try:
        return delete_mailbox(_source(), account, path)
    except TriageError as e:
        return _triage_err(e)


def tool_cancel_scheduled(id: str) -> dict:
    from . import spool

    found = spool.find(id)
    if found is None:
        return {"ok": False, "error": f"no scheduled message with id {id!r}"}
    state, entry = found
    if state != "pending":
        return {"ok": False,
                "error": f"cannot cancel {id}: status is {state!r} "
                         "(only pending messages can be cancelled)"}
    if not spool.claim(id, "pending", "cancelled"):
        return {"ok": False,
                "error": f"cannot cancel {id}: a dispatcher just claimed it"}
    entry.status = "cancelled"
    spool.update("cancelled", entry)
    return {"ok": True, "id": id, "status": "cancelled",
            "subject": entry.subject, "was_due": entry.send_at}


def tool_doctor() -> dict:
    from . import doctor

    return doctor.run()


# ---------------------------------------------------------------------- #
# MCP wiring                                                             #
# ---------------------------------------------------------------------- #


def _build_mcp_server():
    """Build the FastMCP Server: nineteen tools, or exactly the ten
    read-side tools when EMAIL_MCP_READ_ONLY=1 — the mutating nine are
    lexically gated below, so in a read-only session they never exist."""
    from mcp.server.fastmcp import FastMCP  # type: ignore

    from .config import read_only

    mcp = FastMCP("apple-mail")
    ro = read_only()

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
    ) -> dict:
        """Search emails. `query` matches subject, sender name/address, the
        stored snippet, AND full message bodies (local FTS index). All other
        filters are AND-combined.

        Returns {ok, fts, results}. `fts` reports body-index health —
        state (ready/absent/disabled), indexed/missing/backlog counts, hits
        folded into this search, hits_capped, and a `remedy` command when
        the index is absent or behind. `state: "absent"` means bodies were
        NOT searched (subject/sender/snippet only) until the index is
        built. Each result carries `body_match`: true when it matched only
        in the body (the query is not visible in subject/from/snippet)."""
        return tool_search_emails(
            query=query, from_addr=from_addr, to_addr=to_addr,
            mailbox=mailbox, account=account, before=before, after=after,
            has_attachment=has_attachment, unread_only=unread_only,
            limit=limit, offset=offset,
        )

    @mcp.tool()
    def get_email(id: str, view: str = "full") -> dict:
        """Get one message by its envelope id. `view` sizes the payload to
        the question: "full" (headers + text/HTML bodies + attachment list —
        the default), "metadata" (everything except the bodies), "minimal"
        (id/subject/from/date/mailbox/unread skeleton). Bodies are always
        read live from the mail store, never from the search index."""
        return tool_get_email(id, view=view)

    @mcp.tool()
    def get_emails_batch(ids: list[str], view: str = "full") -> dict:
        """Fetch up to 50 messages in one call — the token-efficient bulk
        read. `ids` are envelope ids from search/list/thread; `view` works
        as in get_email. Returns {ok, view, emails, errors}: per-id
        failures (bad or vanished ids) land in `errors` as data and never
        fail the batch. More than 50 ids is rejected outright ({ok: false})
        — split the request."""
        return tool_get_emails_batch(ids, view=view)

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
    def list_scheduled(state: str | None = None, limit: int = 50) -> dict:
        """List scheduled emails by state: pending (waiting), sending
        (mid-flight), sent (delivered, with delivered_at), failed (gave up
        after retries, with last_error), cancelled. Omit `state` for all.
        This is the equivalent of Mail.app's "Send Later" mailbox."""
        return tool_list_scheduled(state=state, limit=limit)

    @mcp.tool()
    def doctor() -> dict:
        """Diagnose this MCP's environment in one pass: mail-store
        readability (Full Disk Access), Mail.app Automation permission,
        Accessibility (only needed for mailbox_delete's UI fallback), the
        identities file, every transport's health (never bootstraps), the
        scheduled-send dispatcher, spool/plan hygiene, and the FTS body
        index. Returns {ok, read_only, checks} where each check is
        {ok, detail} plus a concrete `fix` (a command or a Settings pane)
        when something is off. Read-only and side-effect free — call it
        first when any other tool misbehaves."""
        return tool_doctor()

    if not ro:
        # Mutating tools — everything below can move mail or leave a
        # durable trace; under EMAIL_MCP_READ_ONLY=1 none of it registers.

        @mcp.tool()
        def send_email(
            to: str,
            subject: str,
            body: str,
            cc: str | None = None,
            bcc: str | None = None,
            attachments: list[str] | None = None,
            from_identity: str | None = None,
        ) -> dict:
            """Send an email. Composition and delivery are handled internally —
            never send mail any other way (Mail.app's scripted compose corrupts
            the body into a collapsed quote).

            `to`/`cc`/`bcc` are comma-separated address strings ("Name <a@b>" or
            "a@b"). `body` is plain text; blank lines become paragraphs. Replies
            should use `reply_email` instead so threading headers are set.

            `attachments` is a list of local file paths (each entry ONE path —
            never comma-joined). Files are attached with guessed MIME types;
            directories are refused (zip first). Total size is capped (default
            20 MB, EMAIL_MCP_MAX_ATTACH_MB) — over-budget returns {ok: false,
            error} before anything is sent.

            `from_identity` selects the sending identity from
            ~/.email-mcp/identities.toml (omit for the default). Each identity
            carries its own From: address, transport (ssh_sendmail / smtp /
            pipe) and allowlist, so which lane the mail leaves on follows from
            the name alone.

            Safety: while the allowlist guard is active (default), recipients are
            restricted to the sending identity's own address — a returned
            {ok: false, error} naming a blocked address means the guard fired,
            not a delivery failure. A Bcc-to-self is added automatically for a
            Sent record. Returns {ok, message_id, to, cc, bcc, subject,
            attachments} on success.
            """
            return tool_send_email(
                to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                attachments=attachments, from_identity=from_identity,
            )

        @mcp.tool()
        def reply_email(
            id: str,
            body: str,
            reply_all: bool = False,
            cc: str | None = None,
            bcc: str | None = None,
            include_history: bool = True,
            attachments: list[str] | None = None,
            from_identity: str | None = None,
        ) -> dict:
            """Reply to message `id` (an envelope id from search/get), threading
            correctly via In-Reply-To / References and an "Re:" subject.

            The original message is quoted below `body` (attribution line +
            `>`-prefixed plain text / HTML blockquote), like a normal client's
            Reply; set include_history=False for a bare reply.

            Defaults to replying to the original sender only; set reply_all=True
            to also Cc the original To+Cc (minus your own address). `attachments`
            works exactly as in send_email (list of local file paths, size-capped).
            Same delivery and allowlist safety as send_email. Returns the same
            shape.

            `from_identity` selects the sending identity from
            ~/.email-mcp/identities.toml (omit for the default). Each identity
            carries its own From: address, transport (ssh_sendmail / smtp /
            pipe) and allowlist — the reply goes out as that identity.
            """
            return tool_reply_email(
                id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
                include_history=include_history, attachments=attachments,
                from_identity=from_identity,
            )

        @mcp.tool()
        def schedule_email(
            to: str,
            subject: str,
            body: str,
            send_at: str,
            cc: str | None = None,
            bcc: str | None = None,
            attachments: list[str] | None = None,
            from_identity: str | None = None,
        ) -> dict:
            """Schedule an email for later delivery (the MCP's "Send Later").

            `send_at` is ISO-8601; a naive timestamp ("2026-07-29T09:00") means
            LOCAL time, an explicit offset is respected. Everything else works
            exactly like send_email (addresses, body, attachments, allowlist,
            auto Bcc-to-self).

            The message is composed and FROZEN now — attachments are embedded at
            schedule time, so later edits to the source files change nothing. A
            launchd agent checks every 60s and delivers when due; if the Mac is
            asleep at send_at, the message goes out on the first check after
            wake (same semantics as Mail.app's Send Later). Manage with
            list_scheduled / cancel_scheduled.

            `from_identity` selects the sending identity from
            ~/.email-mcp/identities.toml (omit for the default). Each identity
            carries its own From: address, transport (ssh_sendmail / smtp /
            pipe) and allowlist; the manifest records the identity so the
            dispatcher delivers on that identity's transport at fire time.

            Returns {ok, id, send_at (UTC), message_id, ...}. If the result
            warns the dispatcher is not installed, run:
            python -m email_mcp.dispatcher --install-launchd
            """
            res = tool_schedule_email(
                to=to, subject=subject, body=body, send_at=send_at,
                cc=cc, bcc=bcc, attachments=attachments,
                from_identity=from_identity,
            )
            if res.get("ok"):
                from .dispatcher import _plist_path
                if not _plist_path().exists():
                    res["warning"] = (
                        "dispatcher launchd agent NOT installed — nothing will "
                        "send. Run: python -m email_mcp.dispatcher --install-launchd"
                    )
            return res

        @mcp.tool()
        def cancel_scheduled(id: str) -> dict:
            """Cancel a pending scheduled email by id (from schedule_email /
            list_scheduled). Only pending messages can be cancelled — anything
            already sending/sent is past the point of no return."""
            return tool_cancel_scheduled(id=id)

        @mcp.tool()
        def triage_plan(
            query: str = "",
            from_addr: str | None = None,
            to_addr: str | None = None,
            mailbox: str | None = None,
            account: str | None = None,
            before: str | None = None,
            after: str | None = None,
            has_attachment: bool | None = None,
            unread_only: bool = False,
            limit: int = 0,
            actions: list[dict] | None = None,
        ) -> dict:
            """Stage a mailbox-management operation: SELECT messages with the
            same filters as search_emails, and freeze them + `actions` into a
            reviewable plan. NOTHING is modified — mutation happens only when
            triage_apply is called with the returned plan_id.

            The two-call plan/apply split is BY DESIGN: show the returned plan
            (count, summary, messages) to the user before applying. Plans
            expire after 10 minutes and cap at 200 messages (larger selections
            are rejected, never truncated — narrow the query).

            `actions` is a list of dispositions applied to every selected
            message, e.g. [{"action": "mark_read"}, {"action": "move_to",
            "mailbox": "Archive/JIRA"}]. Vocabulary: move_to (same-account,
            target must exist — see mailbox_create), mark_read, mark_unread,
            flag (color 0-6), unflag. There is deliberately NO `delete` here —
            deletion has its own tool, triage_plan_delete. There is no
            `archive` action — use move_to with your archive mailbox. Returns
            {ok, plan_id, count, expires_at, summary, messages} or
            {ok: false, code, error}.
            """
            return tool_triage_plan(
                query=query, from_addr=from_addr, to_addr=to_addr,
                mailbox=mailbox, account=account, before=before, after=after,
                has_attachment=has_attachment, unread_only=unread_only,
                limit=limit, actions=actions,
            )

        @mcp.tool()
        def triage_plan_delete(
            query: str = "",
            from_addr: str | None = None,
            to_addr: str | None = None,
            mailbox: str | None = None,
            account: str | None = None,
            before: str | None = None,
            after: str | None = None,
            has_attachment: bool | None = None,
            unread_only: bool = False,
            limit: int = 0,
        ) -> dict:
            """Stage DELETION of the selected messages — the destructive verb's
            own door (triage_plan refuses `delete`). SELECT with the same
            filters as search_emails; NOTHING is deleted by this call — review
            the returned plan (count, summary, messages) with the user, then
            execute it via triage_apply, exactly like any other plan.

            Deletion uses Mail.app's own delete verb → messages go to their
            account's Trash mailbox; nothing is erased permanently. The
            selection must live in ONE account (cross-account selections are
            rejected — add the account= filter) and caps at 50 messages
            (EMAIL_MCP_TRIAGE_DELETE_MAX), tighter than triage_plan's cap.
            Returns {ok, plan_id, count, expires_at, summary, messages} or
            {ok: false, code, error}.
            """
            return tool_triage_plan_delete(
                query=query, from_addr=from_addr, to_addr=to_addr,
                mailbox=mailbox, account=account, before=before, after=after,
                has_attachment=has_attachment, unread_only=unread_only,
                limit=limit,
            )

        @mcp.tool()
        def triage_apply(plan_id: str) -> dict:
            """Execute a plan staged by triage_plan / triage_plan_delete: one
            batched AppleScript against Mail.app (messages addressed by
            database id — fast at any mailbox size), then verification against
            Mail's own store.

            Large plans take a while (~0.2 s/message + overhead; a 200-message
            plan can run minutes) — do not re-invoke mid-flight; a second call
            safely returns plan_claimed. Requires Mail.app Automation
            permission (same as refresh_mail). Returns {ok, status, planned,
            acted, verified, failures[], pending[]} — per-message failures are
            data, not errors; `pending` means Mail's local store hasn't
            confirmed within the poll window yet, not that the action failed.
            """
            return tool_triage_apply(plan_id=plan_id)

        @mcp.tool()
        def mailbox_create(account: str, path: str) -> dict:
            """Create a mailbox/folder in an account (account = the UUID shown
            in search results; path may nest with slashes, e.g. "Archive/JIRA").
            Idempotent: returns existed=true without touching Mail if it is
            already there. index_verified=false with applescript="OK" means
            Mail created it but the local index hasn't caught up yet.

            ⚠ Exchange (EWS) accounts: AppleScript-created folders may not
            persist server-side — the result carries a warning and moves into
            such a folder can be silently reverted by the server. For Exchange,
            create folders in Mail.app/OWA and triage into them once they
            appear; mailbox_create is reliable for local (On My Mac) and
            plain-IMAP accounts."""
            return tool_mailbox_create(account=account, path=path)

        @mcp.tool()
        def mailbox_delete(account: str, path: str) -> dict:
            """Delete an EMPTY mailbox (non-empty ones are refused — triage the
            messages out first). Idempotent: already-absent returns ok with
            existed=false. The outcome is decided by a live existence re-probe,
            not by AppleScript's reply — Mail's delete verb often reports a
            false error (-10000) on success. deleted=false with ok=false means
            the mailbox genuinely survived (typical for phantom Exchange
            folders — remove those in Mail.app/OWA)."""
            return tool_mailbox_delete(account=account, path=path)

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


def _send_test(
    to: str,
    subject: str,
    body: str,
    attach: list[str] | None = None,
    from_identity: str | None = None,
) -> int:
    """End-to-end exercise of send_email against the real delivery path."""
    result = tool_send_email(
        to=to, subject=subject, body=body, attachments=attach,
        from_identity=from_identity,
    )
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def _transport_check() -> int:
    """DEPRECATED alias for --doctor: prints only the doctor's transports
    check (the old per-identity healthcheck loop, which moved to
    email_mcp.doctor.check_transports). Exit 0 only when every identity
    checks out; ok:false is a state (e.g. a cold SSH socket), not
    necessarily a bug.
    """
    from . import doctor

    check = doctor.check_transports()
    json.dump(check, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if check.get("ok") else 1


def _doctor() -> int:
    """Run every doctor check and print the full JSON report."""
    report = tool_doctor()
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


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
        default="This is a send_email self-test.\n\nSecond paragraph.",
    )
    parser.add_argument(
        "--attach",
        action="append",
        default=None,
        metavar="PATH",
        help="Attach a file to the --send-test message (repeatable).",
    )
    parser.add_argument(
        "--from-identity",
        default=None,
        metavar="NAME",
        help="Identity to send --send-test as (from ~/.email-mcp/"
             "identities.toml; default: the file's default identity).",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run every diagnostic check (permissions, identities, "
             "transports, dispatcher, spool, body index), print one JSON "
             "report, and exit 0 only when all checks are ok.",
    )
    parser.add_argument(
        "--transport-check",
        action="store_true",
        help="DEPRECATED — alias for the doctor's transports check; prints "
             "only that section. Prefer --doctor for the full picture.",
    )
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    if args.refresh_test:
        return _refresh_test(wait_seconds=args.refresh_wait)
    if args.doctor:
        return _doctor()
    if args.transport_check:
        return _transport_check()
    if args.send_test:
        return _send_test(
            args.send_test, args.subject, args.body, args.attach,
            from_identity=args.from_identity,
        )
    # MCP stdio server — blocks until the client disconnects.
    mcp = _build_mcp_server()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
