"""One registry behind doctor, --fix, setup and update (concept primitive 4).

    Check = (id, probe: StateReader -> Finding | None,
                 repair: (StateWriter, Finding) -> list[Action] | None)

Probes are read-only by TYPE: they hold a StateReader, which has nothing
effectful to call. Repairs map a finding to the plan.py action(s) that fix
it, and receive a StateWriter as proof of an adoptable root — a writer
cannot exist for a refused root, so a repair mutating under one is
uncompilable, not merely untested. That is the whole gate. Adoption
itself rides the plan as its first row (plan.AdoptRoot), so even that
effect happens inside execute(), never while the plan is built.

`doctor` renders `findings()`; `doctor --fix` hands `plan_fix()` to
plan.execute — the same executor uninstall uses, so audit events, dry-run
purity and failure honesty are inherited, not re-implemented. `setup` is
this registry run forward on a freshly adopted tree; `update` is the
migration subset (meta state_version compare + stale plist re-render).

meta.json (the managed root's state_version stamp) is read here and
written only through the meta_version repair.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import plan, state
from .state import StateReader, StateWriter

STATE_VERSION = 1
META = "meta.json"

STATE_ROOT = "state_root"
TREE_MODES = "tree_modes"
SECRET_MODES = "secret_modes"
AUDIT_SQUAT = "audit_squat"
META_VERSION = "meta_version"
LEGACY_LAUNCHD = "legacy_launchd"
PLIST_DRIFT = "plist_drift"


@dataclass(frozen=True)
class Finding:
    """One check's verdict: which check, what is wrong, at which paths."""

    check: str
    detail: str
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Check:
    id: str
    probe: Callable[[StateReader], "Finding | None"]
    repair: Callable[[StateWriter, Finding], "list[plan.Action]"] | None = None


# ---------------------------------------------------------------------- #
# the managed surface                                                     #
# ---------------------------------------------------------------------- #


def _managed_dirs(reader: StateReader) -> list[Path]:
    return [reader.root, reader.spool,
            *(reader.spool_state(s) for s in state.SPOOL_STATES),
            reader.plans, reader.graph, reader.fts, reader.audit]


def _secret_files(reader: StateReader) -> list[Path]:
    """The files whose bytes are worth protecting: the marker + meta +
    identities at the root, graph token caches, audit months."""
    files = [reader.marker, reader.root / META,
             reader.root / "identities.toml"]
    for d, suffix in ((reader.graph, ".token.json"),
                      (reader.audit, ".jsonl")):
        try:
            names = sorted(os.listdir(d))  # listdir raises; glob would swallow
        except OSError:
            names = []
        files += [d / n for n in names if n.endswith(suffix)]
    return files


def _mode_offenders(paths: list[Path], want: int,
                    kind: Callable[[int], bool]) -> list[Path]:
    out = []
    for p in paths:
        try:
            lst = os.lstat(p)
        except OSError:
            continue  # absent (fresh install) or unreachable — not a mode fault
        if not kind(lst.st_mode) or stat.S_ISLNK(lst.st_mode):
            continue  # squats and symlinks are other checks' findings
        if stat.S_IMODE(lst.st_mode) != want:
            out.append(p)
    return out


def _agents() -> tuple[tuple[str, Path, Callable[[], str]], ...]:
    """The launchd agents this package renders, as (label, plist, content).
    Lazy imports: probing must not drag the whole send stack in at
    module-import time."""
    from . import dispatcher, fts
    return (
        (dispatcher.LAUNCHD_LABEL, dispatcher._plist_path(),
         dispatcher._plist_content),
        (fts.LAUNCHD_LABEL, fts._plist_path(), fts._plist_content),
    )


# ---------------------------------------------------------------------- #
# meta.json — the managed root's state_version stamp                      #
# ---------------------------------------------------------------------- #


def read_state_version(reader: StateReader) -> int | None:
    """meta.json's state_version, or None (absent, unreadable, malformed)."""
    try:
        data = json.loads((reader.root / META).read_bytes())
    except (OSError, ValueError):
        return None
    v = data.get("state_version") if isinstance(data, dict) else None
    return v if isinstance(v, int) else None


def _meta_stamp() -> str:
    return json.dumps({"state_version": STATE_VERSION}) + "\n"


# ---------------------------------------------------------------------- #
# probes and repairs                                                      #
# ---------------------------------------------------------------------- #


def _probe_tree_modes(reader: StateReader) -> Finding | None:
    bad = _mode_offenders(_managed_dirs(reader), 0o700, stat.S_ISDIR)
    if not bad:
        return None
    return Finding(TREE_MODES,
                   f"{len(bad)} managed dir(s) not 0700: "
                   f"{', '.join(str(p) for p in bad)}",
                   tuple(bad))


def _repair_tree_modes(writer: StateWriter, f: Finding) -> list[plan.Action]:
    return [plan.Chmod(p, 0o700) for p in f.paths]


def _probe_secret_modes(reader: StateReader) -> Finding | None:
    bad = _mode_offenders(_secret_files(reader), 0o600, stat.S_ISREG)
    if not bad:
        return None
    return Finding(SECRET_MODES,
                   f"{len(bad)} secret file(s) not 0600: "
                   f"{', '.join(str(p) for p in bad)}",
                   tuple(bad))


def _repair_secret_modes(writer: StateWriter, f: Finding) -> list[plan.Action]:
    return [plan.Chmod(p, 0o600) for p in f.paths]


