"""Append-only audit ledger: ONE JSONL event per mutation.

The ledger indexes the truths already frozen elsewhere (spool manifests,
plan files, Message-IDs) — it does not create truth. Schema v1 is specified
in docs/v1-contract.md §6. Storage: monthly JSONL files under the state
tree's audit leaf — ~/.email-mcp/audit/YYYY-MM.jsonl, dir 0700, files 0600.

Two writer processes exist (server + launchd dispatcher), so an append is
one os.write on an O_APPEND fd opened per event — lines never interleave —
and each emit resolves the monthly path from its own timestamp, which
dissolves the month-rollover race.

Emit-failure policy: log-and-continue, absolute. emit() NEVER raises; an
unwritable ledger must never block mail. Events never contain message
bodies; subject is capped at 200 chars; an oversize event sheds fields in
a fixed order (detail's failures/pending lists collapse to counts, then
detail drops wholesale) so the envelope and summary always survive within
MAX_EVENT_BYTES.

CLI:  python -m email_mcp.audit --tail 20
      python -m email_mcp.audit --since 2026-07 --event deliver
      python -m email_mcp.audit --status
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime

from . import config, ids, state
from .log import get_logger

_log = get_logger()

SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 16384
SUBJECT_MAX_CHARS = 200

_ENVELOPE = ("v", "ts", "op", "src", "event", "outcome")
# Schema v1's closed optional-field vocabulary (contract §6); anything
# else a hook wants to record belongs inside `detail` — which is FENCED:
# §6's no-bodies/no-secrets guarantee is enforced here, not left to
# call-site discipline (audit finding F2). Keys on the denylist are
# dropped recursively and their names recorded under `_fenced`.
_OPTIONAL = (
    "identity", "account", "mailbox", "message_id", "spool_id", "plan_id",
    "draft_id", "to", "cc", "bcc", "subject", "summary", "detail",
)

_DETAIL_FENCE = frozenset({
    "body", "body_text", "body_html", "content", "html", "raw",
    "password", "secret", "token", "access_token", "refresh_token",
    "authorization",
})


def _fence_detail(value, fenced: list):
    """Recursively strip denylisted keys from a detail payload. The ledger
    is a permanent record: message bodies and credential material must be
    structurally unreachable, not merely avoided by callers."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in _DETAIL_FENCE:
                fenced.append(str(k))
                continue
            out[k] = _fence_detail(v, fenced)
        return out
    if isinstance(value, (list, tuple)):
        return [_fence_detail(v, fenced) for v in value]
    return value
_MONTH_FILE = re.compile(r"\d{4}-\d{2}\.jsonl")

_PROCESS = "server"


def set_process(name: str) -> None:
    """Tag every subsequent emit's `src` ("server", "dispatcher", "cli")."""
    global _PROCESS
    _PROCESS = name


# ---------------------------------------------------------------------- #
# Writing                                                                 #
# ---------------------------------------------------------------------- #


def emit(
    event: str,
    *,
    outcome: str,
    operation_id: str | None = None,
    tool: str | None = None,
    detail: dict | None = None,
    **fields,
) -> str | None:
    """Append one event; return its op, or None when it could not be
    recorded. NEVER raises — an unwritable ledger never blocks mail.

    `operation_id` should be the durable artifact's own id where one
    exists (spool id, plan id — threading server and dispatcher events
    for free); absent, a fresh id is minted.
    """
    try:
        ts = ids.iso(ids.utcnow())
        op = operation_id or ids.new_id()
        record: dict = {
            "v": SCHEMA_VERSION,
            "ts": ts,
            "op": op,
            "src": _PROCESS,
            "event": event,
            "outcome": outcome,
        }
        if tool is not None:
            record["tool"] = tool
        if detail is not None:
            fenced: list = []
            detail = _fence_detail(detail, fenced)
            if fenced and isinstance(detail, dict):
                detail["_fenced"] = sorted(set(fenced))
            fields["detail"] = detail
        for key in _OPTIONAL:
            value = fields.get(key)
            if value is None:
                continue
            if key == "subject" and isinstance(value, str):
                value = value[:SUBJECT_MAX_CHARS]
            record[key] = value
        line = _fit(record)
        # Month resolved from this event's own ts: no rollover race. The
        # ledger dir comes from state adoption — a refused root or broken
        # leaf raises here and is dropped by the fence below (§6:
        # log-and-continue, absolute).
        path = state.State.resolve().adopt().audit / f"{ts[:7]}.jsonl"
        # O_RDWR (not O_WRONLY) so the torn-tail probe below can pread.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # Torn-tail heal: a previous crash/short write can leave the
            # file without a trailing newline; appending straight after it
            # would weld THIS event onto the torn fragment and lose it too.
            # A leading newline isolates the fragment on its own (skipped,
            # counted) line. Still ONE os.write per event — lines from two
            # processes can never interleave. If both writers heal the same
            # torn tail, the extra blank line is skipped silently.
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            os.write(fd, line + b"\n")
        finally:
            os.close(fd)
        os.chmod(path, 0o600)  # launchd agents run with a permissive umask
        return op
    except Exception:
        _log.warning("audit: emit(%s) failed; event dropped", event,
                     exc_info=True)
        return None


