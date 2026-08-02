#!/usr/bin/env python3
"""v1.0-rc runner core — the harness the 18-phase RC programme rides on.

``docs/w3-rc-plan.md`` is the plan this file executes: 18 phases across
two lanes — a *sandbox* lane (a throwaway ``$HOME`` with the real Mail
store attached read-only through ``EMAIL_MCP_MAIL_DIR``) and a *prod*
lane (self-only sends on the real estate). R1 shipped the core:
Context, Report, Sentinel, the phase registry, the
resume/--phase/--dry-run plumbing and the manual-step protocol. Phase
bodies attach through ``@implements`` (S1 binds the sandbox core,
P01–P05, below); a phase with no body is reported as ``unimplemented``
rather than silently passing.

Two rules shape everything here.

**--dry-run is the default.** A bare invocation plans and prints: it
spawns no process, writes no file, and renders the report to stdout
instead of ``docs/``. Effects require ``--execute``, typed by a human.

**The Sentinel is not optional.** Sandbox launchd actions share the
per-user label space with the prod agents, so a sandbox phase can boot
out the real dispatcher without ever naming it. Before anything runs,
the Sentinel takes a sha256 manifest of the real ``~/.email-mcp`` plus
the state of every prod launchd label; afterwards it proves exactly what
changed, separating the churn a run legitimately produces (ledger,
spool, index) from anything that touched identities, credentials or the
agents. If it cannot read that state it refuses to start — an RC that
cannot see the estate it might damage does not begin — and ``--execute``
may not disable it.

Deliberately self-contained: stdlib only, and it never imports
``email_mcp``. The runner drives an INSTALLED wheel through subprocess
and a real MCP client; importing the repo source would exercise the
wrong bytes (same reason ``tools/graph_probe.py`` imports nothing).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# --------------------------------------------------------------------- #
# vocabulary                                                             #
# --------------------------------------------------------------------- #

SANDBOX = "sandbox"
PROD = "prod"
BOTH = "both"

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
DRY = "dry"
PENDING = "manual-pending"
RUNNING = "running"
UNIMPLEMENTED = "unimplemented"

# Statuses a resumed run does not repeat. RUNNING is deliberately absent:
# it means the process died mid-phase, so the phase must run again.
SETTLED = frozenset({PASS, SKIPPED, DRY})

EXIT_OK = 0
EXIT_PHASE_FAILED = 1
EXIT_USAGE = 2
EXIT_SENTINEL_REFUSED = 3
EXIT_SENTINEL_DRIFT = 4
EXIT_INCOMPLETE = 5

# The prod agents whose label space a sandbox launchd action shares.
PROD_LABELS = (
    "com.email-mcp.dispatcher",
    "com.email-mcp.fts",
    "com.paris.email-mcp-dispatcher",  # v0.9 legacy, exercised by P15
)

STATE_ROOT_NAME = ".email-mcp"
# The RC's own journals live OUTSIDE the sentinel root on purpose: state
# written by the runner must never show up as drift in its own manifest.
RC_DIRNAME = ".email-mcp-rc"

# Paths under ~/.email-mcp a run is allowed to churn. The ledger gains an
# event per mutation by design, the spool moves manifests between states,
# the index rebuilds, plans are written and GC'd, and a Graph send
# rotates its token cache. Everything else — identities.toml, meta.json,
# an unknown new file at the root — is material drift.
EXPECTED_CHANGE = ("audit/*", "spool/*", "fts/*", "plans/*", "graph/*.token.json")
# Removal is judged separately and more harshly: a vanished token cache
# is always reported, even though rewriting one is routine.
EXPECTED_REMOVAL = ("spool/*", "plans/*", "fts/*")

# `launchctl print` lines that differ between two reads of a healthy
# agent. Dropped before digesting so ordinary ticking is not drift.
_VOLATILE = ("pid =", "runs =", "last exit code", "state =", "active count",
             "immediate reason", "spawn type", "properties")


class SentinelError(RuntimeError):
    """The Sentinel cannot see the real state — the run must not start."""


class UnsafeAction(RuntimeError):
    """A phase tried to reach past its lane's boundary."""


class PhaseFailure(AssertionError):
    """A phase's acceptance criterion was not met."""


# --------------------------------------------------------------------- #
# the one spawn point                                                    #
# --------------------------------------------------------------------- #


