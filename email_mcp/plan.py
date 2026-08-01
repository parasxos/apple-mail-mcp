"""Destructive work as data (concept primitive 3, docs/v0.11-clean-concept.md).

One Action algebra, computed once, consumed twice:

    plan = [Action | Kept | PrintOnly, ...]      # built by a verb
    render(plan)  -> the preview text            # pure
    execute(plan) -> list[ActionResult]          # the ONLY effectful walk

Preview and execution cannot diverge: they are the same list. Fences live
in the constructors — RemoveTree exists only for the HARDCODED
~/.email-mcp and refuses symlink/home/non-dir via os.lstat at CONSTRUCTION
time, so a refusable action never enters a plan and the preview already
shows every refusal the executor would have hit.

ActionResult carries evidence — the target's before/after existence — so
partial-failure honesty ("still present or partly removed — check it
yourself") is derived from the filesystem, never asserted. The exit code
of every destructive verb is a fold over the same results the report
renders from (lifecycle.confirm_and_run owns that fold).

The executor emits the two additive audit event types of contract §8:
ONE `lifecycle` event per verb run, BEFORE the first effect — recorded
while the ledger still exists, because uninstall --purge is about to
remove it — and one `doctor_fix` event per executed repair.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from . import audit
from .log import get_logger

_log = get_logger()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    """THE seam: tests monkeypatch this one symbol (mirrors doctor's)."""
    return subprocess.run(["launchctl", *args],
                          capture_output=True, text=True, timeout=30)


# ---------------------------------------------------------------------- #
# rows                                                                    #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class Kept:
    """A path the verb deliberately leaves alone: renders in the preview,
    executes as a no-op success."""

    path: Path
    note: str

    def describe(self) -> str:
        return f"keep {self.path} — {self.note}"


@dataclass(frozen=True)
class PrintOnly:
    """A row that can only be said, never done. Env-overridden or refused
    paths ride here, so the preview names exactly what will NOT be
    touched — and the executor cannot touch it, because there is nothing
    to run."""

    text: str

    def describe(self) -> str:
        return self.text


class Action:
    """Base of the algebra: one effect, fenced at construction, judged by
    evidence at execution. `wants` is the goal state of `target()` that
    the evidence is folded against ("absent" | "present" | None)."""

    wants: ClassVar[str | None] = None

    def target(self) -> Path | None:
        return None

    def describe(self) -> str:
        raise NotImplementedError

    def _run(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class UnlinkFile(Action):
    """Remove one file (or symlink — the link itself, never its target).
    An already-absent path is the goal state, not an error."""

    path: Path
    wants: ClassVar[str] = "absent"

    def target(self) -> Path:
        return self.path

    def describe(self) -> str:
        return f"remove file {self.path}"

    def _run(self) -> None:
        Path(self.path).unlink(missing_ok=True)


@dataclass(frozen=True)
class RemoveTree(Action):
    """Recursive removal of the managed state tree — and nothing else.

    Exists ONLY for the hardcoded ~/.email-mcp: construction refuses any
    other path, and the one constructor, `state_root()`, lstat-fences
    symlink/home/non-dir into Kept/PrintOnly rows so a refusable removal
    never enters a plan. Execution is shutil.rmtree, which itself refuses
    a symlink root and walks fd-based on macOS (no TOCTOU re-check
    needed — the fence is the constructor plus rmtree's own)."""

    root: Path
    wants: ClassVar[str] = "absent"

    def __post_init__(self) -> None:
        if self.root != Path.home() / ".email-mcp":
            raise ValueError(
                "RemoveTree exists only for ~/.email-mcp — build it via "
                "RemoveTree.state_root()."
            )

    @staticmethod
    def state_root() -> "RemoveTree | Kept | PrintOnly":
        root = Path.home() / ".email-mcp"
        try:
            lst = os.lstat(root)
        except FileNotFoundError:
            return Kept(root, "already absent")
        except OSError as e:
            return PrintOnly(f"NOT removing {root}: cannot stat it ({e}) — "
                             f"check it yourself")
        if stat.S_ISLNK(lst.st_mode):
            return PrintOnly(f"NOT removing {root}: it is a symlink — "
                             f"remove the link and its target yourself")
        if not stat.S_ISDIR(lst.st_mode):
            return PrintOnly(f"NOT removing {root}: it is not a directory — "
                             f"move it aside yourself")
        try:
            hst = os.stat(Path.home())
        except OSError:
            hst = None
        if hst is not None and (lst.st_dev, lst.st_ino) == \
                (hst.st_dev, hst.st_ino):
            return PrintOnly(f"NOT removing {root}: it IS the home "
                             f"directory (by inode)")
        return RemoveTree(root)

    def target(self) -> Path:
        return self.root

    def describe(self) -> str:
        return f"remove tree {self.root} (recursive)"

    def _run(self) -> None:
        shutil.rmtree(self.root)


@dataclass(frozen=True)
class RenameAside(Action):
    """Move a squatter out of the way, non-clobbering: the destination is
    derived (<name>.bak) and an existing one fails honestly instead of
    being silently replaced (os.rename would)."""

    path: Path
    wants: ClassVar[str] = "absent"

    @property
    def dest(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def target(self) -> Path:
        return self.path

    def describe(self) -> str:
        return f"move {self.path} aside -> {self.dest}"

    def _run(self) -> None:
        if os.path.lexists(self.dest):
            raise FileExistsError(f"{self.dest} already exists — move it "
                                  f"out of the way first")
        os.rename(self.path, self.dest)


@dataclass(frozen=True)
class Chmod(Action):
    """Set a managed path's mode — never through a symlink (a leaf that
    became a link between probe and execute fails, not chases)."""

    path: Path
    mode: int
    wants: ClassVar[str] = "present"

    def target(self) -> Path:
        return self.path

    def describe(self) -> str:
        return f"chmod {self.mode:o} {self.path}"

    def _run(self) -> None:
        os.chmod(self.path, self.mode, follow_symlinks=False)


@dataclass(frozen=True)
class WriteFile(Action):
    """Atomic file write at a fixed mode (temp created already-restrictive,
    then renamed over). backup=True copies the current file to <name>.bak
    first — backup-first for the one file a wizard may rewrite."""

    path: Path
    content: str
    mode: int = 0o600
    backup: bool = False
    wants: ClassVar[str] = "present"

    def target(self) -> Path:
        return self.path

    def describe(self) -> str:
        n = len(self.content.encode("utf-8"))
        via = ", current kept as .bak" if self.backup else ""
        return f"write {self.path} ({n} bytes, mode {self.mode:o}{via})"

    def _run(self) -> None:
        if self.backup and os.path.lexists(self.path):
            shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.content)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, self.mode)  # O_CREAT modes are umask-clipped


@dataclass(frozen=True)
class BootoutAgent(Action):
    """launchctl bootout by service target. An agent that is not loaded is
    the goal state, not an error ("No such process")."""

    label: str

    def describe(self) -> str:
        return f"boot out launchd agent {self.label}"

    def _run(self) -> None:
        proc = _launchctl("bootout", f"gui/{os.getuid()}/{self.label}")
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "No such process" in err or proc.returncode == 3:
                return  # was not loaded — already the goal state
            raise OSError(f"launchctl bootout {self.label}: "
                          f"{err or f'exit {proc.returncode}'}")


@dataclass(frozen=True)
class BootstrapAgent(Action):
    """(Re)load a launchd agent from its plist: bootout first so a changed
    plist takes effect, then bootstrap."""

    label: str
    plist: Path
    wants: ClassVar[str] = "present"

    def target(self) -> Path:
        return self.plist

    def describe(self) -> str:
        return f"bootstrap launchd agent {self.label} ({self.plist})"

    def _run(self) -> None:
        uid = os.getuid()
        _launchctl("bootout", f"gui/{uid}", str(self.plist))  # refresh
        proc = _launchctl("bootstrap", f"gui/{uid}", str(self.plist))
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise OSError(f"launchctl bootstrap {self.label}: "
                          f"{err or f'exit {proc.returncode}'}")


Row = Action | Kept | PrintOnly


# ---------------------------------------------------------------------- #
# the two consumers of one list                                           #
# ---------------------------------------------------------------------- #


def render(plan: list[Row]) -> str:
    """The preview: pure text over the SAME list execute() consumes."""
    return "\n".join(row.describe() for row in plan)


@dataclass(frozen=True)
class ActionResult:
    """One row's outcome plus the evidence it is judged by: the target's
    existence before and after. `ok` requires the run not to raise AND the
    evidence to show the goal state — honesty is derived, never asserted."""

    row: Row
    ok: bool
    before: bool | None
    after: bool | None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.ok

    def describe(self) -> str:
        if self.ok:
            return f"ok: {self.row.describe()}"
        note = self.error or "goal not reached"
        return f"FAILED: {self.row.describe()} — {note}{self._evidence()}"

    def _evidence(self) -> str:
        wants = getattr(self.row, "wants", None)
        if wants == "absent":
            if self.after:
                return " (still present or partly removed — check it yourself)"
            return " (now absent)"
        if wants == "present" and self.after is False:
            return " (not written)"
        return ""


def _goal_met(action: Action, after: bool | None) -> bool:
    if action.wants == "absent":
        return after is False
    if action.wants == "present":
        return after is True
    return True


def execute(plan: list[Row], *, verb: str) -> list[ActionResult]:
    """The ONLY effectful walk over a plan.

    `verb` tags the audit trail: "doctor_fix" emits one doctor_fix event
    per executed action, after it ran; any lifecycle verb (setup / update /
    uninstall) emits ONE lifecycle event before the first effect — while
    the ledger this event lands in still exists. A plan with no Action
    rows mutates nothing and therefore records nothing (the ledger indexes
    mutations, contract §6)."""
    actions = [row for row in plan if isinstance(row, Action)]
    if actions and verb != "doctor_fix":
        audit.emit("lifecycle", outcome=verb,
                   detail={"plan": [row.describe() for row in plan]})
    results: list[ActionResult] = []
    for row in plan:
        if not isinstance(row, Action):
            results.append(ActionResult(row, ok=True, before=None, after=None))
            continue
        target = row.target()
        before = os.path.lexists(target) if target is not None else None
        error: str | None = None
        try:
            row._run()
        except Exception as e:  # the one boundary: errors become values
            error = str(e) or type(e).__name__
            _log.warning("plan[%s]: %s failed: %s", verb, row.describe(), error)
        after = os.path.lexists(target) if target is not None else None
        ok = error is None and _goal_met(row, after)
        results.append(ActionResult(row, ok, before, after, error))
        if verb == "doctor_fix":
            detail: dict = {"action": row.describe()}
            if error:
                detail["error"] = error
            audit.emit("doctor_fix", outcome="fixed" if ok else "failed",
                       detail=detail)
    return results


def render_results(results: list[ActionResult]) -> str:
    """The report: derived from the SAME results the exit code folds over,
    so a failure that reaches neither is unrepresentable."""
    lines = [r.describe() for r in results]
    left = [r for r in results if r.failed]
    lines.append(f"{len(results) - len(left)} done, {len(left)} left behind"
                 if left else f"{len(results)} done")
    return "\n".join(lines)
