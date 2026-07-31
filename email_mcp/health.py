"""ONE definition of "is a report about our own state trustworthy?".

Five release-gate FAILs in a row were the same shape: a caveat was wired
into one reporting surface and forgotten on its sibling. `dispatcher
--status` learned to flag a refused root; `tool_list_scheduled` did not.
Both learned it; then unreadable manifests were flagged in `--status` and
`doctor` but not in the tools. Each fix was correct and each left a twin.

So the computation lives here, once, and every surface that reports counts,
lists or an `ok` about state THIS TOOL owns calls `caveats()` and merges the
result. A new surface that forgets to call it is a visible omission; a new
surface that calls it cannot get half of it.

Two things make such a report untrustworthy:

* **The root is refused.** Scans honestly return empty for a directory we
  may not manage, so "0 pending" and "unmanageable" look identical.
* **We could not read something we own.** A truncated manifest — what a
  crash or ENOSPC leaves mid-save — is real queued mail that the scan
  skips, so "0 pending" and "a message we cannot parse" look identical.

Both cases mean the same thing to a caller: *do not read this as an empty
queue.* That is what `counts_are_meaningful: false` says.
"""
from __future__ import annotations

from . import config

# Keys this module owns on any report it annotates. Named so a reader of a
# response knows they all come from one place.
REFUSED_KEY = "state_root_refused"
UNREADABLE_KEY = "unreadable"
MEANINGFUL_KEY = "counts_are_meaningful"


def caveats(*, spool_states: tuple[str, ...] | None = None,
            audit_query: dict | None = None) -> dict:
    """Everything that makes a state report untrustworthy, or {}.

    `spool_states` — states whose scan just ran; their unreadable-manifest
    record is consulted. `audit_query` — the dict `audit.query()` returned,
    whose unreadable-file record is consulted.

    Callers merge the result into their own response. An empty dict means
    the report can be taken at face value.
    """
    out: dict = {}
    unreadable: dict[str, list[str]] = {}

    refusal = config.state_root_refusal()
    if refusal is not None:
        out[REFUSED_KEY] = refusal

    if spool_states:
        from . import spool

        for state in spool_states:
            # Scan HERE rather than trusting a caller to have scanned
            # first: spool.unreadable() reflects the last scan, so a
            # caller that asked too early got a silent {} — precisely the
            # implicit coupling this module exists to remove. entries() is
            # a pure read, so doing it again is cheap and cannot mislead.
            spool.entries(state)
            names = spool.unreadable(state)
            if names:
                unreadable[f"spool/{state}"] = names

    if audit_query is not None:
        names = list(audit_query.get("unreadable_files") or ())
        if names:
            unreadable["audit"] = names

    if unreadable:
        out[UNREADABLE_KEY] = unreadable
    if out:
        out[MEANINGFUL_KEY] = False
    return out


def summarize(caveat: dict) -> list[str]:
    """One operator-facing line per caveat, for CLI stderr."""
    lines: list[str] = []
    if caveat.get(REFUSED_KEY):
        lines.append(str(caveat[REFUSED_KEY]))
    for where, names in sorted((caveat.get(UNREADABLE_KEY) or {}).items()):
        lines.append(
            f"{where}: cannot read {', '.join(sorted(names))} — data may be "
            "present but uncounted")
    if lines:
        lines.append("the counts are NOT meaningful — run `email-mcp doctor`.")
    return lines