def _spawn(argv, *, cwd=None, env=None, timeout=None, stdin_text=None):
    """The single place this module starts a process.

    Funnelled through one function so a test can fence it and prove a
    dry run spawned nothing at all — including the Sentinel's launchctl
    reads, which must not touch the real launchd from a test.
    """
    import subprocess  # local: nothing above this line may spawn

    return subprocess.run(
        [str(a) for a in argv],
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=timeout,
        input=stdin_text,
        capture_output=True,
        text=True,
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- #
# the plan: 18 phases (bodies attach via @implements)                    #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhaseSpec:
    id: str
    title: str
    lane: str
    acceptance: str
    manual: bool = False
    once: bool = False          # done once ever, not once per run (P18)
    sentinel_strict: bool = False  # verify with zero expected change after


PLAN: tuple[PhaseSpec, ...] = (
    PhaseSpec("P01", "wheel install", SANDBOX,
              "the built wheel installs into the sandbox and `email-mcp "
              "version` reports the wheel's version"),
    PhaseSpec("P02", "scripted setup", SANDBOX,
              "`email-mcp setup` from a fixture answer file leaves a 0700 "
              "tree, 0600 identities.toml, and prints an absolute MCP entry"),
    PhaseSpec("P03", "doctor", BOTH,
              "every doctor check green in the sandbox; prod doctor reports "
              "the real estate healthy and names a fix for anything it isn't"),
    PhaseSpec("P04", "index", BOTH,
              "sandbox `fts --build --limit N` completes and is searchable; "
              "prod `fts --status` shows a full, non-stale index"),
    PhaseSpec("P05", "wire-level search/read", SANDBOX,
              "an MCP client subprocess gets contract envelopes back from "
              "search_emails and get_email over stdio — no exception on the wire"),
    PhaseSpec("P06", "send", BOTH,
              "a self-only send delivers on each lane and its Message-ID is "
              "found in the store afterwards"),
    PhaseSpec("P07", "schedule via launchd, then cancel", SANDBOX,
              "schedule_email spools, the dispatcher ticks it, and "
              "cancel_scheduled moves a second entry to cancelled/"),
    PhaseSpec("P08", "schedule via graph", PROD,
              "a Graph-executor schedule lands as a deferred draft and is "
              "cancelled cleanly"),
    PhaseSpec("P09", "lid-closed delivery", PROD,
              "a scheduled send delivers with the lid closed, within the "
              "tolerance documented in the transport design", manual=True),
    PhaseSpec("P10", "triage plans", SANDBOX,
              "plan → apply (move + flag) verified against the store, and "
              "the audit event carries the plan's summary line"),
    PhaseSpec("P11", "trash plan", SANDBOX,
              "triage_plan_delete → apply lands exactly the planned messages "
              "in Trash, verified against the store"),
    PhaseSpec("P12", "audit inspection", BOTH,
              "exactly one audit event per mutation of this run, each "
              "threading to its operation_id"),
    PhaseSpec("P13", "failure matrix FM1-FM10", SANDBOX,
              "each injected failure yields its coded envelope and loses no "
              "mail; FM3's at-most-once window is DEMONSTRATED, not reconciled"),
    PhaseSpec("P14", "permission revoke / regrant", PROD,
              "with Full Disk Access revoked, doctor names the exact fix; "
              "after regrant every check is green again", manual=True),
    PhaseSpec("P15", "upgrade from v0.9", SANDBOX,
              "v0.9 state generated by a worktree at 75c6f93 (legacy plist "
              "included) is operated by the current wheel, and one v0.9-frozen "
              "spool entry actually delivers"),
    PhaseSpec("P16", "uninstall + purge", SANDBOX,
              "uninstall removes the sandbox agents and token caches, and the "
              "Sentinel proves the real estate is byte-identical",
              sentinel_strict=True),
    PhaseSpec("P17", "teardown", PROD,
              "the prod agents are re-bootstrapped and the dispatcher is "
              "observed to tick within 90s"),
    PhaseSpec("P18", "fresh macOS user account walk", PROD,
              "a brand-new macOS account reaches a working read-only server "
              "in under 15 minutes with no archaeology", manual=True, once=True),
)

PLAN_BY_ID = {spec.id: spec for spec in PLAN}

IMPLEMENTATIONS: dict[str, Callable[["Context"], None]] = {}


def implements(phase_id: str):
    """Bind a body to a planned phase. Registration is the whole
    contract: an id not in PLAN, or bound twice, is a programming error
    the runner refuses to paper over."""
    if phase_id not in PLAN_BY_ID:
        raise KeyError(f"{phase_id} is not in the plan (see docs/w3-rc-plan.md)")

    def deco(fn):
        if phase_id in IMPLEMENTATIONS:
            raise KeyError(f"{phase_id} already has an implementation")
        IMPLEMENTATIONS[phase_id] = fn
        return fn

    return deco


# --------------------------------------------------------------------- #
# Sentinel                                                               #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Snapshot:
    taken_at: str
    root: str
    files: dict[str, str]
    agents: dict[str, dict]

    def as_dict(self) -> dict:
        return {"taken_at": self.taken_at, "root": self.root,
                "files": self.files, "agents": self.agents}

    @classmethod
    def from_dict(cls, raw: dict) -> "Snapshot":
        return cls(raw["taken_at"], raw["root"], raw["files"], raw["agents"])


@dataclass
class SentinelDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    agents_drifted: list[str] = field(default_factory=list)
    agents_noted: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.added or self.changed or self.removed
                    or self.agents_drifted)

    def render(self) -> str:
        if self.clean:
            body = ["The real estate is intact: no material change under the "
                    "state root, every prod agent in its original state."]
            if self.expected:
                body.append(f"Expected churn (ignored by policy): "
                            f"{len(self.expected)} path(s) — "
                            f"{', '.join(self.expected[:8])}"
                            + (" …" if len(self.expected) > 8 else ""))
            if self.agents_noted:
                body.append("Agent output differed only in volatile fields: "
                            + ", ".join(self.agents_noted))
            return "\n\n".join(body)
        lines = ["**MATERIAL DRIFT** — the run changed state it does not own:"]
        for label, items in (("added", self.added), ("changed", self.changed),
                             ("removed", self.removed)):
            if items:
                lines.append(f"- {label}: " + ", ".join(f"`{i}`" for i in items))
        if self.agents_drifted:
            lines.append("- launchd agents: "
                         + ", ".join(f"`{a}`" for a in self.agents_drifted))
        return "\n".join(lines)


def _launchctl_probe(label: str) -> tuple[int, str]:
    """Read one agent's state. Never raises: launchctl being absent is
    itself information the report should carry."""
    try:
        r = _spawn(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                   timeout=20)
    except Exception as exc:  # missing binary, timeout, sandboxed exec
        return 127, f"launchctl unavailable: {exc}"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class Sentinel:
    """A before/after witness for everything the RC must not damage.

    It manifests the real ``~/.email-mcp`` (sha256 + mode per file,
    symlinks recorded but never followed) and the state of the prod
    launchd labels. ``capture`` refuses — loudly — if any of that is
    unreadable: the whole point is to be able to say afterwards what
    changed, and a Sentinel with a hole in it can't.
    """

    def __init__(self, root: Path, *, labels: Sequence[str] = PROD_LABELS,
                 probe: Callable[[str], tuple[int, str]] = _launchctl_probe,
                 max_hash_bytes: int = 64 * 1024 * 1024):
        self.root = Path(root)
        self.labels = tuple(labels)
        self.probe = probe
        self.max_hash_bytes = max_hash_bytes

    # -- capture ------------------------------------------------------ #

    def capture(self) -> Snapshot:
        return Snapshot(_now(), str(self.root), self._manifest(),
                        {label: self._agent(label) for label in self.labels})

    def _manifest(self) -> dict[str, str]:
        if not self.root.exists():
            raise SentinelError(
                f"{self.root} does not exist — the Sentinel cannot witness a "
                "state tree that is not there. Run `email-mcp setup` first, or "
                "point --state-root at the real install.")
        if not self.root.is_dir():
            raise SentinelError(f"{self.root} is not a directory")
        out: dict[str, str] = {}
        self._walk(self.root, "", out)
        return out

    def _walk(self, directory: Path, prefix: str, out: dict[str, str]) -> None:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise SentinelError(
                f"cannot read {directory}: {exc}. The Sentinel refuses to "
                "proceed on a state tree it can only see part of.") from exc
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            path = Path(entry.path)
            if entry.is_symlink():
                # Recorded, never followed — same discipline as
                # config.audit_dir: a link is a fact about the tree, not
                # an invitation to manifest someone else's directory.
                out[rel] = f"link:{os.readlink(path)}"
            elif entry.is_dir():
                out[rel] = f"dir:{self._mode(path):04o}"
                self._walk(path, f"{rel}/", out)
            else:
                out[rel] = self._file_mark(path)

    def _mode(self, path: Path) -> int:
        try:
            return path.stat().st_mode & 0o7777
        except OSError as exc:
            raise SentinelError(f"cannot stat {path}: {exc}") from exc

    def _file_mark(self, path: Path) -> str:
        try:
            st = path.stat()
        except OSError as exc:
            raise SentinelError(f"cannot stat {path}: {exc}") from exc
        mode = st.st_mode & 0o7777
        if st.st_size > self.max_hash_bytes:
            # A multi-GB FTS db is witnessed by size+mtime, not by
            # re-reading it on every run.
            return f"stat:{mode:04o}:{st.st_size}:{st.st_mtime_ns}"
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as exc:
            raise SentinelError(
                f"cannot read {path}: {exc}. The Sentinel refuses to proceed "
                "without a complete manifest of the real state.") from exc
        return f"sha256:{mode:04o}:{st.st_size}:{h.hexdigest()}"

    def _agent(self, label: str) -> dict:
        rc, text = self.probe(label)
        plist = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("path = "):
                plist = stripped[len("path = "):]
                break
        kept = [ln for ln in text.splitlines()
                if not any(v in ln for v in _VOLATILE)]
        digest = hashlib.sha256("\n".join(kept).encode()).hexdigest()
        return {"present": rc == 0, "plist": plist, "digest": digest}

    # -- verify ------------------------------------------------------- #

    def verify(self, baseline: Snapshot, *, strict: bool = False) -> SentinelDiff:
        now = self.capture()
        diff = SentinelDiff()
        for rel, mark in now.files.items():
            was = baseline.files.get(rel)
            if was is None:
                (diff.expected if self._expected(rel, strict, added=True)
                 else diff.added).append(rel)
            elif was != mark:
                (diff.expected if self._expected(rel, strict, added=True)
                 else diff.changed).append(rel)
        for rel in baseline.files:
            if rel not in now.files:
                (diff.expected if self._expected(rel, strict, added=False)
                 else diff.removed).append(rel)
        for label, before in baseline.agents.items():
            after = now.agents.get(label)
            if after is None:
                diff.agents_drifted.append(f"{label} (not re-read)")
            elif (before["present"] != after["present"]
                    or before["plist"] != after["plist"]):
                diff.agents_drifted.append(label)
            elif before["digest"] != after["digest"]:
                diff.agents_noted.append(label)
        return diff

    def _expected(self, rel: str, strict: bool, *, added: bool) -> bool:
        if strict:
            return False
        globs = EXPECTED_CHANGE if added else EXPECTED_REMOVAL
        return any(fnmatch.fnmatch(rel, g) for g in globs)


