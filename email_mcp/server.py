"""FastMCP server exposing the EmailSource as MCP tools.

Run as: ``python -m email_mcp.server`` (stdio) — that's what Claude Code
launches per the README's ``~/.claude.json`` snippet.

Add ``--selftest`` to do a non-MCP smoke check that prints mailbox + latest
subject counts. Useful for verifying Full-Disk-Access on a new machine.
``--doctor`` runs the full environment diagnosis (email_mcp.doctor).

Every tool is a pure function returning a typed value or raising a typed
error; ``envelope.tool`` is the one boundary that turns either into the
wire dict (docs/v1-contract.md §2). The result dataclasses below ARE the
outputSchema — the freeze derives from them (envelope.schema_of).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import audit, codes, envelope, spool
from .config import source_name
from .envelope import InvalidInput, NotFound
from .log import get_logger
from .plans import PlanAction
from .sender import (
    DraftResult, SendError, SendResult, create_draft, reply_email,
    schedule_email, send_email,
)
from .triage import (
    apply_plan, build_delete_plan, build_plan, create_mailbox, delete_mailbox,
)
from .sources import get_source
from .sources.base import (
    AttachmentBlob, AttachmentRef, Email, EmailRef, EmailSource, Mailbox,
    SearchQuery,
)

_log = get_logger()


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


# ---------------------------------------------------------------------- #
# Wire result types — the success shapes of the 20 tools (contract §1).  #
# Fields typed `dict` are dynamic passthroughs the types cannot see      #
# into (fts health, doctor checks, audit events, freshness snapshots).   #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SearchHit(EmailRef):
    """An EmailRef plus body_match: true when the hit came in via the body
    index and the query is not visible in subject/from/snippet."""
    body_match: bool = False


@dataclass(frozen=True)
class SearchPage:
    fts: dict
    results: list[SearchHit]
    note: str | None       # why an empty mailbox-scoped page is empty


@dataclass(frozen=True)
class EmailMetadata:
    """view="metadata": everything except the bodies."""
    ref: EmailRef
    headers: dict
    attachments: list[AttachmentRef]
    flags: dict


@dataclass(frozen=True)
class EmailMinimal:
    """view="minimal": the id/subject/from/date skeleton (triage_plan's
    message shape). view="full" is sources.base.Email itself."""
    id: str
    subject: str
    from_addr: str
    date: datetime
    mailbox: str
    unread: bool


@dataclass(frozen=True)
class OneEmail:
    email: Email | EmailMetadata | EmailMinimal


@dataclass(frozen=True)
class BatchItemError:
    """Per-id failure inside get_emails_batch — data, never tool failure."""
    id: str
    error: str
    code: str


@dataclass(frozen=True)
class EmailBatch:
    view: str
    emails: list[Email | EmailMetadata | EmailMinimal]
    errors: list[BatchItemError]


@dataclass(frozen=True)
class Thread:
    thread: list[EmailRef]


@dataclass(frozen=True)
class MailboxList:
    mailboxes: list[Mailbox]


@dataclass(frozen=True)
class RecentPage:
    messages: list[EmailRef]
    note: str | None       # why an empty mailbox-scoped page is empty


@dataclass(frozen=True)
class AttachmentOut:
    attachment: AttachmentBlob


@dataclass(frozen=True)
class RefreshOutcome:
    """refresh_mail's report. `ok` is the nudge outcome, not tool failure
    (§2 documented exception); `code` is the §3.3 mapping of error_code."""
    ok: bool
    applescript_duration_ms: int | None
    waited_seconds: float
    before: dict | None
    after: dict | None
    new_messages: int | None
    error: str | None
    error_code: int | None
    code: str | None


@dataclass(frozen=True)
class PlanMessageOut:
    id: str
    subject: str
    from_addr: str
    date: str
    mailbox: str
    unread: bool


@dataclass(frozen=True)
class PlanReceipt:
    """The staged-plan shape shared by triage_plan and triage_plan_delete."""
    plan_id: str
    count: int
    expires_at: str
    summary: str
    actions: list[PlanAction]
    messages: list[PlanMessageOut]


@dataclass(frozen=True)
class CancelReceipt:
    id: str
    status: str
    subject: str
    was_due: str


@dataclass(frozen=True)
class AuditPage:
    events: list[dict]
    files_scanned: int
    skipped_lines: int


# ---------------------------------------------------------------------- #
# Tool implementations (pure functions; wired into MCP below)            #
# ---------------------------------------------------------------------- #


@envelope.tool
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
) -> SearchPage:
    _check_page(limit)
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
    hits = src.search(q)

    # Index health rides along with every search (honest degradation) —
    # sources without a body index simply lack the fts_status attribute.
    fts = dict(getattr(src, "fts_status", lambda: None)() or {
        "state": "unavailable", "hits": 0, "hits_capped": False,
    })
    hit_ids = {str(r) for r in fts.pop("rowids", [])}

    ql = query.lower()
    results = []
    for r in hits:
        visible = bool(ql) and (
            ql in r.subject.lower()
            or ql in r.from_addr.lower()
            or ql in r.snippet.lower()
        )
        results.append(SearchHit(
            **asdict(r), body_match=r.id in hit_ids and not visible))
    # An empty page scoped to a server-side-only mailbox must say why —
    # the emptiness is real; only the silence was the bug.
    note = _empty_scope_note(src, mailbox, account) \
        if mailbox and not results else None
    return SearchPage(fts=fts, results=results, note=note)


# Payload views for get_email / get_emails_batch, smallest first.
_VIEWS = ("minimal", "metadata", "full")
_BATCH_MAX_IDS = 50

# One page of search/list/scheduled results. Caps REJECT, never truncate
# (contract §5) — and they exist for the server's own survival: `limit` had
# no ceiling, so a 20,000-row search pulled the whole corpus into a single
# envelope and took the server down on the first real user's machine.
_PAGE_MAX = 500


def _check_page(limit: int) -> None:
    if not 0 < limit <= _PAGE_MAX:
        raise InvalidInput(f"limit {limit} is outside 1..{_PAGE_MAX} — "
                           "lower it and paginate")


def _empty_scope_note(src: EmailSource, mailbox: str,
                      account: str | None) -> str | None:
    """Why an empty mailbox-scoped page is empty, when the store can say:
    Gmail-style accounts advertise server-side counts for mailboxes that
    hold ZERO local rows (their messages live only under [Gmail]/All
    Mail), and an unexplained [] from such a scope read as tool failure
    in the field (2026-08-01). Scope matching mirrors the source's
    mailbox/account filters: URL substring over the quoted name."""
    tail = "/" + urllib.parse.quote(mailbox)
    scoped = [b for b in src.mailboxes()
              if tail in b.path
              and (not account or f"//{account}/" in b.path)]
    server_side = sum(b.total for b in scoped)
    if not scoped or not server_side or any(b.local_count for b in scoped):
        return None
    return (f"mailbox {mailbox!r} holds {server_side} message(s) "
            "server-side but none in the local store — Gmail accounts "
            "keep local copies only under [Gmail]/All Mail; search "
            "there, or drop the mailbox filter.")


def _shape_email(msg: Email, view: str) -> Email | EmailMetadata | EmailMinimal:
    """Size a full Email to the requested view."""
    if view == "full":
        return msg
    if view == "metadata":
        return EmailMetadata(ref=msg.ref, headers=msg.headers,
                             attachments=msg.attachments, flags=msg.flags)
    ref = msg.ref
    return EmailMinimal(id=ref.id, subject=ref.subject,
                        from_addr=ref.from_addr, date=ref.date,
                        mailbox=ref.mailbox, unread=ref.unread)


def _check_view(view: str) -> None:
    if view not in _VIEWS:
        raise InvalidInput(f"unknown view {view!r} (want one of {_VIEWS})")


@envelope.tool
def tool_get_email(id: str, view: str = "full") -> OneEmail:
    _check_view(view)
    return OneEmail(email=_shape_email(_source().get(id), view))


@envelope.tool
def tool_get_emails_batch(ids: list[str], view: str = "full") -> EmailBatch:
    _check_view(view)
    if len(ids) > _BATCH_MAX_IDS:
        raise InvalidInput(f"{len(ids)} ids exceeds the batch cap of "
                           f"{_BATCH_MAX_IDS} — split the request")
    src = _source()
    emails: list[Email | EmailMetadata | EmailMinimal] = []
    errors: list[BatchItemError] = []
    for id in ids:
        try:
            emails.append(_shape_email(src.get(str(id)), view))
        except (ValueError, LookupError) as e:
            errors.append(BatchItemError(id=str(id), error=str(e),
                                         code=envelope.classify(e)))
    return EmailBatch(view=view, emails=emails, errors=errors)


@envelope.tool
def tool_get_thread(thread_id: str) -> Thread:
    return Thread(thread=list(_source().thread(thread_id)))


@envelope.tool
def tool_list_mailboxes() -> MailboxList:
    return MailboxList(mailboxes=list(_source().mailboxes()))


@envelope.tool
def tool_list_recent(
    mailbox: str | None = None,
    account: str | None = None,
    limit: int = 50,
) -> RecentPage:
    _check_page(limit)
    src = _source()
    messages = list(src.recent(mailbox, account, limit))
    note = _empty_scope_note(src, mailbox, account) \
        if mailbox and not messages else None
    return RecentPage(messages=messages, note=note)


@envelope.tool
def tool_get_attachment(id: str, attachment_id: str) -> AttachmentOut:
    return AttachmentOut(attachment=_source().attachment(id, attachment_id))


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


@envelope.tool
def tool_refresh_mail(wait_seconds: float = 5.0, timeout_seconds: float = 30.0) -> RefreshOutcome:
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

    error_code = result.get("error_code")
    return RefreshOutcome(
        ok=result["ok"],
        applescript_duration_ms=result.get("duration_ms"),
        waited_seconds=wait_seconds if result["ok"] else 0.0,
        before=snap_before or None,
        after=snap_after or None,
        new_messages=new_messages,
        error=result.get("error"),
        error_code=error_code,
        # §3.3: the numeric osascript code additionally carries its mapped
        # string code from the one namespace.
        code=codes.OSA_CODE_MAP.get(error_code) if error_code is not None
        else None,
    )


# ---------------------------------------------------------------------- #
# send_email / reply_email — the only write path                        #
# ---------------------------------------------------------------------- #


@envelope.tool
def tool_send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> SendResult:
    """Compose + send. Every terminal outcome records ONE `send` ledger
    event (transmission family: failures are ledger-worthy too — an
    attempt to transmit); the SendError's §3.4 code rides the wire."""
    try:
        res = send_email(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc,
            attachments=attachments, from_identity=from_identity,
        )
    except SendError as e:
        audit.emit("send", outcome="failed", tool="send_email",
                   subject=subject, detail={"error": str(e)[:300]})
        raise
    audit.emit("send", outcome="sent", tool="send_email",
               message_id=res.message_id, identity=from_identity,
               to=res.to, cc=res.cc or None, bcc=res.bcc or None,
               subject=res.subject,
               detail={"attachments": res.attachments}
               if res.attachments else None)
    return res


