"""The wire boundary as one total function (concept primitive 2).

Tools are pure functions returning typed values or raising typed errors;
this module is the ONLY place those become MCP wire dicts
(docs/v1-contract.md §2, §3, §7):

    typed value       -> {ok: true, ...data}
    typed ToolError   -> {ok: false, code, error, fix?, operation_id?}
    anything else     -> classified by _CLASSIFY (the belt), fix: "run
                         doctor", FULL traceback to the file log only

Three promises live here and nowhere else:

- **Byte-bounding of failure prose** (`MAX_ERROR_BYTES`): no failure can
  flood the wire. Failure prose crosses the boundary two ways — raised
  (typed errors, belt catches) and carried as a VALUE (refresh_mail's
  nudge report, mailbox_delete's verdict dicts, the §2 partial-failure
  channels `errors[]`/`failures[]` inside ok:true) — so the one exit
  bounds both: every string of an ok:false envelope, every string of a
  partial-failure record.
- **The minted-id gate on `operation_id`** (contract §2): a failure carries
  `operation_id` iff a durable artifact id had already been minted for the
  operation — typed errors declare theirs at raise time; belt failures
  thread the id named by `op_from` (triage_apply's plan_id, per §1 row 18)
  only when it is in the minted vocabulary (`ids.is_minted_id`) — the
  argument alone is the caller's claim, not proof of a mint. An id is
  never minted *for* a failure.
- **The exception→code classification** (`classify`): one table, ordered
  most-specific-first because the interesting rows are subclasses of the
  broad ones (UnicodeDecodeError IS-A ValueError, KeyError/IndexError ARE
  LookupErrors, sqlite3.ProgrammingError IS-A DatabaseError).

The outputSchema freeze derives from the same types the tools return:
`schema_of` walks a return annotation into the wire shape it produces
(contract §8 — frozen by tests/snapshots/output_schemas.json).
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
import sqlite3
import types
import typing
from datetime import datetime

from . import codes, ids, state
from .domain.errors import InvalidInput, MailUnavailable, NotFound, ToolError
from .log import get_logger

_log = get_logger()

# Failure prose is byte-bounded: ~2000 UTF-8 bytes, clipped at a valid
# character boundary with a visible ellipsis.
MAX_ERROR_BYTES = 2000


# --------------------------------------------------------------------- #
# Exception classification — the one map (contract §3.5)                #
# --------------------------------------------------------------------- #

# Ordered most-specific-first; the first isinstance match wins.
_CLASSIFY: tuple[tuple[type[BaseException], str], ...] = (
    (UnicodeDecodeError, codes.INTERNAL_ERROR),   # IS-A ValueError
    (ValueError, codes.INVALID_INPUT),
    (KeyError, codes.INTERNAL_ERROR),             # IS-A LookupError
    (IndexError, codes.INTERNAL_ERROR),           # IS-A LookupError
    (LookupError, codes.NOT_FOUND),
    (sqlite3.ProgrammingError, codes.INTERNAL_ERROR),  # IS-A DatabaseError
    (state.RefusedError, codes.INVALID_INPUT),  # IS-A OSError: a deliberate
    # policy refusal (retired var, symlink, foreign dir) — the reason is
    # caller-fixable environment, not the belt of last resort
    (FileNotFoundError, codes.MAIL_UNAVAILABLE),
    (PermissionError, codes.MAIL_UNAVAILABLE),
    (sqlite3.DatabaseError, codes.MAIL_UNAVAILABLE),
)


def classify(exc: BaseException) -> str:
    """§3 code for an untyped exception; `internal_error` is the floor."""
    for etype, code in _CLASSIFY:
        if isinstance(exc, etype):
            return code
    return codes.INTERNAL_ERROR


# --------------------------------------------------------------------- #
# Wire serialization                                                    #
# --------------------------------------------------------------------- #


def to_jsonable(obj: typing.Any) -> typing.Any:
    """Recursively convert dataclasses + datetimes to JSON-friendly types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v)
                for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def _bound(prose: str) -> str:
    raw = prose.encode("utf-8")
    if len(raw) <= MAX_ERROR_BYTES:
        return prose
    return raw[:MAX_ERROR_BYTES].decode("utf-8", "ignore") + "…"


def _prose(e: BaseException) -> str:
    """str(e), total: a __str__ that raises must not become the very
    escape the boundary exists to prevent (contract §7)."""
    try:
        return str(e)
    except Exception:
        return f"<unprintable {type(e).__name__}>"