# --------------------------------------------------------------------- #
# Report                                                                 #
# --------------------------------------------------------------------- #


class Report:
    """The run's markdown log.

    LIVE runs append to ``docs/rc-report-<date>.md`` as they go, so a
    crash keeps the evidence it earned. Dry runs render to the sink and
    write NOTHING: the plan is the product, not a file.
    """

    def __init__(self, path: Path | None, *, live: bool, sink=None):
        self.path = Path(path) if path else None
        self.live = live
        self.sink = sink if sink is not None else sys.stdout
        self.blocks: list[str] = []
        if self.live and self.path is None:
            raise ValueError("a live report needs a path")

    def emit(self, markdown: str) -> None:
        block = markdown.rstrip() + "\n\n"
        self.blocks.append(block)
        self.sink.write(block)
        if self.live:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            first = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                if first:
                    fh.write(f"# v1.0-rc report — "
                             f"{datetime.now().strftime('%Y-%m-%d')}\n\n")
                fh.write(block)

    @property
    def text(self) -> str:
        return "".join(self.blocks)

    def open_run(self, *, run_id: str, live: bool, lanes: str,
                 phases: Sequence[PhaseSpec], resumed: bool,
                 sentinel_root: Path, files: int) -> None:
        head = "resumed" if resumed else "started"
        self.emit(
            f"## Run `{run_id}` — {head} {_now()}\n\n"
            f"- mode: **{'LIVE' if live else 'DRY-RUN (no effects)'}**\n"
            f"- lanes: {lanes}\n"
            f"- phases this pass: {', '.join(p.id for p in phases) or 'none'}\n"
            f"- sentinel: {files} path(s) under `{sentinel_root}`, "
            f"{len(PROD_LABELS)} launchd label(s) witnessed")

    def phase(self, result: "PhaseResult") -> None:
        mark = {PASS: "PASS", FAIL: "**FAIL**", SKIPPED: "skipped",
                DRY: "dry-run", PENDING: "**MANUAL — PENDING**",
                UNIMPLEMENTED: "**unimplemented**"}.get(result.status,
                                                        result.status)
        tag = " [MANUAL]" if result.spec.manual else ""
        lines = [f"### {result.spec.id} · {result.spec.title} · "
                 f"{result.spec.lane}{tag} — {mark} ({result.duration:.1f}s)",
                 "",
                 f"- [{'x' if result.status == PASS else ' '}] "
                 f"{result.spec.acceptance}"]
        lines += [f"- {line}" for line in result.detail]
        self.emit("\n".join(lines))

    def close_run(self, *, results: Sequence["PhaseResult"], diff: SentinelDiff | None,
                  verdict: str) -> None:
        tally: dict[str, int] = {}
        for r in results:
            tally[r.status] = tally.get(r.status, 0) + 1
        counts = " · ".join(f"{n} {s}" for s, n in sorted(tally.items()))
        body = [f"### Sentinel — after the pass\n\n"
                f"{diff.render() if diff else 'not evaluated (dry run)'}",
                f"### Verdict\n\n{counts or 'nothing ran'} → **{verdict}**"]
        self.emit("\n\n".join(body))


# --------------------------------------------------------------------- #
# Context — the only sanctioned way a phase touches the world            #
# --------------------------------------------------------------------- #


@dataclass
class Ran:
    argv: tuple[str, ...]
    rc: int
    out: str
    err: str
    dry: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class PhaseResult:
    spec: PhaseSpec
    status: str
    detail: list[str] = field(default_factory=list)
    started_at: str = ""
    duration: float = 0.0

    def as_dict(self) -> dict:
        return {"id": self.spec.id, "status": self.status,
                "detail": self.detail, "started_at": self.started_at,
                "duration": round(self.duration, 3)}