@envelope.tool
def tool_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    in_reply_to: str = "",
    from_identity: str | None = None,
) -> DraftResult:
    """The `draft` audit event records composition, not transmission
    (contract §6, additive 2026-08-02) — the ledger is never blind to
    what was composed, even though nothing leaves the machine."""
    try:
        res = create_draft(
            to=to, subject=subject, body=body, cc=cc,
            in_reply_to=in_reply_to, from_identity=from_identity,
        )
    except SendError as e:
        audit.emit("draft", outcome="failed", tool="create_draft",
                   subject=subject, detail={"error": str(e)[:300]})
        raise
    audit.emit("draft", outcome="created", tool="create_draft",
               message_id=res.message_id, identity=from_identity,
               to=res.to, cc=res.cc or None, subject=res.subject,
               detail={"draft_id": res.draft_id, "account": res.account})
    return res


@envelope.tool
def tool_reply_email(
    id: str,
    body: str,
    reply_all: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    include_history: bool = True,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> SendResult:
    try:
        res = reply_email(
            _source(), id=id, body=body, reply_all=reply_all, cc=cc, bcc=bcc,
            include_history=include_history, attachments=attachments,
            from_identity=from_identity,
        )
    except SendError as e:
        # The reply context (which message, whether reply-all) is lost
        # below the sender return — record it here, at the tool layer.
        audit.emit("reply", outcome="failed", tool="reply_email",
                   detail={"orig_id": id, "reply_all": reply_all,
                           "error": str(e)[:300]})
        raise
    audit.emit("reply", outcome="sent", tool="reply_email",
               message_id=res.message_id, identity=from_identity,
               to=res.to, cc=res.cc or None, bcc=res.bcc or None,
               subject=res.subject,
               detail={"orig_id": id, "reply_all": reply_all})
    return res


@envelope.tool
def tool_schedule_email(
    to: str,
    subject: str,
    body: str,
    send_at: str,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[str] | None = None,
    from_identity: str | None = None,
) -> spool.Entry:
    try:
        entry = schedule_email(
            to=to, subject=subject, body=body, send_at=send_at,
            cc=cc, bcc=bcc, attachments=attachments,
            from_identity=from_identity,
        )
    except SendError as e:
        audit.emit("schedule", outcome="failed", tool="schedule_email",
                   subject=subject, detail={"error": str(e)[:300]})
        raise
    # graph_fallback: the identity asked for Exchange-side deferred send
    # but the entry landed on launchd — the silent-fallback decision is
    # only recoverable here, at the tool layer.
    graph_fallback = False
    if entry.executor == "launchd":
        from . import identities
        try:
            graph_fallback = identities.get(entry.identity).executor == "graph"
        except SendError:
            pass  # identities unreadable mid-call: not a fallback signal
    audit.emit(
        "schedule", outcome="scheduled", operation_id=entry.id,
        tool="schedule_email", spool_id=entry.id,
        message_id=entry.message_id, identity=entry.identity,
        to=entry.to, cc=entry.cc or None, bcc=entry.bcc or None,
        subject=entry.subject,
        detail={"executor": entry.executor, "send_at": entry.send_at,
                "draft_id": entry.graph_draft_id,
                "graph_fallback": graph_fallback},
    )
    return entry


@envelope.tool
def tool_list_scheduled(state: str | None = None, limit: int = 50) -> dict:
    from .dispatcher import LAUNCHD_LABEL, _plist_path

    if state and state not in spool.STATES:
        raise InvalidInput(f"unknown state {state!r} "
                           f"(want one of {spool.STATES})")
    # The page gate also closes the slice hole: `[-limit:]` with limit=0
    # is `[0:]` — the WHOLE spool, the opposite of "give me nothing".
    _check_page(limit)
    states = [state] if state else list(spool.STATES)
    return {
        "dispatcher_installed": _plist_path().exists(),
        "dispatcher_label": LAUNCHD_LABEL,
        **{s: spool.entries(s)[-limit:] for s in states},
    }


def _plan_receipt(plan) -> PlanReceipt:
    return PlanReceipt(
        plan_id=plan.id,
        count=len(plan.messages),
        expires_at=plan.expires_at,
        summary=plan.summary,
        actions=list(plan.actions),
        messages=[
            PlanMessageOut(id=str(m.rowid), subject=m.subject,
                           from_addr=m.from_addr, date=m.date,
                           mailbox=m.mailbox, unread=m.unread)
            for m in plan.messages
        ],
    )


@envelope.tool
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
) -> PlanReceipt:
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
    return _plan_receipt(build_plan(_source(), q, actions))


