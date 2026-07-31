"""FastMCP server exposing the EmailSource as MCP tools.

Run as: ``python -m email_mcp.server`` (stdio) — that's what Claude Code
launches per the README's ``~/.claude.json`` snippet.

Add ``--selftest`` to do a non-MCP smoke check that prints mailbox + latest
subject counts. Useful for verifying Full-Disk-Access on a new machine.
``--doctor`` runs the full environment diagnosis (email_mcp.doctor).
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from . import __version__, audit, codes, config, ids
from .config import source_name
from .log import get_logger
from .sender import SendError, reply_email, schedule_email, send_email
from .triage import (
    TriageError, apply_plan, build_delete_plan, build_plan, create_mailbox,
    delete_mailbox,
)
from .sources import get_source
from .sources.base import EmailSource, SearchQuery

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
# Wire-safety belt (docs/v1-contract.md §3.5 / §7)                        #
# ---------------------------------------------------------------------- #


# Generous for genuine one-line prose, small enough that no failure envelope
# can carry a caller's payload back across the wire.
_MAX_ERROR_CHARS = 2000

# A message id is an Envelope Index ROWID or a minted spool id — tens of
# bytes. Bounded in the same unit as _clip so the two cannot disagree.
_MAX_ID_BYTES = 256


def _clip(text: str) -> str:
    """Bound one line of failure prose (contract §7).

    Applies to EVERY `ok: false` site, not just the belt's. Most failure
    messages quote the offending argument back — `unknown view {view!r}`,
    `cannot cancel {id}` — so a 60 KB argument returns as a 60 KB envelope
    on a stdio transport unless every construction site is bounded. Clipping
    only inside the belt closed one door and left twenty open.

    Measured in UTF-8 BYTES, not characters: 2 000 astral-plane characters
    JSON-escape to ~24 KB of ASCII, so a character cap bounds the prose
    while leaving the wire payload unbounded — the thing §7 actually
    promises. `errors="ignore"` drops a split trailing codepoint cleanly.
    """
    if not isinstance(text, str):
        return text
    raw = text.encode("utf-8")
    if len(raw) <= _MAX_ERROR_CHARS:
        return text
    cut = raw[:_MAX_ERROR_CHARS].decode("utf-8", errors="ignore")
    return cut + f"… [truncated, {len(text)} chars; see log]"


def _bound(result):
    """Clip the `error` prose of a failure envelope on its way out.

    Every one of the 20 tools is belted, so this is the single choke point
    every envelope crosses — a new `ok: false` site cannot bypass it, which
    is why the bound lives here rather than at the twenty construction
    sites. Success envelopes and per-id `errors[]` entries are shaped data,
    not prose, and pass through untouched except for their own `error`.
    """
    if not isinstance(result, dict):
        return result
    if result.get("ok") is False and isinstance(result.get("error"), str):
        result["error"] = _clip(result["error"])
    # get_emails_batch reports per-id failures as data inside ok: true.
    # Clip the `id` too: it is str(caller_argument), echoed once per entry —
    # 50 ids at the batch cap × a 60 KB id was a 3 MB envelope that §5's
    # over-cap reject never sees because 50 is AT the cap, not over it.
    entries = result.get("errors")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                for key in ("error", "id"):
                    if isinstance(entry.get(key), str):
                        entry[key] = _clip(entry[key])
    return result


def _classify(e: BaseException) -> tuple[str, bool]:
    """Map an exception to its §3 code, and whether the prose should name the
    exception type.

    THE single class→code map: the belt and `get_emails_batch`'s per-id loop
    both go through here, so one store fault cannot yield two different codes
    depending on which tool the caller happened to use (contract §3: "a code
    means the same thing on every tool that uses it"). Order matters — each
    clause below is a subclass of a later one.
    """
    if isinstance(e, UnicodeDecodeError):
        # A ValueError subclass, but NOT the caller's fault: MCP arguments
        # arrive as valid str, so undecodable bytes come from the store or a
        # bug (audit finding F6).
        return codes.INTERNAL_ERROR, True
    if isinstance(e, ValueError):
        return codes.INVALID_INPUT, False
    if isinstance(e, (KeyError, IndexError)):
        # LookupError subclasses, but sources signal unknown ids with bare
        # LookupError — a KeyError/IndexError here is an internal container
        # miss, not a caller reference that wasn't found (F6).
        return codes.INTERNAL_ERROR, True
    if isinstance(e, LookupError):
        return codes.NOT_FOUND, False
    if isinstance(e, sqlite3.ProgrammingError):
        # A DatabaseError sibling of OperationalError, but it means our own
        # SQL or parameter binding is wrong (closed connection, wrong
        # placeholder count) — a bug, not an unreadable store.
        return codes.INTERNAL_ERROR, True
    if isinstance(e, (FileNotFoundError, PermissionError, sqlite3.DatabaseError)):
        # The store (or its SQLite index) is not readable — missing Mail
        # setup, missing Full Disk Access, locked or corrupt index (§3.5).
        # DatabaseError, not just OperationalError: a junk Envelope Index
        # raises "file is not a database" as a bare DatabaseError on some
        # paths and OperationalError on others, and one store must not
        # yield two codes.
        return codes.MAIL_UNAVAILABLE, False
    return codes.INTERNAL_ERROR, True


def _belt(op_from: str | None = None, *, config_gate: bool = True):
    """No exception escapes a dict-returning tool to the MCP wire: known
    caller-fixable classes map to coded envelopes, anything else is the
    belt of last resort, `internal_error`. Every catch logs the FULL
    traceback to the file log (never the wire) and the envelope carries
    fix: "run doctor". Success shapes pass through byte-untouched — the
    belt only exists where the tool used to crash.

    `op_from` names the parameter whose value is the operation's durable
    artifact id (triage_apply's plan_id), threaded into the failure
    envelope as operation_id per contract §2.

    `config_gate=False` exempts a tool from the retired-variable
    precheck below. Exactly one tool needs it: `doctor`, whose entire job
    is to REPORT a broken configuration. Gating it would swallow the
    report the error message tells the operator to go and read."""
    def deco(fn):
        sig = inspect.signature(fn)

        def _msg(e: BaseException, typed: bool = False) -> str:
            """Render an exception without ever raising.

            The belt is the last line of defence (contract §7: every tool is
            total), so its own message formatting must not be the thing that
            escapes. An exception whose `__str__` — or whose metaclass
            `__name__` — raises still gets an envelope.
            """
            try:
                text = f"{type(e).__name__}: {e}" if typed else str(e)
            except Exception:
                return "unexpected internal error (see log)"
            # ONE clip for the whole server. This used to be an inline
            # character slice, so the belt — the path every one of the 20
            # tools takes for an uncaught exception — kept returning
            # character-bounded prose after _clip was made byte-bounded,
            # and an astral-plane message came back at 4x the size the
            # validation paths produced.
            return _clip(text)

        def _fail(code: str, error: str, args: tuple, kwargs: dict) -> dict:
            _log.exception("belt[%s]: %s", fn.__name__, code)
            out: dict = {"ok": False, "code": code, "error": error,
                         "fix": "run doctor"}
            if op_from is not None:
                try:
                    value = sig.bind_partial(*args, **kwargs) \
                               .arguments.get(op_from)
                except TypeError:
                    value = None
                # Only echo an id we could actually have minted. The raw
                # argument is the caller's CLAIM, not proof the artifact
                # exists — echoing it unfiltered violated §2 ("never minted
                # *for* a failure") and put the caller's own 60 KB string
                # back on the wire inside the failure envelope.
                if value and ids.is_minted_id(str(value)):
                    out["operation_id"] = str(value)
            return out

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                # A retired per-directory variable is a CONFIGURATION
                # fault, not a filesystem one, so it is checked here rather
                # than left to the resolver — which only validates on the
                # write path. Without this, a read tool resolved the
                # DEFAULT root, found nothing, and answered
                # {"ok": true, "pending": []} while the user's queued mail
                # sat in the directory the retired variable named. An empty
                # success is the worst possible answer to "where is my
                # mail": it is indistinguishable from "you have none".
                retired = (config.retired_state_var_error()
                           if config_gate else None)
                if retired is not None:
                    return _fail(codes.INVALID_INPUT, _clip(retired),
                                 args, kwargs)
                return _bound(fn(*args, **kwargs))
            except UnicodeDecodeError as e:
                # ValueError subclass, but NOT the caller's fault: MCP
                # arguments arrive as valid str — undecodable bytes come
                # from the store or a bug (audit finding F6).
                return _fail(codes.INTERNAL_ERROR,
                             _msg(e, typed=True), args, kwargs)
            except Exception as e:
                # BaseException is deliberately NOT caught: SystemExit,
                # KeyboardInterrupt and GeneratorExit are control flow, not
                # tool failures, and swallowing them would make Ctrl-C and
                # interpreter shutdown unobservable. §7 names the carve-out.
                code, typed = _classify(e)
                return _fail(code, _msg(e, typed=typed), args, kwargs)
        return wrapper
    return deco


# ---------------------------------------------------------------------- #
# Tool implementations (pure functions; wired into MCP below)            #
# ---------------------------------------------------------------------- #


@_belt()
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
    return {"ok": False, "code": codes.INVALID_INPUT,
            "error": f"unknown view {view!r} (want one of {_VIEWS})"}


@_belt()
def tool_get_email(id: str, view: str = "full") -> dict:
    if view not in _VIEWS:
        return _bad_view(view)
    return {"ok": True, "email": _shape_email(_source().get(id), view)}


@_belt()
def tool_get_emails_batch(ids: list[str], view: str = "full") -> dict:
    if view not in _VIEWS:
        return _bad_view(view)
    if len(ids) > _BATCH_MAX_IDS:
        return {"ok": False, "code": codes.INVALID_INPUT,
                "error": f"{len(ids)} ids exceeds the batch cap of "
                         f"{_BATCH_MAX_IDS} — split the request"}
    # Length-validate BEFORE the loop, reporting position rather than
    # echoing the value: a real id is an Envelope Index ROWID or a minted
    # spool id, never hundreds of characters. Per-id errors[] echo each id
    # back for correlation, so 50 ids at the cap × a 60 KB "id" was a 3 MB
    # envelope that the over-cap reject above never sees (50 is AT the cap).
    for pos, id in enumerate(ids):
        # UTF-8 bytes, matching _clip: 256 astral characters are 1 024 bytes,
        # which slipped under a byte-measured clip while 50 of them still
        # made a 300 KB envelope. Every length gate on this path counts the
        # same unit.
        size = len(str(id).encode("utf-8"))
        if size > _MAX_ID_BYTES:
            return {"ok": False, "code": codes.INVALID_INPUT,
                    "error": f"id at position {pos} is {size} bytes — "
                             "not a message id"}
    src = _source()
    emails: list[dict] = []
    errors: list[dict] = []
    for id in ids:
        # Per-id failures are data inside the ok envelope (§2); each entry
        # carries the same code the single-read belt would have used —
        # literally, via the shared class map, so a store fault cannot be
        # invalid_input here and internal_error through get_email (§3).
        try:
            emails.append(_shape_email(src.get(str(id)), view))
        except Exception as e:
            code, typed = _classify(e)
            errors.append({"id": str(id), "code": code,
                           "error": f"{type(e).__name__}: {e}" if typed
                                    else str(e)})
    return {"ok": True, "view": view, "emails": emails, "errors": errors}


@_belt()
def tool_get_thread(thread_id: str) -> dict:
    return {"ok": True,
            "thread": [_to_jsonable(r) for r in _source().thread(thread_id)]}


@_belt()
def tool_list_mailboxes() -> dict:
    return {"ok": True,
            "mailboxes": [_to_jsonable(m) for m in _source().mailboxes()]}


@_belt()
def tool_list_recent(
    mailbox: str | None = None,
    account: str | None = None,
    limit: int = 50,
) -> dict:
    return {"ok": True,
            "messages": [_to_jsonable(r)
                         for r in _source().recent(mailbox, account, limit)]}


@_belt()
def tool_get_attachment(id: str, attachment_id: str) -> dict:
    return {"ok": True,
            "attachment": _to_jsonable(_source().attachment(id, attachment_id))}


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
            "code": codes.OSASCRIPT_UNAVAILABLE,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": f"osascript timed out after {timeout_seconds:g}s.",
            "code": codes.MAIL_UNRESPONSIVE,
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
        # Contract §3.3: the raw osascript number additionally carries its
        # mapped namespace string (v0.11); unmapped numbers carry none.
        mapped = codes.OSA_CODE_MAP.get(code)
        if mapped:
            out["code"] = mapped
    return out


@_belt()
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
        "code": result.get("code"),
    }


# ---------------------------------------------------------------------- #
# send_email / reply_email — the only write path                        #
# ---------------------------------------------------------------------- #


# Contract §3.4 (SEND_CODES_V011): the frozen prose → code table, wired.
# The raise sites' prose IS the table's left column verbatim, so matching
# is exact, not fuzzy. Two tiers because transports prefix their messages
# with the lane ("[<identity>/<driver>] …"): compose/validation prose is
# matched at the START of the message, transport prose anywhere after the
# lane prefix. Order matters within the second tier — the "transport
# unavailable:" preflight wrapper must win over the secret-source prose it
# may embed (a failed preflight is transport_unavailable per §3.4 even
# when the underlying reason is an unreadable credential).
_SEND_PREFIX_CODES: tuple[tuple[str, str], ...] = (
    ("header_injection:", codes.HEADER_INJECTION),
    ("invalid_recipient:", codes.INVALID_RECIPIENT),
    ("Refusing to send as identity", codes.RECIPIENT_NOT_ALLOWED),
    ("attachment not found:", codes.ATTACHMENT_NOT_FOUND),
    ("attachment is a directory:", codes.ATTACHMENT_UNREADABLE),
    ("cannot read attachment", codes.ATTACHMENT_UNREADABLE),
    ("attachments total", codes.ATTACHMENTS_TOO_LARGE),
    ("invalid header content:", codes.INVALID_HEADER),
    ("`to` is required", codes.INVALID_INPUT),
    ("`subject` is required", codes.INVALID_INPUT),
    ("`body` is empty", codes.INVALID_INPUT),
    ("invalid send_at", codes.INVALID_SEND_AT),
    ("send_at is in the past", codes.SEND_AT_IN_PAST),
)
_SEND_LANE_CODES: tuple[tuple[str, str], ...] = (
    ("transport unavailable:", codes.TRANSPORT_UNAVAILABLE),
    ("ssh not found on PATH", codes.TRANSPORT_UNAVAILABLE),
    ("command not found:", codes.TRANSPORT_UNAVAILABLE),
    ("delivery pipe timed out", codes.DELIVERY_FAILED),
    ("delivery failed (exit", codes.DELIVERY_FAILED),
    ("hung for 60s", codes.DELIVERY_FAILED),
    ("SMTP delivery via", codes.DELIVERY_FAILED),
    ("SMTP auth failed", codes.AUTH_FAILED),
    ("unknown transport driver", codes.IDENTITY_MISCONFIGURED),
    ("bad transport params", codes.IDENTITY_MISCONFIGURED),
    ("needs a secret source", codes.IDENTITY_MISCONFIGURED),
    ("`command` is empty.", codes.IDENTITY_MISCONFIGURED),
    # smtp's secret-source errors. Matched by substring, not prefix: they
    # now carry the §3.4 `[identity/smtp]` lane prefix, and older
    # unprefixed prose must keep mapping too. They sit LAST so a preflight
    # wrapper ("… transport unavailable: <credential prose>") still reads
    # as transport_unavailable — that needle is checked first.
    ("`security` CLI not found", codes.CREDENTIALS_UNAVAILABLE),
    ("Keychain read for", codes.CREDENTIALS_UNAVAILABLE),
    ("Keychain item", codes.CREDENTIALS_UNAVAILABLE),
    ("`op` CLI not found", codes.CREDENTIALS_UNAVAILABLE),
    ("1Password read for", codes.CREDENTIALS_UNAVAILABLE),
)


def _send_code(e: SendError) -> str:
    """Map a SendError to its §3.4 code. Exception TYPE decides first
    (IdentityError / GraphError are classes), then the frozen prose table.
    An unmatched message (future prose drift) degrades to the closest
    honest bucket: a lane-prefixed error came from a transport mid-flight
    (delivery_failed); anything else is caller-fixable by construction
    (§7) and reads as invalid_input."""
    from .identities import IdentityError

    if isinstance(e, IdentityError):
        return (codes.UNKNOWN_IDENTITY
                if str(e).startswith("unknown identity")
                else codes.IDENTITY_MISCONFIGURED)
    # GraphError subclasses SendError but lives in a lazily-imported
    # module (launchd-only setups never load it) — if one was raised, the
    # module is in sys.modules by definition. The Exchange lane could not
    # complete the call: retry later, same remedy as a failed preflight.
    graph_mod = sys.modules.get("email_mcp.graph")
    if graph_mod is not None and isinstance(e, graph_mod.GraphError):
        return codes.TRANSPORT_UNAVAILABLE
    msg = str(e)
    for prefix, code in _SEND_PREFIX_CODES:
        if msg.startswith(prefix):
            return code
    for needle, code in _SEND_LANE_CODES:
        if needle in msg:
            return code
    return (codes.DELIVERY_FAILED if msg.startswith("[")
            else codes.INVALID_INPUT)


@_belt()
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
    structured {ok: false, code, error} for caller-fixable failures (the
    code per contract §3.4). Every terminal outcome records ONE `send`
    ledger event (transmission family: failures are ledger-worthy too —
    an attempt to transmit)."""
    try:
        res = send_email(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc,
            attachments=attachments, from_identity=from_identity,
        )
    except SendError as e:
        code = _send_code(e)
        audit.emit("send", outcome="failed", tool="send_email",
                   subject=subject,
                   detail={"error": str(e)[:300], "code": code})
        return {"ok": False, "code": code, "error": str(e)}
    audit.emit("send", outcome="sent", tool="send_email",
               message_id=res.message_id, identity=from_identity,
               to=res.to, cc=res.cc or None, bcc=res.bcc or None,
               subject=res.subject,
               detail={"attachments": res.attachments}
               if res.attachments else None)
    return _to_jsonable(res)


@_belt()
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
    except SendError as e:
        # The reply context (which message, whether reply-all) is lost
        # below the sender return — record it here, at the tool layer.
        code = _send_code(e)
        audit.emit("reply", outcome="failed", tool="reply_email",
                   detail={"orig_id": id, "reply_all": reply_all,
                           "error": str(e)[:300], "code": code})
        return {"ok": False, "code": code, "error": str(e)}
    audit.emit("reply", outcome="sent", tool="reply_email",
               message_id=res.message_id, identity=from_identity,
               to=res.to, cc=res.cc or None, bcc=res.bcc or None,
               subject=res.subject,
               detail={"orig_id": id, "reply_all": reply_all})
    return _to_jsonable(res)


@_belt()
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
    except SendError as e:
        code = _send_code(e)
        audit.emit("schedule", outcome="failed", tool="schedule_email",
                   subject=subject,
                   detail={"error": str(e)[:300], "code": code})
        return {"ok": False, "code": code, "error": str(e)}
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
    out = {"ok": True, **_to_jsonable(entry)}
    # Inside the belt (leak closure): the plist probe must not be the one
    # unbelted line on the schedule path.
    from .dispatcher import _plist_path
    if not _plist_path().exists():
        out["warning"] = (
            "dispatcher launchd agent NOT installed — nothing will "
            "send. Run: python -m email_mcp.dispatcher --install-launchd"
        )
    return out


@_belt()
def tool_list_scheduled(state: str | None = None, limit: int = 50) -> dict:
    from . import spool
    from .dispatcher import LAUNCHD_LABEL, _plist_path

    states = [state] if state else list(spool.STATES)
    if state and state not in spool.STATES:
        return {"ok": False, "code": codes.INVALID_INPUT,
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


@_belt()
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


@_belt()
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


@_belt(op_from="plan_id")
def tool_triage_apply(plan_id: str) -> dict:
    try:
        return apply_plan(_source(), plan_id)
    except TriageError as e:
        return _triage_err(e)


@_belt()
def tool_mailbox_create(account: str, path: str) -> dict:
    try:
        out = create_mailbox(_source(), account, path)
    except TriageError as e:
        return _triage_err(e)
    # Store family: emit only on actual change — the idempotent
    # already-there path (existed=true) leaves no ledger event.
    if out.get("existed") is False:
        audit.emit("mailbox_create", outcome="created",
                   tool="mailbox_create", account=account, mailbox=path)
    return out


@_belt()
def tool_mailbox_delete(account: str, path: str) -> dict:
    try:
        out = delete_mailbox(_source(), account, path)
    except TriageError as e:
        return _triage_err(e)
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


@_belt()
def tool_cancel_scheduled(id: str) -> dict:
    """Failure envelopes carry a §3 code (v0.11): unknown id → not_found;
    state conflicts (not pending / claimed mid-call / already sent) →
    invalid_input with the state in the prose; identity and Exchange
    problems map through _send_code. Every failure after the entry was
    found also carries operation_id = the spool id (§2: the durable
    artifact already existed), threading it to the ledger's `op`."""
    from . import spool

    def _done(out: dict, outcome: str, *, reason: str | None = None,
              subject: str | None = None, **extra) -> dict:
        """The ONE cancel emit: every terminal exit records exactly one
        `cancel` event; op = the spool id (the artifact-id rule threads
        it to the entry's schedule/deliver events)."""
        detail = {"reason": reason, **extra} if reason else None
        audit.emit("cancel", outcome=outcome, operation_id=id,
                   tool="cancel_scheduled", spool_id=id, subject=subject,
                   detail=detail)
        return out

    found = spool.find(id)
    if found is None:
        return _done(
            {"ok": False, "code": codes.NOT_FOUND,
             "error": f"no scheduled message with id {id!r}"},
            "failed", reason="not_found")
    state, entry = found
    if state != "pending":
        return _done(
            {"ok": False, "code": codes.INVALID_INPUT, "operation_id": id,
             "error": f"cannot cancel {id}: status is {state!r} "
                      "(only pending messages can be cancelled)"},
            "failed", reason="not_pending", subject=entry.subject,
            state=state)

    if entry.executor == "graph":
        # Exchange holds an armed deferred draft — revoke it FIRST; the
        # local manifest only moves to cancelled/ once Exchange's claim is
        # confirmed gone. On any ambiguity the entry stays pending.
        from . import graph, identities

        try:
            ident = identities.get(entry.identity)
        except SendError as e:
            return _done(
                {"ok": False, "code": _send_code(e), "operation_id": id,
                 "error": f"cannot cancel {id}: {e}"},
                "failed", reason="identity_unavailable",
                subject=entry.subject)
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
            return _done(
                {"ok": False, "code": _send_code(e), "operation_id": id,
                 "error": f"cannot cancel {id}: Exchange still holds the "
                          f"deferred draft and the revoke failed ({e}). "
                          "Retry, or discard the draft in Outlook/OWA "
                          "yourself, then cancel again."},
                "failed", reason="revoke_failed", subject=entry.subject)
        if outcome == "gone":
            # F10 race: the draft vanished on its own — did Exchange
            # already send it? Only Sent Items can say.
            try:
                sent = graph.sent_by_message_id(ident, entry.message_id)
            except SendError as e:
                return _done(
                    {"ok": False, "code": _send_code(e),
                     "operation_id": id,
                     "error": f"cannot cancel {id}: the deferred draft is "
                              f"gone but Sent Items could not be checked "
                              f"({e}) — outcome ambiguous, retry."},
                    "failed", reason="sent_check_failed",
                    subject=entry.subject)
            if sent:
                # Atomic ownership hand-off (same rename fence as the
                # cancel below) — a concurrently reconciling dispatcher
                # must not race this terminal move.
                if not spool.claim(id, "pending", "sent"):
                    return _done(
                        {"ok": False, "code": codes.INVALID_INPUT,
                         "operation_id": id,
                         "error": f"cannot cancel {id}: a dispatcher "
                                  "just moved it — re-check "
                                  "list_scheduled"},
                        "failed", reason="claim_lost",
                        subject=entry.subject)
                entry.delivered_at = spool.iso(spool.utcnow())
                entry.next_attempt_at = None
                entry.last_error = None
                entry.status = "sent"
                spool.update("sent", entry)
                # Terminal state change, ok:false on the wire — still a
                # ledger-worthy outcome of its own.
                return _done(
                    {"ok": False, "code": codes.INVALID_INPUT,
                     "operation_id": id, "id": id, "status": "sent",
                     "error": f"cannot cancel {id}: Exchange already sent "
                              "it (found in Sent Items) — the entry has "
                              "been moved to sent/."},
                    "too_late_sent", subject=entry.subject)
            # Confirmed absent from Drafts AND Sent Items: nothing is
            # armed (someone may have discarded it in OWA) — proceed as
            # revoked and cancel the local entry below.

    if not spool.claim(id, "pending", "cancelled"):
        return _done(
            {"ok": False, "code": codes.INVALID_INPUT, "operation_id": id,
             "error": f"cannot cancel {id}: a dispatcher just claimed it"},
            "failed", reason="claim_lost", subject=entry.subject)
    entry.status = "cancelled"
    spool.update("cancelled", entry)
    return _done(
        {"ok": True, "id": id, "status": "cancelled",
         "subject": entry.subject, "was_due": entry.send_at},
        "cancelled", subject=entry.subject)


@_belt(config_gate=False)   # doctor must REPORT a broken configuration
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


@_belt()
def tool_audit(
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    event: str | None = None,
    plan_id: str | None = None,
    operation_id: str | None = None,
    limit: int = 50,
) -> dict:
    for name, value in (("since", since), ("until", until)):
        if value is not None and not _valid_iso_bound(str(value)):
            return {"ok": False, "code": codes.INVALID_INPUT,
                    "error": f"invalid ISO datetime for `{name}`: {value!r} "
                             "(want ISO-8601; prefixes allowed, e.g. "
                             "2026-07 or 2026-07-29)"}
    out = audit.query(since=since, until=until, tool=tool, event=event,
                      plan_id=plan_id, operation_id=operation_id,
                      limit=limit)
    return {"ok": True, **out}


# ---------------------------------------------------------------------- #
# MCP wiring                                                             #
# ---------------------------------------------------------------------- #


def _stamp_server_version(mcp) -> None:
    """Advertise OUR version in the initialize handshake's serverInfo.

    FastMCP has no `version=` parameter (checked across mcp 1.2–1.29): it
    builds the low-level Server without one, and that Server's
    create_initialization_options() then falls back to
    importlib.metadata.version("mcp") — so every client is told the MCP
    *library's* version, which silently changes with whichever mcp the user
    resolved. The low-level object is the only place the value lives.
    """
    low = getattr(mcp, "_mcp_server", None)
    if low is not None and hasattr(low, "version"):
        low.version = __version__


def _build_mcp_server():
    """Build the FastMCP Server: twenty tools, or exactly the eleven
    read-side tools when EMAIL_MCP_READ_ONLY=1 — the mutating nine are
    lexically gated below, so in a read-only session they never exist."""
    from mcp.server.fastmcp import FastMCP  # type: ignore

    from .config import read_only

    mcp = FastMCP("apple-mail")
    _stamp_server_version(mcp)
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
        read live from the mail store, never from the search index.
        Returns {ok, email} — the shaped message rides under `email`."""
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
        Returns {ok, thread} — the refs ride under `thread`."""
        return tool_get_thread(thread_id)

    @mcp.tool()
    def list_mailboxes() -> dict:
        """List all known mailboxes across all accounts, with message counts.
        Returns {ok, mailboxes}."""
        return tool_list_mailboxes()

    @mcp.tool()
    def list_recent(
        mailbox: str | None = None,
        account: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List the newest messages, optionally scoped to a mailbox/account.
        Returns {ok, messages}."""
        return tool_list_recent(mailbox=mailbox, account=account, limit=limit)

    @mcp.tool()
    def get_attachment(id: str, attachment_id: str) -> dict:
        """Materialise an attachment to a tmp file and return its path. The
        caller (Claude) can then `Read` the file. Bytes are never inlined.
        Returns {ok, attachment} — path and metadata under `attachment`."""
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
        and the audit ledger (the `audit` member of `checks` since v0.11;
        a deprecated top-level `audit` mirror remains for v0.10 readers).
        Returns {ok, read_only, checks} where each check is {ok, detail}
        plus a concrete `fix` (a command or a Settings pane) when
        something is off. Read-only and side-effect free — call it first
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
            code: "attachments_too_large", error} before anything is sent.

            `from_identity` selects the sending identity from
            ~/.email-mcp/identities.toml (omit for the default). Each identity
            carries its own From: address, transport (ssh_sendmail / smtp /
            pipe) and allowlist, so which lane the mail leaves on follows from
            the name alone.

            Safety: while the allowlist guard is active (default), recipients are
            restricted to the sending identity's own address — a returned
            {ok: false, code: "recipient_not_allowed", error} means the guard
            fired, not a delivery failure. A Bcc-to-self is added automatically
            for a Sent record. Returns {ok, message_id, to, cc, bcc, subject,
            attachments} on success; failures carry a stable `code` (contract
            §3.4) beside the prose.
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
            return tool_schedule_email(
                to=to, subject=subject, body=body, send_at=send_at,
                cc=cc, bcc=bcc, attachments=attachments,
                from_identity=from_identity,
            )

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
    # Startup gate: a retired per-directory variable is refused before the
    # stdio server binds, so a misconfigured install fails at launch with
    # one legible line instead of serving tools that quietly answer "you
    # have no mail". --doctor is exempt: it exists to report the fault.
    retired = config.retired_state_var_error()
    if retired is not None and not args.doctor:
        print(f"email-mcp: {retired}", file=sys.stderr)
        return 2
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