class Context:
    """What a phase body is handed.

    Every effect goes through here — ``sh`` for processes, ``write`` for
    files, ``manual`` for a human — because that is what makes the two
    hard promises checkable: a dry run does nothing, and a sandbox-lane
    phase cannot reach the real state root even by accident.
    """

    def __init__(self, *, lane: str, dry_run: bool, repo_root: Path,
                 sandbox_home: Path, real_home: Path, state_dir: Path,
                 sentinel: Sentinel, report: Report,
                 mail_dir: Path | None = None,
                 answer: Callable[[str], str] | None = None):
        self.lane = lane
        self.dry_run = dry_run
        self.repo_root = Path(repo_root)
        self.sandbox_home = Path(sandbox_home)
        self.real_home = Path(real_home)
        self.state_dir = Path(state_dir)
        self.sentinel = sentinel
        self.report = report
        self.mail_dir = Path(mail_dir) if mail_dir else None
        self.answer = answer
        self.intents: list[str] = []
        self.result: PhaseResult | None = None
        roots = [self.sandbox_home, self.state_dir,
                 Path(tempfile.gettempdir())]
        if report.path is not None:
            roots.append(report.path.parent)
        self.write_roots = tuple(self._resolve(r) for r in roots)

    # -- evidence ----------------------------------------------------- #

    def note(self, text: str) -> None:
        if self.result is not None:
            self.result.detail.append(text)

    def require(self, condition, why: str) -> None:
        """The acceptance criterion, inline. A false condition ends the
        phase with a recorded reason, never a bare traceback."""
        if not condition:
            raise PhaseFailure(why)

    # -- processes ---------------------------------------------------- #

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The lane's environment. Sandbox gets a fake HOME and the real
        Mail store attached read-only; every inherited EMAIL_MCP_* is
        wiped first so the operator's shell cannot leak a prod path into
        the sandbox."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("EMAIL_MCP_")}
        if self.lane == SANDBOX:
            env["HOME"] = str(self.sandbox_home)
            if self.mail_dir is not None:
                env["EMAIL_MCP_MAIL_DIR"] = str(self.mail_dir)
        env.update(extra or {})
        self._fence_env(env)
        return env

    def _fence_env(self, env: dict[str, str]) -> None:
        if self.lane != SANDBOX:
            return
        guarded = self._resolve(self.sentinel.root)
        if self._resolve(Path(env.get("HOME", "/"))) == self._resolve(self.real_home):
            raise UnsafeAction("sandbox lane refuses to run with the real HOME")
        for key, value in env.items():
            if not key.startswith("EMAIL_MCP_") or not value:
                continue
            if self._within(self._resolve(Path(value)), guarded):
                raise UnsafeAction(
                    f"{key} points into the real state root ({value}) — the "
                    "sandbox lane may not operate the prod estate")

    def sh(self, argv: Sequence[str], *, timeout: float | None = 300,
           cwd: Path | None = None, extra_env: dict[str, str] | None = None,
           stdin_text: str | None = None, check: bool = False) -> Ran:
        argv = tuple(str(a) for a in argv)
        self._fence_cmd(argv)
        if self.dry_run:
            self.intents.append("run: " + " ".join(argv))
            self.note(f"would run `{' '.join(argv)}`")
            return Ran(argv, 0, "", "", dry=True)
        proc = _spawn(argv, cwd=cwd, env=self.env(extra_env), timeout=timeout,
                      stdin_text=stdin_text)
        ran = Ran(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and not ran.ok:
            raise PhaseFailure(
                f"`{' '.join(argv)}` exited {ran.rc}: "
                f"{(ran.err or ran.out).strip()[:200]}")
        return ran

    def _fence_cmd(self, argv: tuple[str, ...]) -> None:
        if self.lane != SANDBOX:
            return
        guarded = str(self._resolve(self.sentinel.root))
        for arg in argv:
            # A hit must end on a path boundary: the runner's own
            # ~/.email-mcp-rc (journals, the default sandbox home) shares
            # the root's spelling as a prefix but is a sibling, not the
            # estate — the first bound body found the naive substring
            # match refusing its own venv path.
            idx = arg.find(guarded)
            while idx != -1:
                end = idx + len(guarded)
                if end == len(arg) or arg[end] == "/":
                    raise UnsafeAction(
                        f"sandbox-lane command names the real state root: "
                        f"{arg}")
                idx = arg.find(guarded, idx + 1)

    # -- files -------------------------------------------------------- #

    def write(self, path: Path, text: str, *, mode: int = 0o600) -> Path:
        """Write a scratch/fixture file. Refused outside the run's own
        roots, and refused inside the real state root under any
        circumstances — including a state dir someone mis-pointed."""
        path = Path(path)
        target = self._resolve(path)
        guarded = self._resolve(self.sentinel.root)
        if self._within(target, guarded):
            raise UnsafeAction(
                f"refusing to write inside the real state root: {path}")
        if not any(self._within(target, root) for root in self.write_roots):
            raise UnsafeAction(
                f"{path} is outside the run's write roots "
                f"({', '.join(str(r) for r in self.write_roots)})")
        if self.dry_run:
            self.intents.append(f"write: {path}")
            self.note(f"would write `{path}` ({len(text)} bytes)")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    @staticmethod
    def _resolve(path: Path) -> Path:
        # strict=False: a not-yet-created scratch path still resolves.
        return Path(os.path.abspath(os.path.expanduser(str(path))))

    @staticmethod
    def _within(child: Path, parent: Path) -> bool:
        return child == parent or parent in child.parents

    # -- humans ------------------------------------------------------- #

    def manual(self, spec: PhaseSpec) -> tuple[str, str]:
        """The manual-step protocol.

        Print what the operator must do, then take a verdict. With no
        operator attached (the unattended default) the step is recorded
        PENDING and the run continues — an unattended pass is never
        invented, and a pending step keeps the whole run INCOMPLETE.
        """
        prompt = (f"MANUAL {spec.id} — {spec.title} ({spec.lane} lane)\n"
                  f"Acceptance: {spec.acceptance}\n"
                  f"Answer pass / fail / skip, optionally 'verdict: evidence'")
        if self.answer is None or self.dry_run:
            self.note("no operator attached — recorded PENDING; re-run with "
                      "--interactive and this phase selected")
            return PENDING, ""
        for _ in range(3):
            raw = (self.answer(prompt) or "").strip()
            verdict, _, evidence = raw.partition(":")
            got = verdict.strip().lower()
            if got in ("pass", "ok", "yes"):
                return PASS, evidence.strip()
            if got in ("fail", "no"):
                return FAIL, evidence.strip()
            if got in ("skip", "skipped"):
                return SKIPPED, evidence.strip()
        self.note("operator gave no usable verdict after 3 attempts")
        return PENDING, ""


# --------------------------------------------------------------------- #
# RunState — the resume journal                                          #
# --------------------------------------------------------------------- #


class RunState:
    """One journal file per run, flushed around every phase.

    Written BEFORE a phase starts (status RUNNING) and again when it
    settles, so a `kill -9` mid-phase — which is a thing this RC does on
    purpose — leaves a journal that says exactly which phase was in
    flight. RUNNING is not settled, so --resume re-runs it.
    """

    def __init__(self, path: Path, *, run_id: str, report_path: str,
                 live: bool, lanes: str, persist: bool = True):
        self.path = Path(path)
        self.run_id = run_id
        self.report_path = report_path
        self.live = live
        self.lanes = lanes
        # A dry run journals nothing: there is no progress to resume from
        # a pass that did not happen.
        self.persist = persist
        self.started_at = _now()
        self.results: dict[str, dict] = {}
        self.baseline: dict | None = None

    # -- persistence -------------------------------------------------- #

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "started_at": self.started_at,
                "report_path": self.report_path, "live": self.live,
                "lanes": self.lanes, "results": self.results,
                "baseline": self.baseline}

    def save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)  # atomic: a crash never truncates a journal

    @classmethod
    def load(cls, path: Path) -> "RunState":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        state = cls(Path(path), run_id=raw["run_id"],
                    report_path=raw.get("report_path", ""),
                    live=raw.get("live", False), lanes=raw.get("lanes", ""))
        state.started_at = raw.get("started_at", "")
        state.results = raw.get("results", {})
        state.baseline = raw.get("baseline")
        return state

    @classmethod
    def latest(cls, state_dir: Path) -> "RunState | None":
        journals = sorted(Path(state_dir).glob("rc-*.json"))
        return cls.load(journals[-1]) if journals else None

    # -- transitions -------------------------------------------------- #

    def begin(self, spec: PhaseSpec) -> None:
        self.results[spec.id] = {"id": spec.id, "status": RUNNING,
                                 "detail": [], "started_at": _now(),
                                 "duration": 0.0}
        self.save()

    def finish(self, result: PhaseResult) -> None:
        self.results[result.spec.id] = result.as_dict()
        self.save()

    def settled_ids(self) -> set[str]:
        return {pid for pid, r in self.results.items()
                if r.get("status") in SETTLED}