@envelope.tool
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
) -> PlanReceipt:
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
    return _plan_receipt(build_delete_plan(_source(), q))


@envelope.tool(op_from="plan_id")
def tool_triage_apply(plan_id: str) -> dict:
    return apply_plan(_source(), plan_id)


@envelope.tool
def tool_mailbox_create(account: str, path: str) -> dict:
    out = create_mailbox(_source(), account, path)
    # Store family: emit only on actual change — the idempotent
    # already-there path (existed=true) leaves no ledger event.
    if out.get("existed") is False:
        audit.emit("mailbox_create", outcome="created",
                   tool="mailbox_create", account=account, mailbox=path)
    return out


@envelope.tool
def tool_mailbox_delete(account: str, path: str) -> dict:
    out = delete_mailbox(_source(), account, path)
    # Emit only when a deletion was actually issued at Mail (existed) —
    # the idempotent already-absent path leaves no ledger event.
    if out.get("existed"):
        outcome = "deleted" if out.get("deleted") \
            else out.get("code", "delete_failed")
        audit.emit("mailbox_delete", outcome=outcome, tool="mailbox_delete",
                   account=account, mailbox=path,
                   detail={"method": out["method"]}
                   if out.get("method") else None)
    return out


@envelope.tool
def tool_cancel_scheduled(id: str) -> CancelReceipt:
    def _finish(outcome: str, *, reason: str | None = None,
                subject: str | None = None, **extra) -> None:
        """The ONE cancel emit: every terminal exit records exactly one
        `cancel` event; op = the spool id (the artifact-id rule threads
        it to the entry's schedule/deliver events)."""
        detail = {"reason": reason, **extra} if reason else None
        audit.emit("cancel", outcome=outcome, operation_id=id,
                   tool="cancel_scheduled", spool_id=id, subject=subject,
                   detail=detail)

    found = spool.find(id)
    if found is None:
        _finish("failed", reason="not_found")
        raise NotFound(f"no scheduled message with id {id!r}")
    state, entry = found
    if state != "pending":
        _finish("failed", reason="not_pending", subject=entry.subject,
                state=state)
        raise InvalidInput(
            f"cannot cancel {id}: status is {state!r} "
            "(only pending messages can be cancelled)",
            operation_id=id)

    if entry.executor == "graph":
        # Exchange holds an armed deferred draft — revoke it FIRST; the
        # local manifest only moves to cancelled/ once Exchange's claim is
        # confirmed gone. On any ambiguity the entry stays pending.
        from . import graph, identities

        try:
            ident = identities.get(entry.identity)
        except SendError as e:
            _finish("failed", reason="identity_unavailable",
                    subject=entry.subject)
            raise SendError(f"cannot cancel {id}: {e}", code=e.code,
                            operation_id=id) from e
        try:
            draft_id = entry.graph_draft_id
            if draft_id is None:
                # Crash-window entry: the draft (if any) is unrecorded —
                # the frozen Message-ID is the recovery key.
                draft_id = graph.find_draft_by_message_id(
                    ident, entry.message_id)
            outcome = (graph.delete_draft(ident, draft_id)
                       if draft_id else "gone")
        except SendError as e:
            _finish("failed", reason="revoke_failed", subject=entry.subject)
            raise SendError(
                f"cannot cancel {id}: Exchange still holds the deferred "
                f"draft and the revoke failed ({e}). Retry, or discard the "
                "draft in Outlook/OWA yourself, then cancel again.",
                code=e.code, operation_id=id) from e
        if outcome == "gone":
            # F10 race: the draft vanished on its own — did Exchange
            # already send it? Only Sent Items can say.
            try:
                sent = graph.sent_by_message_id(ident, entry.message_id)
            except SendError as e:
                _finish("failed", reason="sent_check_failed",
                        subject=entry.subject)
                raise SendError(
                    f"cannot cancel {id}: the deferred draft is gone but "
                    f"Sent Items could not be checked ({e}) — outcome "
                    "ambiguous, retry.",
                    code=e.code, operation_id=id) from e
            if sent:
                # Atomic ownership hand-off (same rename fence as the
                # cancel below) — a concurrently reconciling dispatcher
                # must not race this terminal move.
                if not spool.claim(id, "pending", "sent"):
                    _finish("failed", reason="claim_lost",
                            subject=entry.subject)
                    raise InvalidInput(
                        f"cannot cancel {id}: a dispatcher just moved it — "
                        "re-check list_scheduled",
                        operation_id=id)
                entry.delivered_at = spool.iso(spool.utcnow())
                entry.next_attempt_at = None
                entry.last_error = None
                entry.status = "sent"
                spool.update("sent", entry)
                # Terminal state change, ok:false on the wire — still a
                # ledger-worthy outcome of its own. The failure keeps the
                # entry's terminal state as data (v0.10 wire keys).
                _finish("too_late_sent", subject=entry.subject)
                raise InvalidInput(
                    f"cannot cancel {id}: Exchange already sent it (found "
                    "in Sent Items) — the entry has been moved to sent/.",
                    operation_id=id, data={"id": id, "status": "sent"})
            # Confirmed absent from Drafts AND Sent Items: nothing is
            # armed (someone may have discarded it in OWA) — proceed as
            # revoked and cancel the local entry below.

    if not spool.claim(id, "pending", "cancelled"):
        _finish("failed", reason="claim_lost", subject=entry.subject)
        raise InvalidInput(f"cannot cancel {id}: a dispatcher just claimed it",
                           operation_id=id)
    entry.status = "cancelled"
    spool.update("cancelled", entry)
    _finish("cancelled", subject=entry.subject)
    return CancelReceipt(id=id, status="cancelled", subject=entry.subject,
                         was_due=entry.send_at)


