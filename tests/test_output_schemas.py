"""outputSchema freeze at v0.11 (docs/v1-contract.md §8) — two artifacts:

1. **The derived freeze** (snapshots/output_schemas.json): each tool's
   success shape is derived FROM ITS RETURN TYPE (envelope.schema_of), so
   every variant a tool can return is frozen by construction. Rows that
   read "dict" are dynamic passthroughs (doctor's checks, triage_apply's
   result, fts health) whose shape no type can see into.

2. **The oracle proof** (snapshots/oracle_output_schemas.json): the
   shipped v0.11 build's fixture-observed wire snapshot, copied verbatim —
   the implementation-independent acceptance surface. The comparator below
   proves the derived shapes agree with the oracle wherever both are
   mechanically comparable; every incomparable node is named in
   EXPECTED_SKIPS and asserted, never silently passed.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import typing
from pathlib import Path

import pytest

from email_mcp import envelope, server

SNAPSHOT = Path(__file__).parent / "snapshots" / "output_schemas.json"
ORACLE = Path(__file__).parent / "snapshots" / "oracle_output_schemas.json"


def derived_schemas() -> dict:
    """{tool name: success shape} for the 20-tool surface, derived from
    the return annotations of server.tool_* (the boundary adds ok)."""
    shapes = {}
    for name in dir(server):
        if not name.startswith("tool_"):
            continue
        fn = inspect.unwrap(getattr(server, name))
        shape = envelope.schema_of(typing.get_type_hints(fn)["return"])
        if isinstance(shape, dict):
            shape = {"ok": "bool", **shape}
        shapes[name[len("tool_"):]] = shape
    return shapes


def _render(schemas: dict) -> str:
    """Canonical byte form: sorted keys, 2-space indent, trailing newline —
    regenerating an unchanged surface is a byte-identical file."""
    return json.dumps(schemas, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------- #
# The derived freeze                                                    #
# --------------------------------------------------------------------- #


def test_output_schemas_match_snapshot():
    current = derived_schemas()
    assert len(current) == 21
    if not SNAPSHOT.exists():
        # The freeze must be self-defending: a deleted snapshot fails
        # loudly instead of silently re-freezing whatever the types now
        # derive. Restore it from git, or regenerate DELIBERATELY:
        #   python -c "from tests.test_output_schemas import *; \
        #              SNAPSHOT.write_text(_render(derived_schemas()))"
        pytest.fail(
            "tests/snapshots/output_schemas.json is missing — the "
            "outputSchema freeze (contract §8) cannot be verified. Restore "
            "it via `git checkout tests/snapshots/` or regenerate "
            "deliberately."
        )
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == frozen, (
        "outputSchema drift against tests/snapshots/output_schemas.json — "
        "tool outputs are FROZEN at v0.11 (docs/v1-contract.md §8). If the "
        "change is genuinely intended, regenerate the snapshot and say so "
        "explicitly in the change description."
    )


def test_snapshot_regeneration_is_byte_stable():
    first = _render(derived_schemas())
    second = _render(derived_schemas())
    assert first == second  # two derivations, identical bytes
    assert first == SNAPSHOT.read_text(encoding="utf-8")


def test_no_tool_declares_a_structured_output_schema(monkeypatch):
    """The oracle froze `declared: null` for all 20 tools — clients get
    the envelope as unstructured content, exactly like the shipped build."""
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    mcp = server._build_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 21
    assert {t.name: t.outputSchema for t in tools} == \
        {t.name: None for t in tools}


# --------------------------------------------------------------------- #
# The oracle proof                                                      #
# --------------------------------------------------------------------- #

# Every node the comparator cannot check mechanically, by exact path —
# asserted, so a new incomparable can never slip in silently.
EXPECTED_SKIPS = frozenset({
    # whole rows: tools returning dynamic dicts (typed values, not
    # dataclasses) — doctor's report, apply's result, spool state keys,
    # triage's mailbox shapes
    "doctor",
    "list_scheduled",
    "mailbox_create",
    "mailbox_delete",
    "triage_apply",
    # dynamic passthrough fields inside typed rows
    "search_emails.fts",                    # index-health dict
    "get_email.email.headers",              # header names are data
    "get_email.email.flags",
    "get_emails_batch.emails[].headers",
    "get_emails_batch.emails[].flags",
    "refresh_mail.before",                  # freshness snapshots
    "refresh_mail.after",
    "audit.events[]",                       # ledger events: optional keys
    # added post-boundary by the MCP wrapper only when the dispatcher is
    # missing — conditional presence, so not part of the typed return
    "schedule_email.warning",
    # ADDITIVE keys since the oracle was taken (contract §8, 2026-08-01):
    # ghost-mailbox honesty — local vs server-side counts, and the note
    # explaining an empty page scoped to a server-side-only mailbox
    "list_mailboxes.mailboxes[].local_count",
    "list_recent.note",
    "search_emails.note",
    # body-gap fix (2026-08-06): declared provenance on backfilled bodies
    "get_email.email.body_source",
    "get_emails_batch.emails[].body_source",
})

# oracle keys whose ABSENCE from the derived shape is expected (see above)
_WAIVED_MISSING = frozenset({"schedule_email.warning"})

# derived keys the oracle never saw: deliberate additive growth after the
# shipped snapshot (contract §8) — any OTHER addition still fails the proof
_WAIVED_ADDED = frozenset({
    "list_mailboxes.mailboxes[].local_count",
    "list_recent.note",
    "search_emails.note",
    # body-gap fix (2026-08-06): backfilled bodies carry declared
    # provenance — additive on the full view only
    "get_email.email.body_source",
    "get_emails_batch.emails[].body_source",
})


def _compare(oracle, derived, path: str, skips: set, bad: list) -> None:
    """Oracle (fixture-observed: dicts, [element] lists, scalar type
    names) vs derived (typed: adds scalar unions "a|b", {"one_of": […]},
    opaque "dict"). Agreement is exact except where recorded in `skips`."""
    if isinstance(derived, str) and "dict" in derived.split("|"):
        if isinstance(oracle, dict):
            skips.add(path)
        else:
            bad.append(f"{path}: oracle {oracle!r} vs opaque {derived!r}")
        return
    if isinstance(derived, dict) and set(derived) == {"one_of"}:
        for variant in derived["one_of"]:
            vskips: set = set()
            vbad: list = []
            _compare(oracle, variant, path, vskips, vbad)
            if not vbad:
                skips |= vskips
                return
        bad.append(f"{path}: no one_of variant matches the oracle")
        return
    if isinstance(oracle, dict):
        if not isinstance(derived, dict):
            bad.append(f"{path}: oracle object vs derived {derived!r}")
            return
        for key in oracle:
            child = f"{path}.{key}"
            if key not in derived:
                if child in _WAIVED_MISSING:
                    skips.add(child)
                else:
                    bad.append(f"{child}: frozen by the oracle, "
                               "missing from the derived shape")
                continue
            _compare(oracle[key], derived[key], child, skips, bad)
        for key in derived:
            if key not in oracle:
                child = f"{path}.{key}"
                if child in _WAIVED_ADDED:
                    skips.add(child)
                else:
                    bad.append(f"{child}: derived adds a key the oracle "
                               "never froze")
        return
    if isinstance(oracle, list):
        if not isinstance(derived, list):
            bad.append(f"{path}: oracle array vs derived {derived!r}")
        elif oracle:  # [] = observed empty: nothing frozen to compare
            _compare(oracle[0], derived[0], f"{path}[]", skips, bad)
        return
    # scalars: the oracle's observed type must be a member of the
    # derived union ("null" observed where the type says "str|null").
    if not (isinstance(derived, str) and oracle in derived.split("|")):
        bad.append(f"{path}: oracle {oracle!r} vs derived {derived!r}")


def test_derived_shapes_agree_with_the_shipped_oracle():
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))["success_shapes"]
    derived = derived_schemas()
    # The oracle is the SHIPPED v0.11 surface; tools added since
    # (create_draft, 2026-08-02) are additive and frozen by the live
    # snapshot above, not by the oracle.
    assert set(oracle) <= set(derived)

    skips: set = set()
    bad: list = []
    for name in sorted(oracle):
        _compare(oracle[name], derived[name], name, skips, bad)
    assert bad == []
    assert skips == EXPECTED_SKIPS