def _once_done(state_dir: Path, phase_id: str) -> bool:
    """True when a `once` phase has passed in ANY recorded run — the
    fresh-account walk is a one-time proof, not a per-run chore."""
    for journal in sorted(Path(state_dir).glob("rc-*.json")):
        try:
            raw = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if raw.get("results", {}).get(phase_id, {}).get("status") == PASS:
            return True
    return False


# --------------------------------------------------------------------- #
# selection + execution                                                  #
# --------------------------------------------------------------------- #


def parse_phase_selector(raw: str) -> list[str]:
    """`P03`, `P03,P05`, `P05-P08` → explicit ids, order preserved from
    the plan so a selector can never reorder the life story."""
    wanted: set[str] = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if "-" in token:
            lo, hi = (t.strip() for t in token.split("-", 1))
            if lo not in PLAN_BY_ID or hi not in PLAN_BY_ID:
                raise ValueError(f"unknown phase range {token}")
            lo_i, hi_i = int(lo[1:]), int(hi[1:])
            wanted |= {s.id for s in PLAN if lo_i <= int(s.id[1:]) <= hi_i}
        else:
            if token not in PLAN_BY_ID:
                raise ValueError(f"unknown phase {token}")
            wanted.add(token)
    return [s.id for s in PLAN if s.id in wanted]


def select(plan: Sequence[PhaseSpec], *, lane: str, selector: str | None,
           skip: Iterable[str] = (), state_dir: Path | None = None
           ) -> list[PhaseSpec]:
    chosen = list(plan)
    if lane != BOTH:
        chosen = [s for s in chosen if s.lane in (lane, BOTH)]
    if selector:
        wanted = set(parse_phase_selector(selector))
        chosen = [s for s in chosen if s.id in wanted]
    skip = set(skip)
    chosen = [s for s in chosen if s.id not in skip]
    if state_dir is not None:
        chosen = [s for s in chosen
                  if not (s.once and _once_done(state_dir, s.id))]
    return chosen


def execute(spec: PhaseSpec, ctx: Context,
            implementations: dict[str, Callable[[Context], None]]
            ) -> PhaseResult:
    """Run one phase. Returns a result for every ordinary outcome and
    lets only BaseException through — a KeyboardInterrupt or a real
    process kill must not be recorded as a tidy failure."""
    result = PhaseResult(spec, RUNNING, started_at=_now())
    ctx.result = result
    ctx.lane = spec.lane if spec.lane != BOTH else ctx.lane
    started = time.monotonic()
    try:
        if spec.manual:
            status, evidence = ctx.manual(spec)
            if evidence:
                result.detail.append(f"operator: {evidence}")
            result.status = status
        else:
            fn = implementations.get(spec.id)
            if fn is None:
                result.detail.append(
                    "no body bound yet — see docs/w3-rc-plan.md")
                result.status = UNIMPLEMENTED
            else:
                fn(ctx)
                result.status = DRY if ctx.dry_run else PASS
    except PhaseFailure as exc:
        result.detail.append(f"acceptance not met: {exc}")
        result.status = FAIL
    except UnsafeAction as exc:
        result.detail.append(f"refused: {exc}")
        result.status = FAIL
    except Exception as exc:  # a phase body's own bug is a phase failure
        result.detail.append(f"{type(exc).__name__}: {exc}")
        result.status = FAIL
    finally:
        result.duration = time.monotonic() - started
        ctx.result = None
    return result


def run(specs: Sequence[PhaseSpec], ctx: Context, state: RunState,
        baseline: Snapshot | None, *,
        implementations: dict[str, Callable[[Context], None]] | None = None,
        keep_going: bool = False) -> tuple[list[PhaseResult], SentinelDiff | None]:
    impls = IMPLEMENTATIONS if implementations is None else implementations
    results: list[PhaseResult] = []
    lane_at_entry = ctx.lane
    for spec in specs:
        state.begin(spec)
        ctx.lane = lane_at_entry
        result = execute(spec, ctx, impls)
        if (spec.sentinel_strict and baseline is not None
                and result.status in SETTLED and not ctx.dry_run):
            # The phase whose whole claim is "the real estate is
            # untouched" gets its claim checked immediately, with zero
            # expected churn allowed.
            strict = ctx.sentinel.verify(baseline, strict=True)
            if not strict.clean:
                result.status = FAIL
                result.detail.append("strict sentinel: " + strict.render())
        state.finish(result)
        ctx.report.phase(result)
        results.append(result)
        if result.status == FAIL and not keep_going:
            break
        if result.status == UNIMPLEMENTED and not ctx.dry_run:
            # Live: an unbound phase is a hole in the life story, stop.
            # Dry: printing the whole plan IS the point, keep walking.
            break
    ctx.lane = lane_at_entry
    diff = None
    if baseline is not None and not ctx.dry_run:
        diff = ctx.sentinel.verify(baseline)
    return results, diff


def verdict_for(results: Sequence[PhaseResult], diff: SentinelDiff | None,
                dry_run: bool) -> tuple[str, int]:
    if diff is not None and not diff.clean:
        return "SENTINEL DRIFT — investigate before continuing", EXIT_SENTINEL_DRIFT
    if any(r.status == FAIL for r in results):
        return "FAILED", EXIT_PHASE_FAILED
    if dry_run:
        unbound = sum(1 for r in results if r.status == UNIMPLEMENTED)
        return (f"DRY RUN — plan only, nothing executed "
                f"({unbound} phase(s) with no body yet)"), EXIT_OK
    if any(r.status == UNIMPLEMENTED for r in results):
        return "BLOCKED — unimplemented phase", EXIT_PHASE_FAILED
    if any(r.status == PENDING for r in results):
        return "INCOMPLETE — manual steps outstanding", EXIT_INCOMPLETE
    return "PASSED", EXIT_OK


# --------------------------------------------------------------------- #
# soak report                                                            #
# --------------------------------------------------------------------- #