@envelope.tool
def tool_doctor() -> dict:
    from . import doctor

    return doctor.run()


_ISO_BOUND_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


def _valid_iso_bound(value: str) -> bool:
    """A ledger query bound is either a calendar prefix (2026, 2026-07,
    2026-07-29) or a full ISO-8601 timestamp."""
    if _ISO_BOUND_RE.fullmatch(value):
        return True
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


@envelope.tool
def tool_audit(
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    event: str | None = None,
    plan_id: str | None = None,
    operation_id: str | None = None,
    limit: int = 50,
) -> AuditPage:
    for name, value in (("since", since), ("until", until)):
        if value is not None and not _valid_iso_bound(str(value)):
            raise InvalidInput(
                f"invalid ISO datetime for `{name}`: {value!r} "
                "(want ISO-8601; prefixes allowed, e.g. "
                "2026-07 or 2026-07-29)")
    return AuditPage(**audit.query(
        since=since, until=until, tool=tool, event=event,
        plan_id=plan_id, operation_id=operation_id, limit=limit))


# ---------------------------------------------------------------------- #
# MCP wiring                                                             #
# ---------------------------------------------------------------------- #


def _build_mcp_server():
    """Build the FastMCP Server: twenty-one tools, or exactly the eleven
    read-side tools when EMAIL_MCP_READ_ONLY=1 — the mutating ten are
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
        filters are AND-combined. `from_addr`/`to_addr` are case-insensitive
        SUBSTRING matches over both address and display name — e.g.
        from_addr="google.com" matches every sender at that domain.

        Returns {ok, fts, results}. `fts` reports body-index health —
        state (ready/absent/disabled), indexed/missing/backlog counts, hits
        folded into this search, hits_capped, and a `remedy` command when
        the index is absent or behind. `state: "absent"` means bodies were
        NOT searched (subject/sender/snippet only) until the index is
        built. Each result carries `body_match`: true when it matched only
        in the body (the query is not visible in subject/from/snippet).
        An empty page scoped to a mailbox that exists only server-side
        (see list_mailboxes' local_count) carries a `note` saying why."""
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
        (id/subject/from/date/mailbox/unread skeleton). Body provenance is
        declared, never implied: `body_source` is null when the body was
        read live from the mail store (the normal case), and
        "server_backfill" when Mail never downloaded it and `body_text`
        is the server copy the search index fetched (Exchange via
        Graph, Gmail/IMAP via the imap lane) — plain
        text only (no `body_html`), capped at the index's per-doc limit
        with a visible "[…body truncated…]" marker when cut.
        Returns {ok, email}."""
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
    def get_thread(thread_id: str) -> dict:
        """Get every message in a conversation, oldest first.
        Returns {ok, thread}."""
        return tool_get_thread(thread_id)

    @mcp.tool()
    def list_mailboxes() -> dict:
        """List all known mailboxes across all accounts. Each entry
        carries two counts: `total` (what the account reports server-side)
        and `local_count` (messages actually in the local store — what
        search/list can serve). Gmail label mailboxes typically show
        total > 0 with local_count 0: their local copies live only under
        [Gmail]/All Mail. Returns {ok, mailboxes}."""
        return tool_list_mailboxes()

    @mcp.tool()
    def list_recent(
        mailbox: str | None = None,
        account: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List the newest messages, optionally scoped to a mailbox/account.
        An empty page scoped to a mailbox that exists only server-side
        (see list_mailboxes' local_count) carries a `note` saying why.
        Returns {ok, messages}."""
        return tool_list_recent(mailbox=mailbox, account=account, limit=limit)

    @mcp.tool()
    def get_attachment(id: str, attachment_id: str) -> dict:
        """Materialise an attachment to a tmp file and return its path. The
        caller (Claude) can then `Read` the file. Bytes are never inlined.
        Returns {ok, attachment}."""
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
        This is the equivalent of Mail.app's "Send Later" mailbox.

        Each entry carries `executor`: "launchd" means the local dispatcher
        delivers it (Mac must be awake at/after send_at); "graph" means
        Exchange holds an armed deferred draft (`graph_draft_id`) and sends
        it server-side even with the Mac off — such entries stay pending
        until a dispatcher pass confirms the outcome."""
        return tool_list_scheduled(state=state, limit=limit)

    @mcp.tool()
    def doctor() -> dict:
        """Diagnose this MCP's environment in one pass: mail-store
        readability (Full Disk Access), Mail.app Automation permission,
        Accessibility (only needed for mailbox_delete's UI fallback), the
        identities file, every transport's health (never bootstraps), the
        scheduled-send dispatcher, spool/plan hygiene, the FTS body index,
        and the audit ledger (reported as the top-level `audit` section).
        Returns {ok, read_only, checks, audit} where each check is
        {ok, detail} plus a concrete `fix` (a command or a Settings pane)
        when something is off. Checks marked `advisory: true` (an optional
        extra's permission, e.g. Accessibility) warn without flipping the
        top-level `ok`. Read-only and side-effect free — call it first
        when any other tool misbehaves."""
        return tool_doctor()

    @mcp.tool()
    def audit(
        since: str | None = None,
        until: str | None = None,
        tool: str | None = None,
        event: str | None = None,
        plan_id: str | None = None,
        operation_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Read the append-only audit ledger — the MCP's own receipts.
        Every mutation records exactly one event (send, reply, schedule,
        deliver, cancel, recover, plan_create, plan_finish,
        mailbox_create, mailbox_delete, and the graph_* reconcile
        outcomes), so "what did the tool change yesterday?" is one call:
        audit(since=...). Events outlive the artifacts they index (plan
        files are GC'd after 7 days; the ledger keeps their summary).

        Filters AND-combine. `since`/`until` are ISO-8601 bounds —
        prefixes are allowed and inclusive ("2026-07" means that whole
        month). `tool` filters by the emitting tool name, `event` by
        event name, `plan_id` by triage plan. `operation_id` threads one
        operation across processes: a scheduled send's `schedule` and
        `deliver` events share it (it is the spool id; for triage it is
        the plan id).

        Returns {ok, events, files_scanned, skipped_lines} with events
        newest-first (`limit` clamped to 1..500, default 50).
        `skipped_lines` counts torn or corrupt ledger lines that were
        tolerated and skipped — non-zero flags ledger damage but never
        fails the call. Events carry recipients and subjects, NEVER
        message bodies."""
        return tool_audit(
            since=since, until=until, tool=tool, event=event,
            plan_id=plan_id, operation_id=operation_id, limit=limit,
        )

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

            Safety: recipients are unrestricted by default; an identity that
            DECLARES a guard (an allowlist, or allow_all = false) is
            restricted to that allowlist plus its own address — a returned
            {ok: false, error} naming a blocked address means the declared
            guard fired, not a delivery failure. A Bcc-to-self is added
            automatically for a Sent record. Returns {ok, message_id, to,
            cc, bcc, subject, attachments} on success.
            """
            return tool_send_email(
                to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                attachments=attachments, from_identity=from_identity,
            )

        @mcp.tool()
        def create_draft(
            to: str,
            subject: str,
            body: str,
            cc: str | None = None,
            in_reply_to: str = "",
            from_identity: str | None = None,
        ) -> dict:
            """Create a DRAFT in the identity's own Drafts folder — it is
            NEVER sent, and this tool has no way to send it. The draft
            appears in Mail.app, Outlook web and the phone (it is filed
            server-side), ready for the human to edit and send from their
            own mail client.

            Only identities with a declared drafts lane can file
            (currently Exchange/Microsoft accounts, drafts = "graph" in
            ~/.email-mcp/identities.toml — `email-mcp setup` enables it
            with one browser sign-in). Other identities return
            {ok: false, code: "draft_unsupported"} with the enable steps;
            the tool never files into a different account instead.

            `to`/`cc` are comma-separated address strings. `body` is
            plain text (paragraph breaks preserved; an HTML alternative
            is composed automatically). Attachments are not supported on
            drafts. Returns {ok, draft_id, message_id, to, cc, subject,
            folder, account} — `draft_id` is the server-side id; search
            locally by subject once Mail syncs.
            """
            return tool_create_draft(
                to=to, subject=subject, body=body, cc=cc,
                in_reply_to=in_reply_to, from_identity=from_identity,
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

            Identities with `executor = "graph"` additionally hand the frozen
            message to Exchange as a deferred-send draft, so it transmits at
            send_at even with the Mac off; if Graph refuses, the entry falls
            back silently to the local launchd path. The returned entry's
            `executor` field says which path holds it.

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
            already sending/sent is past the point of no return.

            For executor="graph" entries the Exchange deferred draft is
            revoked first; if the revoke fails the entry stays pending and
            the error says how to discard the draft in Outlook/OWA. If
            Exchange already sent it, the result is {ok: false} saying so and
            the entry moves to sent/."""
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
            triage_apply is called with the returned plan_id. As in
            search_emails, `from_addr` is a case-insensitive substring match
            over both address and display name.

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
            filters as search_emails, EXCEPT `from_addr`: here it is an EXACT
            full-address match (case-insensitive, never a substring), so a
            fragment like "google.com" selects nothing instead of staging a
            domain-wide delete. NOTHING is deleted by this call — review
            the returned plan (count, summary, messages) with the user, then
            execute it via triage_apply, exactly like any other plan.

            Deletion uses Mail.app's own delete verb → messages go to their
            account's Trash mailbox; nothing is erased permanently. The
            selection never includes Trash mailboxes, so already-trashed
            copies are not re-selected and plan → apply → re-plan converges.
            The selection must live in ONE account (cross-account selections
            are rejected — add the account= filter) and caps at 50 messages
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


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
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