def _dumps(record: dict) -> bytes:
    # default=str: a hook passing a Path / datetime / bytes inside detail
    # must cost a coerced value, never the whole event (emit would drop it
    # wholesale on TypeError otherwise — a silently lost receipt).
    return json.dumps(
        record, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _fit(record: dict) -> bytes:
    """Serialize compactly, shedding fields in the contract's order until
    the line fits MAX_EVENT_BYTES: detail's failures/pending lists collapse
    to counts first, then detail drops wholesale; the envelope and summary
    always survive — a truncated event still says what happened, to how
    many, with which outcome."""
    line = _dumps(record)
    if len(line) <= MAX_EVENT_BYTES:
        return line
    detail = record.get("detail")
    if isinstance(detail, dict):
        collapsed = {
            k: len(v)
            if k in ("failures", "pending") and isinstance(v, list) else v
            for k, v in detail.items()
        }
        if collapsed != detail:
            collapsed["truncated"] = True
            record["detail"] = collapsed
            line = _dumps(record)
            if len(line) <= MAX_EVENT_BYTES:
                return line
    if "detail" in record:
        record["detail"] = {"truncated": True}
        line = _dumps(record)
        if len(line) <= MAX_EVENT_BYTES:
            return line
    # Last resort (a pathological non-detail field): envelope + summary.
    record = {
        k: v for k, v in record.items()
        if k in _ENVELOPE or k in ("tool", "summary")
    }
    record["detail"] = {"truncated": True}
    line = _dumps(record)
    while len(line) > MAX_EVENT_BYTES and record.get("summary"):
        record["summary"] = record["summary"][: len(record["summary"]) // 2]
        line = _dumps(record)
    return line


# ---------------------------------------------------------------------- #
# Reading                                                                 #
# ---------------------------------------------------------------------- #


def _bound(value) -> str | None:
    """Normalize a since/until bound to an ISO string (prefixes allowed)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ids.iso(value)
    return str(value)


def query(
    since=None,
    until=None,
    tool: str | None = None,
    event: str | None = None,
    plan_id: str | None = None,
    operation_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Scan the ledger; return {"events" (newest first), "files_scanned",
    "skipped_lines"}.

    since/until are ISO-8601 strings, prefixes allowed and inclusive
    ("2026-07" means the whole month); they also prune monthly files by
    name before any line is read. Torn or corrupt lines are counted and
    skipped, never fatal. limit is clamped to 1..500. An absent ledger
    directory is simply empty — read paths never create it.
    """
    since, until = _bound(since), _bound(until)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    root = config.audit_dir()  # a path question: read paths never create
    if not root.is_dir():
        return {"events": [], "files_scanned": 0, "skipped_lines": 0}

    events: list[dict] = []
    files_scanned = 0
    skipped = 0
    for path in sorted(root.iterdir()):
        if not _MONTH_FILE.fullmatch(path.name):
            continue
        month = path.name[:7]
        if since and month < since[:7]:
            continue
        if until and month > until[:7]:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_scanned += 1
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            ts = str(rec.get("ts", ""))
            if since and ts < since:
                continue
            if until and ts[: len(until)] > until:
                continue
            if tool is not None and rec.get("tool") != tool:
                continue
            if event is not None and rec.get("event") != event:
                continue
            if plan_id is not None and rec.get("plan_id") != plan_id:
                continue
            if operation_id is not None and rec.get("op") != operation_id:
                continue
            events.append(rec)
    # Newest first, tolerating non-monotonic timestamps (clock skew, two
    # writers): reverse scan order first so the stable sort breaks ts ties
    # in favor of the later-appended line.
    events.reverse()
    events.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return {
        "events": events[:limit],
        "files_scanned": files_scanned,
        "skipped_lines": skipped,
    }


def tail(n: int = 20) -> list[dict]:
    """The last n events, oldest → newest (tail -n semantics)."""
    return list(reversed(query(limit=n)["events"]))


# ---------------------------------------------------------------------- #
# CLI — the audit CLI is a reader and may print (stdout is not the wire)  #
# ---------------------------------------------------------------------- #


def _print_status() -> None:
    root = config.audit_dir()
    if not root.is_dir():
        print(f"dir: {root} (absent — no events recorded yet)")
        return
    months = sorted(
        p.name for p in root.iterdir() if _MONTH_FILE.fullmatch(p.name)
    )
    print(f"dir: {root}")
    span = f" ({months[0][:7]} … {months[-1][:7]})" if months else ""
    print(f"files: {len(months)}{span}")
    last = query(limit=1)["events"]
    if last:
        print(f"last event: {last[0].get('ts', '-')} "
              f"{last[0].get('event', '?')}/{last[0].get('outcome', '?')}")
    else:
        print("last event: -")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="email_mcp.audit",
        description="Read the append-only audit ledger (JSONL to stdout).",
    )
    parser.add_argument("--tail", type=int, default=None, metavar="N",
                        help="print the last N events, oldest first")
    parser.add_argument("--since", default=None, metavar="ISO",
                        help="lower bound; ISO-8601 prefixes OK (2026-07)")
    parser.add_argument("--until", default=None, metavar="ISO",
                        help="upper bound, inclusive prefix (2026-07-29)")
    parser.add_argument("--tool", default=None, help="filter by tool name")
    parser.add_argument("--event", default=None, help="filter by event name")
    parser.add_argument("--plan-id", default=None, help="filter by plan id")
    parser.add_argument("--op", default=None, help="filter by operation id")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="max events (1..500, default 50)")
    parser.add_argument("--status", action="store_true",
                        help="ledger directory, file count, last event")
    args = parser.parse_args(argv)

    set_process("cli")
    if args.status:
        _print_status()
        return 0
    if args.tail is not None:
        for rec in tail(args.tail):
            print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
        return 0
    filters = (args.since, args.until, args.tool, args.event,
               args.plan_id, args.op, args.limit)
    if all(f is None for f in filters):
        parser.print_help()
        return 2
    out = query(
        since=args.since, until=args.until, tool=args.tool,
        event=args.event, plan_id=args.plan_id, operation_id=args.op,
        limit=args.limit if args.limit is not None else 50,
    )
    for rec in out["events"]:  # newest first, like the audit tool
        print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