def soak_report(state_dir: Path) -> str:
    """Aggregate every journal in the state dir: the RC's claim is that
    the life story survives *repetition*, so the interesting number is
    per-phase pass rate across runs, not any single run."""
    journals = sorted(Path(state_dir).glob("rc-*.json"))
    runs: list[dict] = []
    for journal in journals:
        try:
            runs.append(json.loads(journal.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    lines = [f"## Soak report — {len(runs)} run(s) under `{state_dir}`", ""]
    if not runs:
        return "\n".join(lines + ["No journals found."])
    lines += ["| Phase | Title | pass | fail | other | last |",
              "|---|---|---|---|---|---|"]
    for spec in PLAN:
        seen = [r.get("results", {}).get(spec.id) for r in runs]
        seen = [s for s in seen if s]
        if not seen:
            continue
        npass = sum(1 for s in seen if s.get("status") == PASS)
        nfail = sum(1 for s in seen if s.get("status") == FAIL)
        other = len(seen) - npass - nfail
        lines.append(f"| {spec.id} | {spec.title} | {npass} | {nfail} | "
                     f"{other} | {seen[-1].get('status')} |")
    lines += ["", "Runs:"]
    for raw in runs:
        settled = sum(1 for r in raw.get("results", {}).values()
                      if r.get("status") in SETTLED)
        lines.append(f"- `{raw.get('run_id')}` {raw.get('started_at')} "
                     f"({'live' if raw.get('live') else 'dry'}) — "
                     f"{settled}/{len(raw.get('results', {}))} settled")
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# phase bodies — R2, stage S1: the sandbox core (P01–P05)                #
# --------------------------------------------------------------------- #
#
# Every body follows one shape: issue EVERY effect through ctx first (so
# a dry run prints the complete plan of commands), then `return` on dry,
# then assert the phase's acceptance criterion with ctx.require and leave
# the evidence in ctx.note. Bodies never import email_mcp — the wheel
# P01 installs is the thing under test, and it is driven by subprocess.


def _venv_bin(ctx: Context, name: str) -> Path:
    """The sandbox install P01 lays down and every later sandbox phase
    drives. One spelling on purpose: if P02+ guessed a different path,
    they would certify a different binary than the one P01 built."""
    return ctx.sandbox_home / "venv" / "bin" / name


@implements("P01")
def _p01_wheel_install(ctx: Context) -> None:
    venv = ctx.sandbox_home / "venv"
    dist = ctx.sandbox_home / "dist"
    ctx.sh([sys.executable, "-m", "venv", venv], timeout=300, check=True)
    pip = venv / "bin" / "pip"
    built = ctx.sh([pip, "wheel", "--no-deps", "-w", dist, ctx.repo_root],
                   timeout=900, check=True)
    if built.dry:
        ctx.sh([pip, "install", dist / "email_mcp-<built>.whl"])
        ctx.sh([_venv_bin(ctx, "email-mcp"), "version"])
        return
    wheels = sorted(dist.glob("email_mcp-*.whl"))
    ctx.require(len(wheels) == 1,
                f"expected exactly one built wheel under {dist}, "
                f"found {len(wheels)}")
    wheel = wheels[0]
    expected = wheel.name.split("-")[1]
    ctx.sh([pip, "install", wheel], timeout=900, check=True)
    # cwd far from the checkout: the reported version must come from the
    # installed wheel, unreachable by a repo-tree import.
    ran = ctx.sh([_venv_bin(ctx, "email-mcp"), "version"],
                 cwd=ctx.sandbox_home, timeout=120, check=True)
    reported = ran.out.strip()
    ctx.require(reported == expected,
                f"`email-mcp version` reports {reported!r} but the wheel "
                f"is {expected!r} — the wrong bytes answered")
    ctx.note(f"wheel {wheel.name} installed into the sandbox; "
             f"`email-mcp version` → {reported}")


@implements("P02")
def _p02_scripted_setup(ctx: Context) -> None:
    # The fixture IS the wizard contract: prompts documented, answers fed
    # one per stdin line in the order lifecycle.setup asks them. A stale
    # fixture misfeeds every later answer, so the suite pins each prompt
    # to the shipped wizard (test_rc_runner.py).
    fixture = ctx.repo_root / "tests" / "fixtures" / "setup_answers.json"
    answers = json.loads(fixture.read_text(encoding="utf-8"))["answers"]
    ran = ctx.sh([_venv_bin(ctx, "email-mcp"), "setup"],
                 stdin_text="".join(a["answer"] + "\n" for a in answers),
                 timeout=300)
    if ran.dry:
        return
    ctx.require(ran.ok, f"setup exited {ran.rc}: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    root = ctx.sandbox_home / STATE_ROOT_NAME
    loose = [str(d) for d in [root, *root.rglob("*")]
             if d.is_dir() and d.stat().st_mode & 0o777 != 0o700]
    ctx.require(not loose, "tree not 0700: " + ", ".join(loose))
    ident = root / "identities.toml"
    ctx.require(ident.is_file() and ident.stat().st_mode & 0o777 == 0o600,
                "identities.toml is missing or not 0600")
    # No secret VALUE anywhere: the tree may name secret references
    # (a Keychain item), never hold one.
    leaky = [str(f) for f in root.rglob("*") if f.is_file()
             and "password" in f.read_text(encoding="utf-8",
                                           errors="replace").lower()]
    ctx.require(not leaky, "a secret value reached the tree: "
                           + ", ".join(leaky))
    ctx.require(not (root / "fts" / "fts.db").exists(),
                "setup built the FTS index (the fixture defers it)")
    command = next((line.split('"')[3] for line in ran.out.splitlines()
                    if '"command"' in line), "")
    ctx.require(command.startswith("/"),
                f"printed MCP entry point is not absolute: {command!r}")
    ctx.note(f"{len(answers)} scripted answers consumed; 0700 tree, "
             f"0600 identities.toml, FTS unbuilt; MCP entry `{command}`")


@implements("P03")
def _p03_doctor(ctx: Context) -> None:
    email_mcp = _venv_bin(ctx, "email-mcp")
    # A BOTH phase runs its own comparison: the same verb on each lane,
    # each with that lane's expectation (plan §1) — so the body sets the
    # lane per half; run() restores it afterwards.
    ctx.lane = SANDBOX
    sandbox = ctx.sh([email_mcp, "doctor"], timeout=300)
    ctx.lane = PROD
    prod = ctx.sh([email_mcp, "doctor"], timeout=300)
    # The JSON report backs the per-failure fix check; --doctor exits
    # non-zero on a red estate by design, so its rc proves nothing here.
    prod_json = ctx.sh([email_mcp, "--doctor"], timeout=300)
    if ctx.dry_run:
        return
    fails = "; ".join(line for line in sandbox.out.splitlines()
                      if line.startswith("FAIL"))
    ctx.require(sandbox.ok, "sandbox doctor is not green: "
                            + (fails or (sandbox.err or sandbox.out)
                               .strip()[:200]))
    ctx.note("sandbox doctor: every check green")
    if prod.ok:
        ctx.note("prod doctor: the real estate is healthy")
        return
    report = json.loads(prod_json.out)
    red = {name: c
           for name, c in {**report["checks"], "audit": report["audit"]}
           .items() if not c["ok"]}
    unfixed = sorted(name for name, c in red.items() if not c.get("fix"))
    ctx.require(not unfixed, "prod doctor failure(s) name no fix: "
                             + ", ".join(unfixed))
    if not red:
        # Red exit with green checks: the redness came from the checks
        # registry, whose findings print with the one repair command.
        ctx.require("doctor --fix" in prod.out,
                    "prod doctor findings name no fix")
    for name, c in sorted(red.items()):
        ctx.note(f"prod {name}: {c['detail']} — fix: {c['fix']}")
    ctx.note("prod doctor names a concrete fix for every failure")


# One indexed document must round-trip through a MATCH query — the
# definition of "searchable" that a row count cannot fake. Runs under the
# installed wheel's interpreter so the query goes through the shipped
# read seam (rowids_matching), not a re-implementation.
_FTS_PROBE = '''\
"""P04 probe: prove one indexed document comes back out of a MATCH."""
import json
import re

from email_mcp import fts

idx = fts.FtsIndex()
out = {"rowid": None, "token": None, "hits": []}
conn = idx._open_ro()
try:
    for row in conn.execute("SELECT rowid, body FROM body_fts LIMIT 25"):
        tokens = re.findall(r"[A-Za-z0-9]{3,}", row["body"] or "")
        if tokens:
            out = {"rowid": row["rowid"], "token": tokens[0],
                   "hits": idx.rowids_matching(tokens[0])}
            break
finally:
    conn.close()
print(json.dumps(out))
'''


@implements("P04")
def _p04_index(ctx: Context) -> None:
    email_mcp = _venv_bin(ctx, "email-mcp")
    # Sandbox half: a bounded build (fullness is prod's claim, not this
    # one), then the round-trip probe.
    ctx.lane = SANDBOX
    build = ctx.sh([email_mcp, "fts", "--build", "--limit", "200"],
                   timeout=900)
    probe = ctx.write(ctx.sandbox_home / "rc-p04-probe.py", _FTS_PROBE)
    searched = ctx.sh([_venv_bin(ctx, "python"), probe], timeout=120)
    # Prod half: the operator's real index, read-only.
    ctx.lane = PROD
    status = ctx.sh([email_mcp, "fts", "--status", "--json"], timeout=120)
    if ctx.dry_run:
        return
    ctx.require(build.ok, f"sandbox fts --build exited {build.rc}: "
                          f"{(build.err or build.out).strip()[:200]}")
    ctx.require(searched.ok, f"fts probe exited {searched.rc}: "
                             f"{(searched.err or searched.out).strip()[:200]}")
    hit = json.loads(searched.out)
    ctx.require(hit["rowid"] is not None,
                "the sandbox index holds no indexed documents")
    ctx.require(hit["rowid"] in hit["hits"],
                f"indexed doc {hit['rowid']} not found by MATCH "
                f"{hit['token']!r} — built but not searchable")
    ctx.note(f"sandbox: built with --limit 200; MATCH {hit['token']!r} "
             f"returned rowid {hit['rowid']} among {len(hit['hits'])} hit(s)")
    st = json.loads(status.out)
    ctx.require(st.get("state") == "ready",
                f"prod index state is {st.get('state')!r}, not ready")
    docs = st.get("docs", {})
    ctx.require(docs.get("indexed", 0) > 0,
                "prod index holds no indexed documents")
    ctx.require(docs.get("missing", 0) == 0 and docs.get("error", 0) == 0,
                f"prod index is not full: {docs}")
    freshest = max(filter(None, (st.get("built_at"), st.get("last_sync_at"),
                                 st.get("last_reconcile_at"))), default=None)
    ctx.require(freshest is not None,
                "prod index carries no build/sync timestamp")
    # Two nightly agent cadences: one missed 03:30 sync is tolerated,
    # a habitually-unsynced index is stale — not the number the operator
    # would actually get.
    age = datetime.now(timezone.utc) - datetime.fromisoformat(freshest)
    ctx.require(age.total_seconds() < 48 * 3600,
                f"prod index is stale: last touched {freshest}")
    ctx.note(f"prod: ready, {docs.get('indexed')} indexed docs, "
             f"freshest stamp {freshest}")


# The test_mcp_wire.py discipline as a standalone program: newline-
# delimited JSON-RPC on real pipes, every stdout byte kept, each response
# awaited before the next send. It prints ONE JSON report; judging the
# frames is the phase's job, not this client's — which is why a corrupt
# line is kept as evidence rather than treated as an error here.
_WIRE_CLIENT = '''\
"""P05 wire client: a real MCP session over stdio, bytes preserved."""
import json
import subprocess
import sys

proc = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True)
lines = []


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\\n")
    proc.stdin.flush()


def wait_for(rid):
    while True:
        line = proc.stdout.readline()
        if not line:
            return None  # EOF before the answer — the report will show it
        lines.append(line.rstrip("\\n"))
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # kept in lines; the phase judges purity
        if msg.get("id") == rid:
            return msg


send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "rc-runner-p05", "version": "0"}}})
wait_for(1)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "search_emails", "arguments": {"limit": 5}}})
search = wait_for(2)

target = "rc-p05-no-such-id"
try:
    envelope = json.loads(search["result"]["content"][0]["text"])
    target = (envelope.get("results") or [{}])[0].get("id") or target
except (TypeError, KeyError, ValueError, IndexError):
    pass  # a malformed search answer still deserves the second call

send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
    "name": "get_email", "arguments": {"id": target}}})
wait_for(3)
proc.stdin.close()
for line in proc.stdout:
    lines.append(line.rstrip("\\n"))
print(json.dumps({"lines": lines, "target": target,
                  "server_exit": proc.wait(timeout=30)}))
'''


@implements("P05")
def _p05_wire_search_read(ctx: Context) -> None:
    client = ctx.write(ctx.sandbox_home / "rc-p05-client.py", _WIRE_CLIENT)
    ran = ctx.sh([_venv_bin(ctx, "python"), client], timeout=300)
    if ran.dry:
        return
    ctx.require(ran.ok, f"wire client exited {ran.rc}: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    report = json.loads(ran.out)
    ctx.require("Traceback" not in "\n".join(report["lines"]),
                "a traceback crossed the wire")
    by_id: dict = {}
    for line in report["lines"]:
        try:
            msg = json.loads(line)
        except ValueError:
            raise PhaseFailure(
                f"non-JSON-RPC bytes on the wire: {line[:120]!r}") from None
        ctx.require(msg.get("jsonrpc") == "2.0",
                    f"non-2.0 frame on the wire: {line[:120]!r}")
        if "id" in msg:
            by_id[msg["id"]] = msg
    server = (by_id.get(1, {}).get("result", {})
              .get("serverInfo", {}).get("name"))
    ctx.require(server == "apple-mail",
                f"initialize answered {server!r}, not the shipped server")
    envelopes: dict[int, dict] = {}
    for rid, tool in ((2, "search_emails"), (3, "get_email")):
        msg = by_id.get(rid)
        ctx.require(msg is not None, f"{tool}: no response on the wire")
        ctx.require("error" not in msg,
                    f"{tool}: a JSON-RPC error crossed the wire")
        result = msg.get("result") or {}
        ctx.require(result.get("isError") is not True,
                    f"{tool}: isError — an exception reached the wire")
        content = result.get("content") or [{}]
        ctx.require(content[0].get("type") == "text",
                    f"{tool}: no text content in the result")
        envelope = json.loads(content[0]["text"])
        ctx.require(isinstance(envelope.get("ok"), bool),
                    f"{tool}: no ok key — not a contract envelope")
        envelopes[rid] = envelope
    search_env, read_env = envelopes[2], envelopes[3]
    ctx.require(search_env["ok"] is True,
                f"search_emails failed on the wire: "
                f"{search_env.get('code')} {search_env.get('error')}")
    if search_env.get("results"):
        ctx.require(read_env["ok"] is True,
                    f"get_email failed for a real id {report['target']!r}: "
                    f"{read_env.get('code')} {read_env.get('error')}")
    else:
        # An empty store is still a wire claim: the miss must come back
        # as a coded envelope, never as an exception.
        ctx.require(read_env["ok"] is False and read_env.get("code"),
                    "get_email on an empty store must yield a coded "
                    "envelope")
    ctx.require(report["server_exit"] == 0,
                f"server exited {report['server_exit']} after the session")
    ctx.note(f"{len(report['lines'])} clean JSON-RPC frame(s); "
             f"search fts state {search_env.get('fts', {}).get('state')!r}; "
             f"get_email target {report['target']!r}")


# --------------------------------------------------------------------- #
# CLI                                                                    #
# --------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rc_runner",
        description="Drive the v1.0-rc programme (docs/w3-rc-plan.md).",
        epilog="Dry-run is the default: without --execute nothing is spawned, "
               "written, sent or bootstrapped.")
    p.add_argument("--execute", action="store_true",
                   help="actually perform the phases (default: dry-run)")
    p.add_argument("--dry-run", action="store_true",
                   help="explicit no-op default; refuses to combine with "
                        "--execute")
    p.add_argument("--lane", choices=(SANDBOX, PROD, BOTH), default=BOTH)
    p.add_argument("--phase", default=None,
                   help="P03 | P03,P07 | P05-P09")
    p.add_argument("--resume", action="store_true",
                   help="continue the newest journal, skipping settled phases")
    p.add_argument("--interactive", action="store_true",
                   help="prompt on MANUAL phases (default: record PENDING)")
    p.add_argument("--keep-going", action="store_true",
                   help="do not stop the pass at the first failure")
    p.add_argument("--list", action="store_true", help="print the plan and exit")
    p.add_argument("--soak-report", action="store_true",
                   help="aggregate every journal in the state dir and exit")
    p.add_argument("--no-sentinel", action="store_true",
                   help="skip the before/after witness — dry runs only")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--state-root", default=None,
                   help="the real state tree the Sentinel witnesses "
                        f"(default ~/{STATE_ROOT_NAME})")
    p.add_argument("--sandbox-home", default=None)
    p.add_argument("--mail-dir", default=None,
                   help="Mail store attached read-only in the sandbox lane")
    p.add_argument("--report", default=None)
    return p


def _plan_table() -> str:
    lines = ["| Phase | Lane | Mode | Title | Acceptance |",
             "|---|---|---|---|---|"]
    for spec in PLAN:
        mode = "MANUAL" if spec.manual else "auto"
        if spec.once:
            mode += ", once"
        bound = "" if spec.manual or spec.id in IMPLEMENTATIONS else " *(no body)*"
        lines.append(f"| {spec.id} | {spec.lane} | {mode} | {spec.title}"
                     f"{bound} | {spec.acceptance} |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *,
         plan: Sequence[PhaseSpec] | None = None,
         implementations: dict[str, Callable[[Context], None]] | None = None,
         answer: Callable[[str], str] | None = None,
         sentinel: Sentinel | None = None,
         sink=None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    out = sink if sink is not None else sys.stdout
    plan = list(plan) if plan is not None else list(PLAN)
    impls = IMPLEMENTATIONS if implementations is None else implementations

    if args.execute and args.dry_run:
        print("--execute and --dry-run are mutually exclusive", file=sys.stderr)
        return EXIT_USAGE
    live = bool(args.execute)          # the opt-in; absence means dry-run
    if args.no_sentinel and live:
        # The Sentinel is the only thing standing between a sandbox
        # launchd action and the operator's real agents.
        print("--no-sentinel is refused with --execute: a live run is "
              "exactly when the witness is needed", file=sys.stderr)
        return EXIT_USAGE

    if args.list:
        out.write(_plan_table() + "\n")
        return EXIT_OK

    real_home = Path.home()
    repo_root = Path(__file__).resolve().parents[1]
    state_dir = Path(args.state_dir).expanduser() if args.state_dir \
        else real_home / RC_DIRNAME
    state_root = Path(args.state_root).expanduser() if args.state_root \
        else real_home / STATE_ROOT_NAME
    if Context._within(Context._resolve(state_dir), Context._resolve(state_root)):
        print(f"--state-dir must live outside {state_root}: the runner's own "
              "journals would otherwise register as drift", file=sys.stderr)
        return EXIT_USAGE

    if args.soak_report:
        out.write(soak_report(state_dir) + "\n")
        return EXIT_OK

    watcher = sentinel if sentinel is not None else Sentinel(state_root)

    resumed_state = RunState.latest(state_dir) if args.resume else None
    if args.resume and resumed_state is None:
        print(f"--resume found no journal under {state_dir}", file=sys.stderr)
        return EXIT_USAGE

    run_id = (resumed_state.run_id if resumed_state
              else datetime.now().strftime("rc-%Y%m%d-%H%M%S"))
    report_path = Path(args.report).expanduser() if args.report else (
        repo_root / "docs" / f"rc-report-{datetime.now():%Y-%m-%d}.md")
    report = Report(report_path if live else None, live=live, sink=out)

    specs = select(plan, lane=args.lane, selector=args.phase,
                   skip=resumed_state.settled_ids() if resumed_state else (),
                   state_dir=state_dir)

    baseline: Snapshot | None = None
    if not args.no_sentinel:
        try:
            baseline = watcher.capture()
        except SentinelError as exc:
            report.emit(f"### Sentinel — REFUSED\n\n{exc}")
            print(f"sentinel refused: {exc}", file=sys.stderr)
            return EXIT_SENTINEL_REFUSED

    state = resumed_state or RunState(
        state_dir / f"{run_id}.json", run_id=run_id,
        report_path=str(report_path) if live else "", live=live,
        lanes=args.lane, persist=live)
    state.persist = live
    if baseline is not None and state.baseline is None:
        state.baseline = baseline.as_dict()
    elif resumed_state is not None and state.baseline is not None:
        # A resumed pass compares against the ORIGINAL baseline: drift a
        # crashed pass caused must not be laundered by re-capturing.
        baseline = Snapshot.from_dict(state.baseline)
    if live:
        state.save()

    ctx = Context(lane=args.lane if args.lane != BOTH else SANDBOX,
                  dry_run=not live, repo_root=repo_root,
                  sandbox_home=Path(args.sandbox_home).expanduser()
                  if args.sandbox_home else state_dir / "sandbox-home",
                  real_home=real_home, state_dir=state_dir, sentinel=watcher,
                  report=report,
                  mail_dir=Path(args.mail_dir).expanduser()
                  if args.mail_dir else None,
                  answer=answer if answer is not None
                  else (_stdin_answer if args.interactive else None))

    report.open_run(run_id=run_id, live=live, lanes=args.lane, phases=specs,
                    resumed=resumed_state is not None,
                    sentinel_root=state_root,
                    files=len(baseline.files) if baseline else 0)
    try:
        results, diff = run(specs, ctx, state, baseline,
                            implementations=impls, keep_going=args.keep_going)
    except SentinelError as exc:
        report.emit(f"### Sentinel — REFUSED mid-run\n\n{exc}")
        return EXIT_SENTINEL_REFUSED
    text, code = verdict_for(results, diff, not live)
    report.close_run(results=results, diff=diff, verdict=text)
    return code


def _stdin_answer(prompt: str) -> str:
    print("\n" + prompt)
    try:
        return input("> ")
    except EOFError:
        return ""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