def _probe_audit_squat(reader: StateReader) -> Finding | None:
    try:
        lst = os.lstat(reader.audit)
    except OSError:
        return None  # absent = fresh install, unreachable = root's problem
    if stat.S_ISDIR(lst.st_mode):  # lstat: a symlink never reads as a dir
        return None
    return Finding(AUDIT_SQUAT,
                   f"{reader.audit} exists but is not a real directory — "
                   f"every audit event is being dropped",
                   (reader.audit,))


def _repair_audit_squat(writer: StateWriter, f: Finding) -> list[plan.Action]:
    # Rename aside only; the real 0700 leaf is created by the next
    # adoption the moment anything writes — the one effectful door.
    return [plan.RenameAside(f.paths[0])]


def _probe_meta_version(reader: StateReader) -> Finding | None:
    if not os.path.isdir(reader.root):
        return None  # a root that does not exist yet has nothing to version
    v = read_state_version(reader)
    if v == STATE_VERSION:
        return None
    detail = (f"{reader.root / META} absent — the state tree is unstamped"
              if v is None else
              f"state_version {v} != {STATE_VERSION} — migration pending")
    return Finding(META_VERSION, detail, (reader.root / META,))


def _repair_meta_version(writer: StateWriter, f: Finding) -> list[plan.Action]:
    return [plan.WriteFile(writer.root / META, _meta_stamp(), mode=0o600)]


def _probe_legacy_launchd(reader: StateReader) -> Finding | None:
    from . import dispatcher
    agents = Path.home() / "Library" / "LaunchAgents"
    stray = [agents / f"{label}.plist" for label in dispatcher.LEGACY_LABELS
             if os.path.lexists(agents / f"{label}.plist")]
    if not stray:
        return None
    return Finding(LEGACY_LAUNCHD,
                   f"legacy launchd agent plist(s): "
                   f"{', '.join(p.name for p in stray)} — a second "
                   f"dispatcher may be ticking over the same spool",
                   tuple(stray))


def _repair_legacy_launchd(writer: StateWriter, f: Finding) -> list[plan.Action]:
    rows: list[plan.Action] = []
    for plist in f.paths:
        rows += [plan.BootoutAgent(plist.stem), plan.UnlinkFile(plist)]
    return rows


def _probe_plist_drift(reader: StateReader) -> Finding | None:
    stale = []
    for label, plist, content in _agents():
        try:
            current = plist.read_text(encoding="utf-8")
        except OSError:
            continue  # not installed — nothing to be stale
        if current != content():
            stale.append(plist)
    if not stale:
        return None
    return Finding(PLIST_DRIFT,
                   f"{len(stale)} installed launchd plist(s) differ from "
                   f"the current render (moved interpreter, changed PATH): "
                   f"{', '.join(p.name for p in stale)}",
                   tuple(stale))


def _repair_plist_drift(writer: StateWriter, f: Finding) -> list[plan.Action]:
    rows: list[plan.Action] = []
    for label, plist, content in _agents():
        if plist in f.paths:
            rows += [plan.WriteFile(plist, content(), mode=0o644),
                     plan.BootstrapAgent(label, plist)]
    return rows


REGISTRY: tuple[Check, ...] = (
    Check(TREE_MODES, _probe_tree_modes, _repair_tree_modes),
    Check(SECRET_MODES, _probe_secret_modes, _repair_secret_modes),
    Check(AUDIT_SQUAT, _probe_audit_squat, _repair_audit_squat),
    Check(META_VERSION, _probe_meta_version, _repair_meta_version),
    Check(LEGACY_LAUNCHD, _probe_legacy_launchd, _repair_legacy_launchd),
    Check(PLIST_DRIFT, _probe_plist_drift, _repair_plist_drift),
)


# ---------------------------------------------------------------------- #
# the two consumers                                                       #
# ---------------------------------------------------------------------- #


def _survey() -> state.Refused | tuple[state.Resolved,
                                       list[tuple[Check, Finding]]]:
    r = state.State.resolve()
    if isinstance(r, state.Refused):
        return r
    reader = r.reader()
    hits = [(c, f) for c in REGISTRY if (f := c.probe(reader)) is not None]
    return r, hits


def findings() -> list[Finding]:
    """Probe every check (doctor renders these). A refused root is ONE
    finding carrying the same reason string every other surface shows —
    and the only one, because no reader exists to probe anything else."""
    survey = _survey()
    if isinstance(survey, state.Refused):
        return [Finding(STATE_ROOT, survey.reason)]
    return [f for _, f in survey[1]]


def plan_fix(only: frozenset[str] | None = None) -> list[plan.Row]:
    """Map findings to their repair actions — the plan doctor --fix hands
    to plan.execute. `only` restricts to a subset of check ids (update's
    migration slice). Adoption is the plan's FIRST row, never a build
    effect: the writer the repairs are built against comes to exist
    inside execute(), so a dry run or a declined confirmation adopts
    nothing. For a refused root no StateWriter can exist — the answer is
    a PrintOnly report, never a mutation."""
    survey = _survey()
    if isinstance(survey, state.Refused):
        return [plan.PrintOnly(f"cannot fix: {survey.reason}")]
    resolved, hits = survey
    todo = [(c, f) for c, f in hits
            if c.repair is not None and (only is None or c.id in only)]
    if not todo:
        return []
    adopt = plan.AdoptRoot(resolved)
    rows: list[plan.Row] = [adopt]
    for c, f in todo:
        rows += c.repair(adopt.writer, f)
    return rows
