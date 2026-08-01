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
  flood the wire, because there is no other `ok: false` construction site
  to bound.
- **The minted-id gate on `operation_id`** (contract §2): a failure carries
  `operation_id` iff a durable artifact id had already been minted for the
  operation — typed errors declare theirs at raise time; belt failures
  thread the id named by `op_from` (triage_apply's plan_id, per §1 row 18).
  It is never minted *for* a failure.
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

from . import codes
from .log import get_logger

_log = get_logger()

# Failure prose is byte-bounded: ~2000 UTF-8 bytes, clipped at a valid
# character boundary with a visible ellipsis.
MAX_ERROR_BYTES = 2000


# --------------------------------------------------------------------- #
# The closed typed-error hierarchy (contract §3)                        #
# --------------------------------------------------------------------- #


class ToolError(Exception):
    """Base of the closed typed-error hierarchy. Every instance carries
    exactly one `code` from the §3 namespace (codes.py): the belt-code
    subclasses below fix theirs as a class attribute; SendError carries
    the §3.4 assignment of its raise site; TriageError carries its §3.1
    code. Prose is caller-fixable and safe to surface verbatim.

    `fix` — optional concrete remedy (a command or Settings pane).
    `operation_id` — set ONLY when a durable artifact id (plan id, spool
    id) existed before the failure; the boundary passes it through.
    `data` — the few extra wire keys a failure keeps for v0.10
    compatibility (cancel_scheduled's too-late outcome reports the
    entry's terminal state)."""

    code: str = codes.INTERNAL_ERROR

    def __init__(
        self,
        error: str,
        *,
        code: str | None = None,
        fix: str | None = None,
        operation_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        super().__init__(error)
        if code is not None:
            self.code = code
        self.fix = fix
        self.operation_id = operation_id
        self.data = data


class NotFound(ToolError):
    """A referenced object does not exist (unknown id, vanished file)."""
    code = codes.NOT_FOUND


class InvalidInput(ToolError):
    """A caller-supplied value could not be parsed or used."""
    code = codes.INVALID_INPUT


class MailUnavailable(ToolError):
    """The Mail store is not readable (not configured, or no FDA)."""
    code = codes.MAIL_UNAVAILABLE


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


# --------------------------------------------------------------------- #
# The boundary decorator                                                #
# --------------------------------------------------------------------- #


def tool(fn=None, *, op_from: str | None = None):
    """Wrap a typed tool function into the one wire envelope.

    Success values pass through `to_jsonable` and gain `ok: true`; a value
    carrying its own `ok` keeps it (doctor and refresh_mail report health,
    not tool failure — the §2 documented exceptions). `op_from` names the
    parameter whose value is a durable artifact id minted by an EARLIER
    operation; belt failures thread it as `operation_id`.
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
            return str(value) if value else None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                value = fn(*args, **kwargs)
            except ToolError as e:
                out = {"ok": False, "code": e.code, "error": _bound(str(e))}
                if e.fix:
                    out["fix"] = e.fix
                if e.operation_id:
                    out["operation_id"] = e.operation_id
                if e.data:
                    out.update(to_jsonable(e.data))
                return out
            except Exception as e:
                code = classify(e)
                _log.exception("belt[%s]: %s", fn.__name__, code)
                prose = (f"{type(e).__name__}: {e}"
                         if code == codes.INTERNAL_ERROR else str(e))
                out = {"ok": False, "code": code, "error": _bound(prose),
                       "fix": "run doctor"}
                minted = _minted_id(args, kwargs)
                if minted:
                    out["operation_id"] = minted
                return out
            return {"ok": True, **to_jsonable(value)}

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