def _clip(record: typing.Any) -> typing.Any:
    """Byte-bound every string inside one failure record."""
    if isinstance(record, str):
        return _bound(record)
    if isinstance(record, list):
        return [_clip(x) for x in record]
    if isinstance(record, dict):
        return {k: _clip(v) for k, v in record.items()}
    return record


# The §2 partial-failure channels: failure records riding inside ok:true.
_FAILURE_CHANNELS = ("errors", "failures")


def _bounded(out: dict) -> dict:
    """Bound failure prose wherever it crosses the boundary: the whole
    envelope when ok is false (typed, belt, or carried as a value —
    refresh_mail's nudge report, mailbox_delete's verdict dicts), and
    each partial-failure record when ok is true. Success payloads
    (bodies, attachments) are never touched."""
    if out.get("ok") is False:
        return _clip(out)
    return {k: _clip(v) if k in _FAILURE_CHANNELS else v
            for k, v in out.items()}


# --------------------------------------------------------------------- #
# The boundary decorator                                                #
# --------------------------------------------------------------------- #


def tool(fn=None, *, op_from: str | None = None):
    """Wrap a typed tool function into the one wire envelope.

    Success values pass through `to_jsonable` and gain `ok: true`; a value
    carrying its own `ok` keeps it (doctor and refresh_mail report health,
    not tool failure — the §2 documented exceptions). `op_from` names the
    parameter that RECEIVES a durable artifact id minted by an EARLIER
    operation; belt failures thread it as `operation_id` iff the value is
    in the minted vocabulary — a caller's argument proves nothing by
    itself. Every path exits through `_bounded`.
    """

    def deco(fn):
        sig = inspect.signature(fn) if op_from is not None else None

        def _minted_id(args: tuple, kwargs: dict) -> str | None:
            if sig is None:
                return None
            try:
                value = sig.bind_partial(*args, **kwargs).arguments.get(op_from)
            except TypeError:
                value = None
            return value if ids.is_minted_id(value) else None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                value = fn(*args, **kwargs)
            except ToolError as e:
                out = {"ok": False, "code": e.code, "error": _prose(e)}
                if e.fix:
                    out["fix"] = e.fix
                if e.operation_id:
                    out["operation_id"] = e.operation_id
                if e.data:
                    out.update(to_jsonable(e.data))
            except Exception as e:
                code = classify(e)
                _log.exception("belt[%s]: %s", fn.__name__, code)
                msg = _prose(e)
                out = {"ok": False, "code": code,
                       "error": (f"{type(e).__name__}: {msg}"
                                 if code == codes.INTERNAL_ERROR else msg),
                       "fix": "run doctor"}
                minted = _minted_id(args, kwargs)
                if minted:
                    out["operation_id"] = minted
            else:
                out = {"ok": True, **to_jsonable(value)}
            return _bounded(out)

        return wrapper

    return deco if fn is None else deco(fn)


# --------------------------------------------------------------------- #
# Schema derivation — the outputSchema freeze walks the types           #
# --------------------------------------------------------------------- #

_SCALARS: dict[type, str] = {
    str: "str", bool: "bool", int: "int", float: "float",
    datetime: "str",              # isoformat at the boundary
    type(None): "null",
}


def schema_of(tp: typing.Any) -> typing.Any:
    """Wire schema of a type annotation, in the frozen snapshot dialect:
    dataclasses → {field: schema}, list[X] → [schema], scalars → type-name
    strings, X|Y scalars → "x|y" (null last), dataclass unions →
    {"one_of": [variants]} in declaration order, dict → "dict" (dynamic
    keys — the type cannot see inside). Anything else fails loudly so an
    unfreezable return type can never ship silently."""
    if tp in _SCALARS:
        return _SCALARS[tp]
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        members = [schema_of(m) for m in typing.get_args(tp)]
        if all(isinstance(m, str) for m in members):
            named = sorted(m for m in members if m != "null")
            return "|".join(named + (["null"] if "null" in members else []))
        return {"one_of": members}
    if origin is list:
        return [schema_of(typing.get_args(tp)[0])]
    if tp is dict or origin is dict:
        return "dict"
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        return {f.name: schema_of(hints[f.name])
                for f in dataclasses.fields(tp)}
    raise ValueError(f"cannot derive a wire schema for {tp!r}")
