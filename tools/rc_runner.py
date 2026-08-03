#!/usr/bin/env python3
"""v1.0-rc runner core — the harness the 19-phase RC programme rides on.

``docs/w3-rc-plan.md`` is the plan this file executes: 19 phases across
two lanes — a *sandbox* lane (a throwaway ``$HOME`` with the real Mail
store attached read-only through ``EMAIL_MCP_MAIL_DIR``) and a *prod*
lane (self-only sends on the real estate). R1 shipped the core:
Context, Report, Sentinel, the phase registry, the
resume/--phase/--dry-run plumbing and the manual-step protocol. Phase
bodies attach through ``@implements`` (S1 bound the sandbox core
P01–P05; S2 binds the mutation story P06–P07 and P10–P12; S3 the prod
lane P08/P09/P14/P17 and the added P19 drafts phase; S4 closes R2 with
the failure matrix P13, the v0.9 upgrade P15, uninstall+purge P16 and
the once-ever P18 walk — every planned phase now has a body).

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
import re
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
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
# the index rebuilds, plans are written and GC'd, a Graph send rotates
# its token cache, and the prod dispatcher appends to its own log when
# the pass hands it work (P08/P09 schedule against the real spool).
# Everything else — identities.toml, meta.json, an unknown new file at
# the root — is material drift.
EXPECTED_CHANGE = ("audit/*", "spool/*", "fts/*", "plans/*",
                   "graph/*.token.json", "dispatcher.log")
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
# the plan: 19 phases (bodies attach via @implements)                    #
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
              "prod `fts --status` is fresh, error-free, and every "
              "un-indexed doc is explained by an absent local body"),
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
    PhaseSpec("P19", "drafts", PROD,
              "create_draft files a base64 draft into the identity's own "
              "Drafts via the Graph lane (three-legged readback); a "
              "lane-less identity refuses draft_unsupported; a human edits, "
              "hand-sends and confirms rendering", manual=True),
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
            # match refusing its own venv path. The scan also runs on the
            # normalized spelling (mirroring _resolve), or a dot-dot
            # re-entry like <root>-rc/../<root> — which the OS resolves
            # to the estate — would sail through the boundary check.
            for spelling in {arg, os.path.normpath(arg)}:
                idx = spelling.find(guarded)
                while idx != -1:
                    end = idx + len(guarded)
                    if end == len(spelling) or spelling[end] == "/":
                        raise UnsafeAction(
                            f"sandbox-lane command names the real state "
                            f"root: {arg}")
                    idx = spelling.find(guarded, idx + 1)

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

    def rm_tree(self, path: Path) -> Path:
        """Remove a scratch tree, behind the SAME fences as write: never
        inside the real state root, never outside the run's own write
        roots, nothing at all in a dry run.

        A phase that means "from scratch" has to be able to say so. P02
        learned this live (2026-08-02): leftovers from an earlier pass
        made the wizard ask one EXTRA question, every scripted answer
        shifted by one, and the `y` meant for the Exchange prompt landed
        on "build the FTS index?" — a full crawl of the real store.
        """
        import shutil

        path = Path(path)
        target = self._resolve(path)
        guarded = self._resolve(self.sentinel.root)
        if self._within(target, guarded):
            raise UnsafeAction(
                f"refusing to remove inside the real state root: {path}")
        if not any(self._within(target, root) for root in self.write_roots):
            raise UnsafeAction(
                f"{path} is outside the run's write roots "
                f"({', '.join(str(r) for r in self.write_roots)})")
        if self.dry_run:
            self.intents.append(f"rm -r: {path}")
            self.note(f"would remove `{path}`")
            return path
        shutil.rmtree(target, ignore_errors=True)
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
            # A manual phase may bind a body (S3): the human cannot act
            # on a bare acceptance line when the phase needs real setup —
            # a deferred send armed, doctor run on both sides of a revoke
            # — so the body stages its effects through ctx and ends at
            # its own ctx.manual gate, returning that gate's verdict.
            # Unbound manual phases keep the bare protocol.
            fn = implementations.get(spec.id)
            status, evidence = fn(ctx) if fn is not None else ctx.manual(spec)
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
        strict_base = None
        if (spec.sentinel_strict and baseline is not None
                and not ctx.dry_run):
            # The strict claim is PHASE-LOCAL — "THIS phase touched
            # nothing real" — so it needs a witness at the phase door.
            # Judged against the RUN-start baseline, P16 was convicted
            # of P08/P09's legitimate prod churn (cancelled manifests,
            # audit events, index metadata — live pass 2026-08-02).
            strict_base = ctx.sentinel.capture()
        result = execute(spec, ctx, impls)
        if (spec.sentinel_strict and strict_base is not None
                and result.status in SETTLED and not ctx.dry_run):
            # The phase whose whole claim is "the real estate is
            # untouched" gets its claim checked immediately, with zero
            # expected churn allowed.
            strict = ctx.sentinel.verify(strict_base, strict=True)
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
    # FROM SCRATCH, always: a version bump leaves the previous release's
    # wheel behind, and the exactly-one guard below trips on it (found
    # live 2026-08-03: 0.12.0 beside 1.0.0rc1). An empty dist is what
    # makes "the one wheel found" provably the wheel THIS run built.
    ctx.rm_tree(dist)
    ctx.sh([sys.executable, "-m", "venv", venv], timeout=300, check=True)
    pip = venv / "bin" / "pip"
    built = ctx.sh([pip, "wheel", "--no-deps", "-w", dist, ctx.repo_root],
                   timeout=900, check=True)
    if built.dry:
        ctx.sh([pip, "install", "--force-reinstall", "--no-deps",
                dist / "email_mcp-<built>.whl"])
        ctx.sh([_venv_bin(ctx, "email-mcp"), "version"])
        return
    wheels = sorted(dist.glob("email_mcp-*.whl"))
    ctx.require(len(wheels) == 1,
                f"expected exactly one built wheel under {dist}, "
                f"found {len(wheels)}")
    wheel = wheels[0]
    expected = wheel.name.split("-")[1]
    # --force-reinstall is load-bearing: the version is identical between
    # builds, so a plain install sees "already satisfied" and keeps the
    # PREVIOUS pass's code — the RC would validate stale bytes forever
    # and every later phase would test yesterday's build (found live
    # 2026-08-02, when a fixed doctor check kept reporting the old
    # answer). --no-deps because the wheel's deps are already there.
    ctx.sh([pip, "install", "--force-reinstall", "--no-deps", wheel],
           timeout=900, check=True)
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
    root = ctx.sandbox_home / STATE_ROOT_NAME
    # FROM SCRATCH, always: "scripted setup" means a first install, and a
    # leftover tree makes the wizard ask an extra question that shifts
    # every answer by one (measured live 2026-08-02 — the shifted `y`
    # started an FTS crawl over the real store and hung the phase).
    ctx.rm_tree(root)
    ran = ctx.sh([_venv_bin(ctx, "email-mcp"), "setup"],
                 stdin_text="".join(a["answer"] + "\n" for a in answers),
                 timeout=300)
    if ran.dry:
        return
    ctx.require(ran.ok, f"setup exited {ran.rc}: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    # The fixture carries the PROMPTS as well as the answers, so a
    # misfeed is detectable instead of silent: every prompt must appear
    # in the transcript, in fixture order. A wizard that grew, lost or
    # reordered a question fails here, naming the drift.
    cursor, drift = 0, []
    for a in answers:
        hit = ran.out.find(a["prompt"], cursor)
        if hit < 0:
            drift.append(a["prompt"])
        else:
            cursor = hit + len(a["prompt"])
    ctx.require(not drift,
                "the wizard did not ask what the fixture answers, in "
                "order — missing/reordered: " + "; ".join(drift[:3])
                + ". Update tests/fixtures/setup_answers.json.")
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
# Why a "missing" doc is missing. Runs under the installed wheel so the
# path resolution is the SHIPPED one: a doc whose mailbox has no local
# store could never have been indexed (Mail keeps those bodies server-
# side); a doc whose mailbox DOES have a store, and whose .emlx is on
# disk, is an indexing failure the RC must not wave through.
_MISSING_PROBE = '''\
"""P04 probe: is every un-indexed doc explained by an absent local store?"""
import json

from email_mcp import config, fts
from email_mcp.sources.apple_mail import _connect_readonly
from email_mcp.sources.apple_mail_paths import find_emlx_path, mailbox_data_dir

SAMPLE = 400

idx = fts.FtsIndex()
conn = idx._open_ro()
rowids = [r[0] for r in conn.execute(
    "SELECT rowid FROM docs WHERE status = \'missing\' "
    "ORDER BY rowid DESC LIMIT ?", (SAMPLE,))]
conn.close()

base = config.mail_dir()
env = _connect_readonly(base / "MailData" / "Envelope Index")
out = {"checked": 0, "unexplained": [], "storeless_mailboxes": 0}
storeless = {}
for rid in rowids:
    row = env.execute(
        "SELECT mb.url AS url FROM messages m "
        "JOIN mailboxes mb ON mb.ROWID = m.mailbox WHERE m.ROWID = ?",
        (rid,)).fetchone()
    if row is None:            # deleted since the crawl — reconcile's job
        continue
    out["checked"] += 1
    url = row["url"]
    if url not in storeless:
        try:
            data_dir = mailbox_data_dir(base, url)
            storeless[url] = not (data_dir and data_dir.is_dir())
        except FileNotFoundError:
            storeless[url] = True      # no UUID subdir == no local store
    if storeless[url]:
        continue
    # The mailbox has a store: the body must genuinely be absent.
    try:
        path = find_emlx_path(mailbox_data_dir(base, url), rid)
    except Exception:
        path = None
    if path is not None and path.exists():
        out["unexplained"].append({"rowid": rid, "mailbox": url})
env.close()
out["storeless_mailboxes"] = sum(1 for v in storeless.values() if v)
print(json.dumps(out))
'''


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
    ctx.require(docs.get("error", 0) == 0,
                f"prod index holds extraction errors: {docs}")
    # "Full" cannot mean missing == 0 on a real Mac: a mailbox whose
    # bodies Mail never downloaded (an Exchange folder kept server-side —
    # this estate's Sent.mbox holds only an Info.plist) has NOTHING on
    # disk to index, and the Envelope Index still counts its messages.
    # So the criterion is EXPLAINED, not zero: every missing doc must
    # belong to a mailbox with no local store. One that does have a local
    # store is a genuine indexing failure and fails the phase (found live
    # 2026-08-02: 95k missing, every sampled one server-side-only).
    missing = docs.get("missing", 0)
    if missing:
        probe = ctx.write(ctx.state_dir / "p04_missing_probe.py",
                          _MISSING_PROBE)
        out = ctx.sh([_venv_bin(ctx, "python"), probe], timeout=300,
                     check=True)
        verdict = json.loads(out.out)
        ctx.require(not verdict["unexplained"],
                    f"{len(verdict['unexplained'])} missing doc(s) have a "
                    f"local store and should have indexed — e.g. "
                    f"{verdict['unexplained'][:3]}")
        ctx.note(f"prod: {missing} missing doc(s), all in mailboxes with "
                 f"no local store ({verdict['checked']} sampled, "
                 f"{verdict['storeless_mailboxes']} storeless mailbox(es))")
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
    # The client's argv[1] is the server it spawns: the bare console
    # script P01 installed — the command a real client registers
    # (contract §7, the test_mcp_wire.py _server_command discipline).
    ran = ctx.sh([_venv_bin(ctx, "python"), client,
                  _venv_bin(ctx, "email-mcp")], timeout=300)
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
# phase bodies — R2, stage S2: the mutation story (P06–P07, P10–P12)     #
# --------------------------------------------------------------------- #
#
# The stage's one governing fact is §1's rule that the sandbox borrows
# the real Mail store READ-ONLY. Everything below is shaped by it: a
# sandbox send is fenced twice (the identity's own declared allowlist +
# a file for a transport) so it cannot reach a real recipient even when
# a phase is wrong; the triage phases prove plan + refusal instead of
# applying against mail they may not touch; and P07 runs the shipped
# dispatcher under an RC-owned launchd label so the operator's agents
# are never displaced. Every mutation a phase performs is recorded into
# one expectations file — the evidence P12 audits the ledger against.

_RC_DISPATCHER_LABEL = "com.email-mcp.rc-dispatcher"

# The ledger's mail-mutation families (contract §6). P12 counts THESE:
# lifecycle/doctor_fix events belong to the estate story S1/S3 own and
# are outside the one-event-per-mail-mutation claim.
_LEDGER_FAMILIES = frozenset({
    "send", "reply", "draft", "schedule", "deliver", "recover", "cancel",
    "graph_adopt", "graph_flip", "graph_sent", "graph_cancelled_external",
    "plan_create", "plan_finish", "mailbox_create", "mailbox_delete",
})

# Poll budgets, module-level so a test can shrink them to zero instead
# of waiting out a live deadline.
_P06_STORE_DEADLINE_S = 300.0   # send → Bcc-to-self round trip, real IMAP
_P06_STORE_POLL_S = 10.0
_P07_DELIVER_DEADLINE_S = 240.0  # RunAtLoad + two 60s ticks + slack
_P07_POLL_S = 3.0


def _poll(deadline_s: float, interval_s: float, probe):
    """First probe immediately, judge the deadline before sleeping — a
    zero deadline means exactly one probe, which is what the tests use
    to exercise the timeout paths without waiting them out."""
    end = time.monotonic() + deadline_s
    while True:
        got = probe()
        if got is not None or time.monotonic() >= end:
            return got
        time.sleep(interval_s)


# The P05 stdio discipline generalized: one real MCP session per run of
# this client, the calls read from a JSON file so a phase can script any
# tool sequence. It reports; judging the envelopes is the phase's job.
_TOOL_CLIENT = '''\
"""S2 tool client: one MCP session over stdio, one envelope per call."""
import json
import subprocess
import sys

calls = json.loads(open(sys.argv[2], encoding="utf-8").read())
proc = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True)


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\\n")
    proc.stdin.flush()


def wait_for(rid):
    while True:
        line = proc.stdout.readline()
        if not line:
            return None  # EOF before the answer — reported, not judged
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # frame purity is P05's claim, not this client's
        if msg.get("id") == rid:
            return msg


send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "rc-runner-s2", "version": "0"}}})
wait_for(1)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

out = []
for n, call in enumerate(calls, start=2):
    send({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {
        "name": call["tool"], "arguments": call["arguments"]}})
    msg = wait_for(n)
    entry = {"tool": call["tool"], "envelope": None, "rpc_error": None,
             "is_error": False}
    if msg is None:
        entry["rpc_error"] = "no response (EOF)"
    elif "error" in msg:
        entry["rpc_error"] = json.dumps(msg["error"])
    else:
        result = msg.get("result") or {}
        entry["is_error"] = bool(result.get("isError"))
        content = result.get("content") or [{}]
        text = content[0].get("text") if content[0].get("type") == "text" \\
            else None
        if text is None:
            entry["rpc_error"] = "no text content in the result"
        else:
            try:
                entry["envelope"] = json.loads(text)
            except ValueError:
                entry["rpc_error"] = f"non-JSON content: {text[:120]!r}"
    out.append(entry)
proc.stdin.close()
print(json.dumps({"calls": out, "server_exit": proc.wait(timeout=30)}))
'''


def _mcp_session(ctx: Context, tag: str, calls, *,
                 extra_env: dict[str, str] | None = None,
                 timeout: float = 600, note: bool = True) -> list[dict] | None:
    """Run one MCP session and hand back one envelope per call (None on
    dry). Transport-level failure — an rpc error, isError, a dirty exit —
    is refused here because those are P05's already-proven claims
    resurfacing; everything envelope-shaped goes back to the phase.
    ``note=False`` is for sessions inside a poll loop, whose repetition
    would flood the report with identical lines."""
    if note:
        ctx.note(f"{tag}: tools/call " + ", ".join(t for t, _ in calls))
    client = ctx.write(ctx.sandbox_home / "rc-s2-client.py", _TOOL_CLIENT)
    calls_file = ctx.write(
        ctx.sandbox_home / f"rc-{tag}.calls.json",
        json.dumps([{"tool": t, "arguments": a} for t, a in calls], indent=2))
    ran = ctx.sh([_venv_bin(ctx, "python"), client,
                  _venv_bin(ctx, "email-mcp"), calls_file],
                 timeout=timeout, extra_env=extra_env)
    if ran.dry:
        return None
    ctx.require(ran.ok, f"{tag}: tool client exited {ran.rc}: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    report = json.loads(ran.out)
    ctx.require(report["server_exit"] == 0,
                f"{tag}: server exited {report['server_exit']}")
    envelopes = []
    for entry in report["calls"]:
        ctx.require(entry["rpc_error"] is None and not entry["is_error"],
                    f"{tag}: {entry['tool']} broke the wire: "
                    f"{entry['rpc_error'] or 'isError'}")
        envelope = entry["envelope"]
        ctx.require(isinstance(envelope, dict)
                    and isinstance(envelope.get("ok"), bool),
                    f"{tag}: {entry['tool']} returned no contract envelope")
        envelopes.append(envelope)
    return envelopes


def _record_mints(ctx: Context, records: list[dict], *,
                  reset: bool = False) -> None:
    """Append this phase's ledger-worthy mutations to the run's
    expectations file — the evidence P12 audits against. P06, the run's
    first mutation, resets the file so a fresh pass never inherits a
    previous run's claims; a resumed pass skips settled phases, so the
    records they earned survive untouched."""
    path = ctx.sandbox_home / "rc-s2-mints.json"
    existing = [] if reset or ctx.dry_run or not path.exists() \
        else json.loads(path.read_text(encoding="utf-8"))
    ctx.write(path, json.dumps(existing + records, indent=2))


def _mint(started: str, lane: str, event: str, outcome: str, *,
          op: str | None = None, message_id: str | None = None) -> dict:
    # ts is the PHASE start, not the record time: the ledger stamps the
    # event when the mutation lands, which is before the phase could
    # write this record — a later stamp would push the event out of
    # P12's since-window.
    return {"ts": started, "lane": lane, "event": event, "outcome": outcome,
            "op": op, "message_id": message_id}


def _fixture_addr(ctx: Context) -> str:
    """The sandbox's own address — the fixture's From: answer, the one
    spelling every sandbox phase treats as 'self'."""
    fixture = json.loads(
        (ctx.repo_root / "tests" / "fixtures" / "setup_answers.json")
        .read_text(encoding="utf-8"))["answers"]
    return next(a["answer"] for a in fixture
                if "From: address" in a["prompt"])


def _arm_sandbox_identity(ctx: Context) -> tuple[str, Path]:
    """Fence the sandbox transport before anything can send.

    Two independent fences, both the product's own mechanisms: the
    identity DECLARES an allowlist of exactly its own address (sends are
    open by default — declaring the guard arms it, so a foreign
    recipient dies as `recipient_not_allowed` inside the sender), and
    the pipe transport delivers into a file under the sandbox home —
    the sandbox's delivery store. P02's wizard answers configured
    `/usr/sbin/sendmail`, which could hand bytes to a real MTA; this
    rewrite is what makes "a sandbox send can never reach a real
    recipient" structural rather than hoped. Idempotent — P06 and P07
    each call it so neither depends on the other having run."""
    addr = _fixture_addr(ctx)
    store = ctx.sandbox_home / "rc-outbox" / "delivered.mbox"
    sink = ctx.write(
        ctx.sandbox_home / "rc-outbox" / "sink.sh",
        "#!/bin/sh\n"
        "# The sandbox's delivery store: bytes appended here provably left\n"
        "# the sender, and a file cannot reach a real recipient.\n"
        f'exec /bin/cat >> "{store}"\n',
        mode=0o700)
    ctx.write(
        ctx.sandbox_home / STATE_ROOT_NAME / "identities.toml",
        'default = "main"\n\n[main]\n'
        f'from_addr = "{addr}"\n'
        'from_name = "RC Sandbox"\n'
        'driver = "pipe"\n'
        f'command = "{sink}"\n'
        f'allowlist = ["{addr}"]\n')
    return addr, store


def _prod_self_addr(ctx: Context) -> str:
    """The prod lane's self-only recipient: the default identity's own
    from_addr, read from the real identities file (a read, not an
    effect — and the tree never holds secret values, P02's claim)."""
    path = ctx.sentinel.root / "identities.toml"
    ctx.require(path.is_file(),
                f"no identities.toml under {ctx.sentinel.root} — the prod "
                "send needs a configured identity")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    name = data.get("default") or (next(iter(tables))
                                   if len(tables) == 1 else None)
    addr = str((tables.get(name) or {}).get("from_addr", "")).strip()
    ctx.require(bool(addr),
                f"cannot determine the default identity's own address "
                f"from {path}")
    return addr


@implements("P06")
def _p06_send(ctx: Context) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    started = _now()
    # Sandbox half: self-only against the armed fences. The foreign-
    # recipient probe uses a reserved TLD so even a broken fence could
    # not resolve a real mailbox — but the claim under test is that the
    # product's own guard refuses it before any transport runs.
    ctx.lane = SANDBOX
    addr, store = _arm_sandbox_identity(ctx)
    probe_to = "rc-p06-blocked@example.invalid"
    sandbox = _mcp_session(ctx, "p06-sandbox", [
        ("send_email", {"to": addr, "subject": f"rc-p06 sandbox {stamp}",
                        "body": "RC P06 sandbox self-send."}),
        ("send_email", {"to": probe_to, "subject": f"rc-p06 fence {stamp}",
                        "body": "This must never leave the sender."}),
    ])
    ctx.lane = PROD
    if ctx.dry_run:
        _mcp_session(ctx, "p06-prod", [
            ("send_email", {"to": "<the default identity's own address>",
                            "subject": f"rc-p06 prod {stamp}",
                            "body": "RC P06 prod self-send."})])
        _mcp_session(ctx, "p06-prod-find", [
            ("refresh_mail", {"wait_seconds": 5}),
            ("search_emails", {"query": f"rc-p06 prod {stamp}",
                               "limit": 10})])
        return

    sent, probe = sandbox
    ctx.require(sent["ok"] is True,
                f"sandbox send failed: {sent.get('code')} {sent.get('error')}")
    ctx.require(sent.get("to") == [addr],
                f"sandbox send was not self-only: to={sent.get('to')}")
    mid = sent["message_id"]
    ctx.require(probe["ok"] is False
                and probe.get("code") == "recipient_not_allowed",
                "the sandbox send fence is not armed: a foreign recipient "
                f"got {probe.get('code') or 'ok=true'}, not "
                "recipient_not_allowed")
    delivered = store.read_text(encoding="utf-8")
    ctx.require(mid in delivered,
                f"{mid} is not in the sandbox delivery store — the send "
                "reported ok but nothing arrived")
    ctx.require(probe_to not in delivered,
                "the refused recipient reached the delivery store")
    ctx.note(f"sandbox: {mid} delivered to the sandbox store; foreign "
             "recipient refused recipient_not_allowed")

    # Prod half: the claim a fake store cannot make — a real self-only
    # send whose Message-ID is read back out of the real Mail store.
    self_addr = _prod_self_addr(ctx)
    subject = f"rc-p06 prod {stamp}"
    prod = _mcp_session(ctx, "p06-prod", [
        ("send_email", {"to": self_addr, "subject": subject,
                        "body": "RC P06 prod self-send."})])
    psent = prod[0]
    ctx.require(psent["ok"] is True,
                f"prod send failed: {psent.get('code')} {psent.get('error')}")
    pmid = psent["message_id"]

    def in_store():
        found = _mcp_session(ctx, "p06-prod-find", [
            ("refresh_mail", {"wait_seconds": 5}),
            ("search_emails", {"query": subject, "limit": 10})],
            note=False)
        hits = found[1].get("results") or [] if found[1]["ok"] else []
        ids = [h["id"] for h in hits][:10]
        if not ids:
            return None
        batch = _mcp_session(ctx, "p06-prod-read", [
            ("get_emails_batch", {"ids": ids, "view": "metadata"})],
            note=False)[0]
        for email in batch.get("emails") or []:
            headers = email.get("headers") or {}
            for key, value in headers.items():
                if key.lower() == "message-id" and value.strip() == pmid:
                    return (email.get("ref") or {}).get("id") or True
        return None

    hit = _poll(_P06_STORE_DEADLINE_S, _P06_STORE_POLL_S, in_store)
    ctx.require(hit is not None,
                f"prod send {pmid} not found in the store within "
                f"{_P06_STORE_DEADLINE_S:.0f}s")
    _record_mints(ctx, [
        _mint(started, SANDBOX, "send", "sent", message_id=mid),
        _mint(started, SANDBOX, "send", "failed"),
        _mint(started, PROD, "send", "sent", message_id=pmid),
    ], reset=True)
    ctx.note(f"prod: {pmid} sent self-only to {self_addr} and read back "
             f"from the store (id {hit})")


def _rc_dispatcher_plist(ctx: Context) -> str:
    """The RC's own dispatcher agent — deliberately NOT the shipped
    label. The per-user launchd domain is shared, so bootstrapping the
    shipped label would displace the operator's real dispatcher (the §2
    hazard); and the shipped plist pins no HOME, so launchd would hand
    the agent the REAL estate. This plist pins the sandbox HOME: the
    shipped dispatcher code, on the launchd cadence, over the sandbox
    spool and nothing else."""
    python = _venv_bin(ctx, "python")
    log = ctx.sandbox_home / "rc-dispatcher.log"
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{_RC_DISPATCHER_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>email_mcp.dispatcher</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>{ctx.sandbox_home}</string>
        <key>PATH</key><string>{path}</string>
    </dict>
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


@implements("P07")
def _p07_schedule_launchd(ctx: Context) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    started = _now()
    addr, store = _arm_sandbox_identity(ctx)
    now = datetime.now(timezone.utc)
    scheduled = _mcp_session(ctx, "p07-schedule", [
        # Due immediately (inside the 120s past-tolerance): the agent's
        # RunAtLoad pass can deliver it without waiting out a tick.
        ("schedule_email", {"to": addr, "subject": f"rc-p07 deliver {stamp}",
                            "body": "RC P07 dispatcher delivery.",
                            "send_at": now.isoformat(timespec="seconds")}),
        ("schedule_email", {"to": addr, "subject": f"rc-p07 cancel {stamp}",
                            "body": "RC P07 cancel target.",
                            "send_at": (now + timedelta(hours=1))
                            .isoformat(timespec="seconds")}),
    ])
    plist = ctx.write(
        ctx.sandbox_home / "Library" / "LaunchAgents"
        / f"{_RC_DISPATCHER_LABEL}.plist",
        _rc_dispatcher_plist(ctx))
    uid = os.getuid()
    target = f"gui/{uid}/{_RC_DISPATCHER_LABEL}"
    if ctx.dry_run:
        ctx.sh(["launchctl", "bootout", target])  # pre-clean a stale label
        ctx.sh(["launchctl", "bootstrap", f"gui/{uid}", plist])
        _mcp_session(ctx, "p07-cancel",
                     [("cancel_scheduled", {"id": "<spool-id>"})])
        ctx.sh(["launchctl", "bootout", target])
        return

    first, second = scheduled
    for env in (first, second):
        ctx.require(env["ok"] is True, f"schedule_email failed: "
                                       f"{env.get('code')} {env.get('error')}")
        ctx.require(env.get("executor") == "launchd",
                    f"entry {env.get('id')} took executor "
                    f"{env.get('executor')!r}, not the launchd spool")
    id1, mid1 = first["id"], first["message_id"]
    id2, mid2 = second["id"], second["message_id"]
    spool = ctx.sandbox_home / STATE_ROOT_NAME / "spool"

    # A crashed earlier pass leaves the agent loaded (kill -9 skips the
    # bootout below); a stale agent double-ticking this spool would fake
    # or break the delivery claim, so clear the label first.
    ctx.sh(["launchctl", "bootout", target])
    try:
        ctx.sh(["launchctl", "bootstrap", f"gui/{uid}", plist], check=True)
        delivered = _poll(
            _P07_DELIVER_DEADLINE_S, _P07_POLL_S,
            lambda: (spool / "sent" / f"{id1}.json").exists() or None)
        ctx.require(delivered,
                    f"the dispatcher agent did not deliver {id1} within "
                    f"{_P07_DELIVER_DEADLINE_S:.0f}s")
        manifest = json.loads(
            (spool / "sent" / f"{id1}.json").read_text(encoding="utf-8"))
        ctx.require(manifest.get("message_id") == mid1,
                    f"sent manifest carries {manifest.get('message_id')!r}, "
                    f"scheduled {mid1!r}")
        ctx.require(mid1 in store.read_text(encoding="utf-8"),
                    f"{mid1} never reached the sandbox delivery store")
        cancel = _mcp_session(ctx, "p07-cancel",
                              [("cancel_scheduled", {"id": id2})])[0]
        ctx.require(cancel["ok"] is True
                    and cancel.get("status") == "cancelled",
                    f"cancel_scheduled answered {cancel.get('code') or cancel}")
        cmanifest = spool / "cancelled" / f"{id2}.json"
        ctx.require(cmanifest.exists(),
                    f"{id2} did not land in cancelled/")
        kept = json.loads(cmanifest.read_text(encoding="utf-8"))
        ctx.require(kept.get("message_id") == mid2
                    and kept.get("status") == "cancelled",
                    f"cancelled manifest is not intact: {kept.get('id')} "
                    f"{kept.get('status')!r} {kept.get('message_id')!r}")
        ctx.require((spool / "cancelled" / f"{id2}.eml").exists(),
                    "the cancelled entry lost its frozen .eml")
    finally:
        # Leave no loaded agent, pass or fail — a live label ticking a
        # dead sandbox is exactly the stale state the pre-clean fears.
        ctx.sh(["launchctl", "bootout", target])
    armed = [p.name for state in ("pending", "sending")
             for p in sorted((spool / state).glob("*.json"))]
    ctx.require(not armed, "armed entries left behind: " + ", ".join(armed))
    _record_mints(ctx, [
        _mint(started, SANDBOX, "schedule", "scheduled", op=id1,
              message_id=mid1),
        _mint(started, SANDBOX, "schedule", "scheduled", op=id2,
              message_id=mid2),
        _mint(started, SANDBOX, "deliver", "sent", op=id1, message_id=mid1),
        _mint(started, SANDBOX, "cancel", "cancelled", op=id2),
    ])
    ctx.note(f"{id1} delivered by the launchd agent ({_RC_DISPATCHER_LABEL}); "
             f"{id2} cancelled with manifest + .eml intact; spool disarmed")


def _trashish(name: str) -> bool:
    low = name.lower()
    return any(t in low for t in ("trash", "deleted", "junk", "bin"))


def _triage_selection(ctx: Context, tag: str):
    """Pick a real selection from the attached store: the busiest
    non-trash mailbox, plus a same-account move target. Trash-adjacent
    names are skipped because a delete plan's selection excludes Trash
    by design — planning over it would read as an empty store."""
    scan = _mcp_session(ctx, f"{tag}-scan", [("list_mailboxes", {})])
    if scan is None:
        return None
    env = scan[0]
    ctx.require(env["ok"] is True,
                f"list_mailboxes failed: {env.get('code')} {env.get('error')}")
    boxes = env.get("mailboxes") or []
    populated = [b for b in boxes
                 if b.get("local_count", 0) > 0 and not _trashish(b["name"])]
    ctx.require(populated,
                "the attached store serves no non-trash mailbox with local "
                "rows — nothing to plan over")
    source = max(populated, key=lambda b: b["local_count"])
    targets = [b for b in boxes
               if b["account"] == source["account"]
               and b["name"] != source["name"] and not _trashish(b["name"])]
    ctx.require(targets,
                f"account {source['account']} has no second mailbox to "
                "plan a move into")
    return source["account"], source["name"], targets[0]["name"]


def _refused_apply(ctx: Context, tag: str, plan_env: dict) -> dict:
    """The shared back half of P10/P11: apply the frozen plan (TTL 0 has
    already expired it), require the refusal BEFORE any mutation, verify
    against the store that nothing moved, and require both ledger events
    to carry the plan's summary — plan_finish is the line that must
    outlive plan GC. Returns the audit envelope's events keyed by name."""
    plan_id, summary = plan_env["plan_id"], plan_env["summary"]
    planned = {m["id"]: m["mailbox"] for m in plan_env["messages"]}
    refused, batch, ledger = _mcp_session(ctx, f"{tag}-refuse", [
        ("triage_apply", {"plan_id": plan_id}),
        ("get_emails_batch", {"ids": sorted(planned), "view": "minimal"}),
        ("audit", {"plan_id": plan_id, "limit": 50}),
    ])
    ctx.require(refused["ok"] is False
                and refused.get("code") == "plan_expired",
                f"apply did not take the refusal path: "
                f"{refused.get('code') or 'ok=true'}")
    ctx.require(refused.get("operation_id") == plan_id,
                "the refusal envelope does not thread to its plan id")
    ctx.require(batch["ok"] is True and not batch.get("errors"),
                f"store readback failed for the planned messages: "
                f"{batch.get('errors') or batch.get('error')}")
    now = {e["id"]: e.get("mailbox") for e in batch.get("emails") or []}
    missing = sorted(set(planned) - set(now))
    ctx.require(not missing,
                "planned messages vanished from the store: "
                + ", ".join(missing))
    moved = sorted(i for i, box in planned.items() if now[i] != box)
    ctx.require(not moved,
                "the refused apply moved mail: " + ", ".join(moved))
    ctx.require(ledger["ok"] is True, "the audit read failed")
    events = ledger.get("events") or []
    by_name = {e.get("event"): e for e in events}
    ctx.require(len(events) == 2
                and set(by_name) == {"plan_create", "plan_finish"},
                f"expected exactly plan_create + plan_finish for {plan_id}, "
                f"got {[e.get('event') for e in events]}")
    ctx.require(by_name["plan_finish"].get("outcome") == "expired",
                f"plan_finish outcome is "
                f"{by_name['plan_finish'].get('outcome')!r}, not expired")
    off = sorted(name for name, e in by_name.items()
                 if e.get("op") != plan_id or e.get("summary") != summary)
    ctx.require(not off, "event(s) missing the plan id as op or the "
                         "summary line: " + ", ".join(off))
    return by_name


# TTL 0 expires the plan at the moment it freezes, so the later apply
# refuses `plan_expired` BEFORE its Mail pre-flight — the coded refusal
# is exercised with zero osascript and zero mutation. That is the plan +
# refusal path §1's read-only store demands of the sandbox lane.
_TTL_EXPIRED = {"EMAIL_MCP_TRIAGE_TTL": "0"}


@implements("P10")
def _p10_triage_plans(ctx: Context) -> None:
    started = _now()
    selection = _triage_selection(ctx, "p10")
    if ctx.dry_run:
        _mcp_session(ctx, "p10-plan", [
            ("triage_plan", {"account": "<account>", "mailbox": "<mailbox>",
                             "limit": 5,
                             "actions": [{"action": "flag", "color": 1},
                                         {"action": "move_to",
                                          "mailbox": "<target>"}]})],
            extra_env=_TTL_EXPIRED)
        _mcp_session(ctx, "p10-refuse", [
            ("triage_apply", {"plan_id": "<plan-id>"}),
            ("get_emails_batch", {"ids": ["<planned>"], "view": "minimal"}),
            ("audit", {"plan_id": "<plan-id>", "limit": 50})])
        return
    account, mailbox, target = selection
    plan_env = _mcp_session(ctx, "p10-plan", [
        ("triage_plan", {"account": account, "mailbox": mailbox, "limit": 5,
                         "actions": [{"action": "flag", "color": 1},
                                     {"action": "move_to",
                                      "mailbox": target}]})],
        extra_env=_TTL_EXPIRED)[0]
    ctx.require(plan_env["ok"] is True,
                f"triage_plan failed: {plan_env.get('code')} "
                f"{plan_env.get('error')}")
    verbs = sorted(a.get("action") for a in plan_env.get("actions") or [])
    ctx.require(verbs == ["flag", "move_to"],
                f"the plan froze {verbs}, not move + flag")
    _refused_apply(ctx, "p10", plan_env)
    _record_mints(ctx, [
        _mint(started, SANDBOX, "plan_create", "created",
              op=plan_env["plan_id"]),
        _mint(started, SANDBOX, "plan_finish", "expired",
              op=plan_env["plan_id"]),
    ])
    ctx.note(f"plan {plan_env['plan_id']}: move+flag over "
             f"{plan_env['count']} message(s) in {mailbox} → {target}; "
             f"apply refused plan_expired before any mutation; store "
             f"verified unmoved; both ledger events carry the summary "
             f"{plan_env['summary']!r}")


@implements("P11")
def _p11_trash_plan(ctx: Context) -> None:
    started = _now()
    selection = _triage_selection(ctx, "p11")
    if ctx.dry_run:
        _mcp_session(ctx, "p11-plan", [
            ("triage_plan_delete", {"account": "<account>",
                                    "mailbox": "<mailbox>", "limit": 3})],
            extra_env=_TTL_EXPIRED)
        _mcp_session(ctx, "p11-refuse", [
            ("triage_apply", {"plan_id": "<plan-id>"}),
            ("get_emails_batch", {"ids": ["<planned>"], "view": "minimal"}),
            ("audit", {"plan_id": "<plan-id>", "limit": 50})])
        return
    account, mailbox, _target = selection
    plan_env = _mcp_session(ctx, "p11-plan", [
        ("triage_plan_delete", {"account": account, "mailbox": mailbox,
                                "limit": 3})],
        extra_env=_TTL_EXPIRED)[0]
    ctx.require(plan_env["ok"] is True,
                f"triage_plan_delete failed: {plan_env.get('code')} "
                f"{plan_env.get('error')}")
    # The delete selection excludes Trash by design — a planned message
    # already sitting there would mean the exclusion failed.
    trashed = sorted(m["id"] for m in plan_env["messages"]
                     if _trashish(m["mailbox"]))
    ctx.require(not trashed,
                "the delete plan selected trash-resident messages: "
                + ", ".join(trashed))
    _refused_apply(ctx, "p11", plan_env)
    _record_mints(ctx, [
        _mint(started, SANDBOX, "plan_create", "created",
              op=plan_env["plan_id"]),
        _mint(started, SANDBOX, "plan_finish", "expired",
              op=plan_env["plan_id"]),
    ])
    ctx.note(f"delete plan {plan_env['plan_id']}: {plan_env['count']} "
             f"message(s) in {mailbox}, none Trash-resident; apply refused "
             f"plan_expired; store verified — nothing landed in Trash; "
             f"ledger carries the summary {plan_env['summary']!r}")


@implements("P12")
def _p12_audit_inspection(ctx: Context) -> None:
    if ctx.dry_run:
        ctx.lane = SANDBOX
        _mcp_session(ctx, "p12-sandbox",
                     [("audit", {"since": "<run-start>", "limit": 500})])
        ctx.lane = PROD
        _mcp_session(ctx, "p12-prod",
                     [("audit", {"since": "<run-start>", "limit": 500})])
        return
    mints_path = ctx.sandbox_home / "rc-s2-mints.json"
    ctx.require(mints_path.exists(),
                "no mutation expectations recorded — P06–P11 write them; "
                "run P12 after the phases whose ledger it audits")
    mints = json.loads(mints_path.read_text(encoding="utf-8"))
    ctx.require(bool(mints), "the expectations file is empty")
    # ISO strings order lexicographically; +00:00 keeps the bound
    # parseable by the server's fromisoformat.
    since = min(r["ts"] for r in mints).replace("Z", "+00:00")

    ctx.lane = SANDBOX
    sandbox = _mcp_session(ctx, "p12-sandbox",
                           [("audit", {"since": since, "limit": 500})])[0]
    ctx.lane = PROD
    prod = _mcp_session(ctx, "p12-prod",
                        [("audit", {"since": since, "limit": 500})])[0]
    for lane, env in ((SANDBOX, sandbox), (PROD, prod)):
        ctx.require(env["ok"] is True, f"{lane} audit read failed: "
                                       f"{env.get('code')} {env.get('error')}")

    def matches(record: dict, events: list[dict]) -> list[dict]:
        return [e for e in events
                if e.get("event") == record["event"]
                and e.get("outcome") == record["outcome"]
                and (record["op"] is None or e.get("op") == record["op"])
                and (record["message_id"] is None
                     or e.get("message_id") == record["message_id"])]

    # Sandbox: the estate was born this pass, so the claim is exact — a
    # bijection between the mutations the phases recorded and the
    # mail-mutation events on the ledger. One each, nothing else.
    observed = [e for e in sandbox.get("events") or []
                if e.get("event") in _LEDGER_FAMILIES]
    expected = [r for r in mints if r["lane"] == SANDBOX]
    for record in expected:
        hits = matches(record, observed)
        ctx.require(len(hits) == 1,
                    f"sandbox {record['event']}/{record['outcome']} "
                    f"(op {record['op']}, mid {record['message_id']}): "
                    f"{len(hits)} event(s), want exactly 1")
    ctx.require(len(observed) == len(expected),
                f"sandbox ledger holds {len(observed)} mutation event(s) "
                f"for {len(expected)} recorded mutation(s)")
    unthreaded = [e.get("event") for e in observed if not e.get("op")]
    ctx.require(not unthreaded, "event(s) carry no operation id: "
                                + ", ".join(unthreaded))

    # Prod: the estate is live — operator traffic during the window is
    # not this run's claim. Ours must appear exactly once each; a second
    # event for one of our mutations would be a double-emit.
    prod_events = [e for e in prod.get("events") or []
                   if e.get("event") in _LEDGER_FAMILIES]
    ours = 0
    for record in (r for r in mints if r["lane"] == PROD):
        hits = matches(record, prod_events)
        ctx.require(len(hits) == 1,
                    f"prod {record['event']}/{record['outcome']} "
                    f"(mid {record['message_id']}): {len(hits)} event(s), "
                    f"want exactly 1")
        ours += 1
    unrelated = len(prod_events) - ours
    ctx.note(f"sandbox: {len(expected)} mutation(s) ↔ {len(observed)} "
             f"event(s), one each, every op threaded — one audit call")
    ctx.note(f"prod: {ours} mutation(s) found exactly once in one audit "
             f"call ({unrelated} unrelated live-estate event(s) outside "
             f"this run's claim); spool ids and plan ids ride as op, "
             f"Message-IDs on the send events")


# --------------------------------------------------------------------- #
# phase bodies — R2, stage S3: the prod lane (P08, P09, P14, P17, P19)   #
# --------------------------------------------------------------------- #
#
# These phases operate the operator's REAL install — real identities,
# real Graph tokens, real agents — so they only ever run under --execute
# with a human present, and every recipient is read out of the real
# identities file (self-only by construction), never typed into a phase.
# The manual halves go through ctx.manual with the EXACT steps in the
# prompt; a bound manual body returns that gate's verdict, so an
# unattended pass still ends PENDING — never invented. P19 is the phase
# the plan predates: tool #21 (create_draft, 2026-08-02) files intent
# back to the human and never sends (docs/draft-design.md).

# The two shipped agents P17 re-bootstraps. PROD_LABELS also carries the
# v0.9 legacy label, which is P15's sandbox business — resurrecting it
# on the real estate would undo the very migration P15 proves.
_P17_AGENTS = PROD_LABELS[:2]

# Poll budgets, module-level so a test can shrink them (the S2 rule).
_P17_TICK_DEADLINE_S = 90.0   # the plan's own number: observed to tick
_P17_TICK_POLL_S = 5.0

# P09's lead time: long enough to close the lid with margin, short
# enough that the operator is still at the machine when it fires.
_P09_LEAD_MIN = 10


def _prod_identity_where(ctx: Context, cond, need: str) -> tuple[str, str]:
    """The first identity in the REAL identities file satisfying `cond`,
    as (name, from_addr) — a read, not an effect, and the tree never
    holds a secret value (P02's claim). The prod lane tests the
    operator's actual capabilities, so an estate without the shape a
    phase needs is a finding with a name, never a silent skip."""
    path = ctx.sentinel.root / "identities.toml"
    if not path.is_file():
        raise PhaseFailure(
            f"no identities.toml under {ctx.sentinel.root} — {need}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for name, table in data.items():
        if not isinstance(table, dict):
            continue  # top-level scalars: `default`
        addr = str(table.get("from_addr", "")).strip()
        if addr and cond(table):
            return name, addr
    raise PhaseFailure(f"no identity in {path} {need}")


# What the tenant holds for one spool entry, read through the shipped
# seam — the same verbs the dispatcher's reconcile pass trusts. "held"
# is the deferred-draft proof: /send was accepted yet the message is
# still a draft, which only PidTagDeferredSendTime can arrange.
_GRAPH_STATUS_PROBE = '''\
"""P08 probe: tenant-side status of a deferred draft (shipped seam)."""
import json
import sys

from email_mcp import graph, identities

ident = identities.get(sys.argv[1])
print(json.dumps({"status": graph.draft_status(ident, sys.argv[2],
                                               sys.argv[3])}))
'''


@implements("P08")
def _p08_schedule_graph(ctx: Context) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    started = _now()
    probe = ctx.write(ctx.sandbox_home / "rc-p08-status.py",
                      _GRAPH_STATUS_PROBE)
    if ctx.dry_run:
        _mcp_session(ctx, "p08-schedule", [
            ("schedule_email", {"to": "<the graph identity's own address>",
                                "subject": f"rc-p08 {stamp}",
                                "body": "RC P08 deferred self-send.",
                                "send_at": "<now + 1h>",
                                "from_identity": "<graph-identity>"})])
        ctx.sh([_venv_bin(ctx, "python"), probe, "<graph-identity>",
                "<draft-id>", "<message-id>"])
        _mcp_session(ctx, "p08-cancel",
                     [("cancel_scheduled", {"id": "<spool-id>"})])
        return
    name, addr = _prod_identity_where(
        ctx, lambda t: t.get("executor") == "graph",
        'with executor = "graph" — the Graph schedule lane needs one')
    send_at = (datetime.now(timezone.utc)
               + timedelta(hours=1)).isoformat(timespec="seconds")
    env = _mcp_session(ctx, "p08-schedule", [
        ("schedule_email", {"to": addr, "subject": f"rc-p08 {stamp}",
                            "body": "RC P08 deferred self-send.",
                            "send_at": send_at, "from_identity": name})])[0]
    ctx.require(env["ok"] is True, f"schedule_email failed: "
                                   f"{env.get('code')} {env.get('error')}")
    sid, mid = env["id"], env["message_id"]
    if env.get("executor") != "graph":
        # The silent graph→launchd fallback is a kept promise for a user
        # and a failed phase for the RC: the lane under test never ran.
        # Disarm first — a failed phase must not leave a real send
        # pending on a one-hour fuse.
        _mcp_session(ctx, "p08-cancel",
                     [("cancel_scheduled", {"id": sid})])
        raise PhaseFailure(
            f"entry {sid} fell back to executor {env.get('executor')!r} — "
            "Exchange never took the schedule (check `python -m "
            "email_mcp.graph --status`)")
    draft_id = env.get("graph_draft_id")
    # From here to the cancel, EVERY exit path must disarm: the missing-
    # draft_id require and a probe-spawn exception both used to leave the
    # real deferred self-send armed on its one-hour fuse (S3 gate
    # finding). `cancelled` flips only once a cancel was actually issued.
    cancelled = False
    try:
        ctx.require(bool(draft_id),
                    f"entry {sid} took the graph executor but carries no "
                    "graph_draft_id — nothing to verify against the tenant")
        held = ctx.sh([_venv_bin(ctx, "python"), probe, name, draft_id, mid],
                      timeout=120)
        cancel = _mcp_session(ctx, "p08-cancel",
                              [("cancel_scheduled", {"id": sid})])[0]
        cancelled = True
    finally:
        if not cancelled:
            try:
                _mcp_session(ctx, "p08-cancel",
                             [("cancel_scheduled", {"id": sid})])
                ctx.note(f"disarmed {sid} after phase failure")
            except Exception as e:  # noqa: BLE001 — never mask the real
                # failure; but an armed fuse the disarm could not reach
                # must be SHOUTED, not buried in a finally.
                ctx.note(f"DISARM FAILED for {sid}: {e} — cancel it "
                         f"yourself NOW: cancel_scheduled id={sid}")
    ctx.require(held.ok, f"tenant status probe exited {held.rc}: "
                         f"{(held.err or held.out).strip()[:200]}")
    held_status = json.loads(held.out)["status"]
    ctx.require(held_status == "held",
                f"the tenant does not hold {draft_id} as a deferred draft "
                f"(draft_status {held_status!r}) — PidTagDeferredSendTime "
                "never armed")
    ctx.require(cancel["ok"] is True and cancel.get("status") == "cancelled",
                f"cancel_scheduled answered {cancel.get('code') or cancel}")
    gone = ctx.sh([_venv_bin(ctx, "python"), probe, name, draft_id, mid],
                  timeout=120, check=True)
    gone_status = json.loads(gone.out)["status"]
    ctx.require(gone_status == "cancelled_externally",
                f"after the cancel the tenant reports {gone_status!r} — "
                "the deferred draft was not removed cleanly")
    _record_mints(ctx, [
        _mint(started, PROD, "schedule", "scheduled", op=sid,
              message_id=mid),
        _mint(started, PROD, "cancel", "cancelled", op=sid),
    ])
    ctx.note(f"{sid}: Exchange held deferred draft {draft_id} "
             f"(send_at {send_at}); cancelled — envelope cancelled, "
             "tenant confirms the draft gone, nothing sent")


@implements("P09")
def _p09_lid_closed(ctx: Context) -> tuple[str, str]:
    spec = PLAN_BY_ID["P09"]
    started = _now()
    if ctx.dry_run:
        _mcp_session(ctx, "p09-schedule", [
            ("schedule_email", {"to": "<the graph identity's own address>",
                                "subject": "rc-p09 lid-closed <stamp>",
                                "body": "RC P09 lid-closed delivery.",
                                "send_at": f"<now + {_P09_LEAD_MIN}m>",
                                "from_identity": "<graph-identity>"})])
        return ctx.manual(spec)
    if ctx.answer is None:
        # Arming a real deferred send with nobody at the lid would burn
        # the evidence window on every unattended pass — stage nothing;
        # the bare protocol records PENDING with the re-run instruction.
        return ctx.manual(spec)
    name, addr = _prod_identity_where(
        ctx, lambda t: t.get("executor") == "graph",
        'with executor = "graph" — lid-closed delivery is the Graph '
        "executor's claim")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    send_at = datetime.now(timezone.utc) + timedelta(minutes=_P09_LEAD_MIN)
    subject = f"rc-p09 lid-closed {stamp}"
    env = _mcp_session(ctx, "p09-schedule", [
        ("schedule_email", {"to": addr, "subject": subject,
                            "body": "RC P09 lid-closed delivery.",
                            "send_at": send_at.isoformat(timespec="seconds"),
                            "from_identity": name})])[0]
    ctx.require(env["ok"] is True, f"schedule_email failed: "
                                   f"{env.get('code')} {env.get('error')}")
    ctx.require(env.get("executor") == "graph",
                f"entry {env.get('id')} fell back to "
                f"{env.get('executor')!r} — a launchd entry cannot deliver "
                "with the lid closed; fix the graph lane and re-run")
    sid, mid = env["id"], env["message_id"]
    _record_mints(ctx, [
        _mint(started, PROD, "schedule", "scheduled", op=sid,
              message_id=mid)])
    hhmm = send_at.strftime("%H:%M:%S")
    steps = (
        f"Entry {sid} is armed at Exchange: subject {subject!r}, send_at "
        f"{hhmm} UTC, self-only to {addr}.\n"
        f"1. CLOSE THE LID NOW and note the time; keep it closed until "
        f"after {hhmm} UTC.\n"
        "2. Reopen, let Mail sync, and find the delivered copy (subject "
        f"above). Compare its Date header to {hhmm} UTC — Exchange fires "
        "the deferred time to the second (calibration 2026-07-29); the "
        "spool entry reconciles to sent/ after the dispatcher's 10-minute "
        "grace.\n"
        "3. Answer `pass: delivered <Date>, lid down <time>` — the two "
        "timestamps ARE the evidence. If it never arrives, answer "
        f"`fail: …` (the entry stays visible in list_scheduled as {sid}).")
    return ctx.manual(replace(spec, acceptance=steps))


@implements("P14")
def _p14_permission_revoke(ctx: Context) -> tuple[str, str]:
    spec = PLAN_BY_ID["P14"]
    email_mcp = _venv_bin(ctx, "email-mcp")
    if ctx.dry_run:
        ctx.sh([email_mcp, "doctor"])    # baseline: green before the revoke
        ctx.sh([email_mcp, "doctor"])    # revoked: red, naming the pane
        ctx.sh([email_mcp, "--doctor"])  # the JSON behind the fix check
        ctx.sh([email_mcp, "doctor"])    # regranted: green again
        return ctx.manual(spec)
    if ctx.answer is None:
        return ctx.manual(spec)
    # Baseline first: if the estate is not green BEFORE the revoke, the
    # phase would blame Full Disk Access for someone else's failure.
    before = ctx.sh([email_mcp, "doctor"], timeout=300)
    ctx.require(before.ok,
                "prod doctor is not green before the revoke — fix the "
                "estate (P03) before P14 can isolate the FDA claim: "
                + (before.err or before.out).strip()[:200])
    revoke = (
        "System Settings → Privacy & Security → Full Disk Access: turn "
        "OFF the entry for the app that runs this server (the terminal "
        "app, or the venv python if granted directly). Do NOT delete the "
        "row — you will turn it back on in the next step.\n"
        "Answer `pass` once revoked; doctor runs next and must name this "
        "exact pane as the fix.")
    verdict = ctx.manual(replace(spec, acceptance=revoke))
    if verdict[0] != PASS:
        return verdict
    if verdict[1]:
        ctx.note(f"operator (revoke): {verdict[1]}")
    revoked = ctx.sh([email_mcp, "doctor"], timeout=300)
    revoked_json = ctx.sh([email_mcp, "--doctor"], timeout=300)
    ctx.require(not revoked.ok,
                "doctor stayed green with Full Disk Access revoked — the "
                "revoke did not bite this binary; toggle the entry that "
                "actually grants it and re-run")
    ctx.require("Traceback" not in revoked.out + revoked.err,
                "doctor crashed instead of reporting the revocation")
    report = json.loads(revoked_json.out)
    red = {name: c
           for name, c in {**report["checks"], "audit": report["audit"]}
           .items() if not c["ok"]}
    unfixed = sorted(name for name, c in red.items() if not c.get("fix"))
    ctx.require(not unfixed, "revoked-side failure(s) name no fix: "
                             + ", ".join(unfixed))
    ctx.require(any("Full Disk Access" in str(c.get("fix"))
                    for c in red.values()),
                "no revoked-side failure names the Full Disk Access pane "
                f"— doctor blamed something else: {sorted(red)}")
    for name, c in sorted(red.items()):
        ctx.note(f"revoked {name}: {c['detail']} — fix: {c['fix']}")
    regrant = (
        "Turn the same Full Disk Access entry back ON (restart the app "
        "if Settings asks), then answer `pass`. Doctor runs once more "
        "and every check must be green again.")
    verdict = ctx.manual(replace(spec, acceptance=regrant))
    if verdict[0] != PASS:
        return verdict
    if verdict[1]:
        ctx.note(f"operator (regrant): {verdict[1]}")
    after = ctx.sh([email_mcp, "doctor"], timeout=300)
    fails = "; ".join(line for line in after.out.splitlines()
                      if line.startswith("FAIL"))
    ctx.require(after.ok, "doctor is not green after the regrant: "
                          + (fails or (after.err or after.out)
                             .strip()[:200]))
    ctx.note("doctor: green → revoked names the FDA pane → green again")
    return PASS, ""


@implements("P17")
def _p17_teardown(ctx: Context) -> None:
    uid = os.getuid()
    agents = ctx.real_home / "Library" / "LaunchAgents"
    dispatcher = _P17_AGENTS[0]
    if ctx.dry_run:
        for label in _P17_AGENTS:
            ctx.sh(["launchctl", "bootout", f"gui/{uid}/{label}"])
            ctx.sh(["launchctl", "bootstrap", f"gui/{uid}",
                    agents / f"{label}.plist"])
        ctx.sh(["launchctl", "print", f"gui/{uid}/{dispatcher}"])
        return
    for label in _P17_AGENTS:
        plist = agents / f"{label}.plist"
        if not plist.exists():
            # The FTS agent is a setup OPTION; the dispatcher is the
            # phase's whole claim.
            ctx.require(label != dispatcher,
                        f"{plist} is not installed — there is no prod "
                        "dispatcher to re-bootstrap; run `email-mcp "
                        "update` first")
            ctx.note(f"{label}: not installed on this estate — skipped")
            continue
        # Re-bootstrap = bootout + bootstrap of the EXISTING plist —
        # deliberately NOT --install-launchd through the sandbox wheel,
        # which would re-render the plist around the RC's venv python
        # and leave an agent pointing into a sandbox that gets purged.
        ctx.sh(["launchctl", "bootout", f"gui/{uid}/{label}"])
        ctx.sh(["launchctl", "bootstrap", f"gui/{uid}", plist], check=True)
        ctx.note(f"{label}: re-bootstrapped from {plist}")

    def ticked():
        ran = ctx.sh(["launchctl", "print", f"gui/{uid}/{dispatcher}"],
                     timeout=30)
        if not ran.ok:
            return None
        runs = re.search(r"\bruns = (\d+)", ran.out)
        clean = re.search(r"last exit code = 0\b", ran.out)
        if runs and int(runs.group(1)) >= 1 and clean:
            return f"runs = {runs.group(1)}, last exit code = 0"
        return None

    # bootout reset launchd's counters, so runs >= 1 with a clean exit
    # can only mean a tick that happened AFTER the re-bootstrap.
    hit = _poll(_P17_TICK_DEADLINE_S, _P17_TICK_POLL_S, ticked)
    ctx.require(hit is not None,
                "the dispatcher was re-bootstrapped but launchd shows no "
                f"completed run within {_P17_TICK_DEADLINE_S:.0f}s")
    ctx.note(f"dispatcher ticked after re-bootstrap ({hit}) — the run is "
             "over because the real dispatcher ran again, not because "
             "the runner said so")


# Tenant-side readback for a filed draft, through the shipped seams:
# graph.draft_receipt for the three acceptance legs, plus the stored
# MIME ($value) for the transfer encoding — OWA's editor mangles
# quoted-printable soft breaks when loading a MIME-created draft
# (measured 2026-08-02, draft-design.md round 1), so the parts must be
# base64 AT REST, not merely in what the composer handed over.
_DRAFT_READBACK_PROBE = '''\
"""P19 probe: what the tenant stores for one filed draft."""
import email
import json
import sys
import urllib.parse

from email_mcp import graph, identities

ident = identities.get(sys.argv[1])
draft_id = sys.argv[2]
out = graph.draft_receipt(ident, draft_id)
status, body = graph._graph(
    "GET", "/me/messages/" + urllib.parse.quote(draft_id) + "/$value", ident)
if status != 200:
    raise SystemExit(f"$value readback failed: HTTP {status}")
msg = email.message_from_string(body.get("raw", ""))
out["text_parts"] = sorted(
    (part.get_content_type(),
     (part.get("Content-Transfer-Encoding") or "").strip().lower())
    for part in msg.walk()
    if part.get_content_type() in ("text/plain", "text/html"))
print(json.dumps(out))
'''


@implements("P19")
def _p19_drafts(ctx: Context) -> tuple[str, str]:
    spec = PLAN_BY_ID["P19"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe = ctx.write(ctx.sandbox_home / "rc-p19-readback.py",
                      _DRAFT_READBACK_PROBE)
    body_text = ("RC P19 draft — edit me, then send me by hand.\n\n"
                 "Second paragraph, so the editor round-trip has real "
                 "soft-break material to preserve.")
    if ctx.dry_run:
        _mcp_session(ctx, "p19-draft", [
            ("create_draft", {"to": "<the drafts identity's own address>",
                              "subject": f"rc-p19 draft {stamp}",
                              "body": body_text,
                              "from_identity": "<drafts-identity>"}),
            ("create_draft", {"to": "<the drafts identity's own address>",
                              "subject": f"rc-p19 refused {stamp}",
                              "body": "This must never be filed.",
                              "from_identity": "<lane-less identity>"})])
        ctx.sh([_venv_bin(ctx, "python"), probe, "<drafts-identity>",
                "<draft-id>"])
        return ctx.manual(spec)
    name, addr = _prod_identity_where(
        ctx, lambda t: t.get("drafts") == "graph",
        'with drafts = "graph" — the drafts lane needs one')
    subject = f"rc-p19 draft {stamp}"
    created = _mcp_session(ctx, "p19-draft", [
        ("create_draft", {"to": addr, "subject": subject,
                          "body": body_text, "from_identity": name}),
    ])[0]
    ctx.require(created["ok"] is True,
                f"create_draft failed: {created.get('code')} "
                f"{created.get('error')}")
    # The refusal half must never demand the OPERATOR keep a lane-less
    # identity on the real estate (live pass 2026-08-02: every prod
    # identity had the lane and the phase refused to run). Prove it
    # against the SAME live wheel with an ephemeral identities override —
    # the real file is never named, never touched.
    bare = "rc-p19-bare"
    scratch = ctx.write(
        ctx.sandbox_home / "rc-p19-identities.toml",
        f'default = "{bare}"\n\n[{bare}]\n'
        f'from_addr = "{addr}"\n'
        'driver = "pipe"\ncommand = "/usr/bin/true"\n')
    refused = _mcp_session(
        ctx, "p19-refused",
        [("create_draft", {"to": addr,
                           "subject": f"rc-p19 refused {stamp}",
                           "body": "This must never be filed."})],
        extra_env={"EMAIL_MCP_IDENTITIES": str(scratch)})[0]
    draft_id, mid = created["draft_id"], created["message_id"]
    ctx.require(refused["ok"] is False
                and refused.get("code") == "draft_unsupported",
                f"identity [{bare}] declares no drafts lane yet "
                f"create_draft answered {refused.get('code') or 'ok=true'} "
                "— the refusal must be draft_unsupported, never a "
                "fallback that files elsewhere")
    ctx.require("identities.toml" in (refused.get("error") or ""),
                "the draft_unsupported refusal names no fix")
    # Independent readback — evidence, never the tool's own report: the
    # three legs again, plus what a client's editor will actually load.
    ran = ctx.sh([_venv_bin(ctx, "python"), probe, name, draft_id],
                 timeout=120, check=True)
    got = json.loads(ran.out)
    legs = (got["is_draft"] is True
            and got["internet_message_id"] == mid
            and got["in_drafts_folder"] is True)
    ctx.require(legs, f"three-legged readback failed for {draft_id}: "
                      f"is_draft={got['is_draft']}, message_id_match="
                      f"{got['internet_message_id'] == mid}, "
                      f"in_drafts_folder={got['in_drafts_folder']}")
    ctx.require(bool(got["text_parts"])
                and all(cte == "base64"
                        for _ctype, cte in got["text_parts"]),
                f"the stored draft is not base64: {got['text_parts']} — "
                "OWA's editor mangles quoted-printable (measured "
                "2026-08-02)")
    ctx.note(f"draft {draft_id} filed into {addr}'s Drafts by [{name}]: "
             f"isDraft, our Message-ID ({mid}), the drafts folder, "
             f"base64 text parts; [{bare}] refused draft_unsupported "
             "with the fix, filing nothing")
    steps = (
        f"A draft is filed: subject {subject!r}, in {addr}'s Drafts.\n"
        "1. Open it in OWA or the phone's Outlook — the CLIENT's editor "
        "is the thing under test, so do not read it through this tool.\n"
        "2. Check every word survived: no 'se=t' / '=aragraph' "
        "artifacts, paragraph break intact (round 1 caught the QP bug "
        "exactly here).\n"
        "3. Edit the body (add a line), then send it by hand — to "
        "yourself.\n"
        "4. Confirm the received copy renders clean.\n"
        "Answer `pass: <what you saw>` / `fail: <what broke>`.")
    return ctx.manual(replace(spec, acceptance=steps))


# --------------------------------------------------------------------- #
# phase bodies — R2, stage S4: the failure matrix and the lifecycle      #
# closure (P13, P15, P16, P18)                                           #
# --------------------------------------------------------------------- #
#
# P13 injects the plan's ten failures into the sandbox estate and judges
# each against its own "expected" column, one evidence line per FM.
# Ordering is load-bearing: FM3 runs LAST because its whole point (the
# plan's rule the stage brief names D11: the at-most-once window is
# DEMONSTRATED, never reconciled) is that nothing ticks the spool after
# the injected kill — a later dispatcher pass would requeue the stranded
# claim and quietly turn the exhibit into a re-delivery. P15 builds a
# REAL v0.9 estate with the v0.9 bytes themselves (a detached worktree
# at the release commit) and lets the current wheel migrate and operate
# it. P16 closes the sandbox life story with the product's own uninstall
# verb, then the runner's strict Sentinel proves the real estate
# byte-identical. P18 is the once-ever human walk: its body IS the
# protocol text.

_V09_REF = "75c6f93"                # the v0.9.0 release commit (P15)
_LEGACY_LABEL = PROD_LABELS[2]      # pre-v0.8 label the migration retires

# FM1's fuse: long enough that an unbounded rebuild over the attached
# real store is provably still crawling when the SIGKILL lands.
_FM1_KILL_AFTER_S = 2.0


def _sandbox_spool(ctx: Context) -> Path:
    return ctx.sandbox_home / STATE_ROOT_NAME / "spool"


def _sink(ctx: Context, script: str) -> Path:
    """(Re)write the sandbox delivery sink. P13 swaps the sink under the
    armed identity to place a kill at an exact point of the delivery
    path; _arm_sandbox_identity restores the plain delivering one."""
    return ctx.write(ctx.sandbox_home / "rc-outbox" / "sink.sh", script,
                     mode=0o700)


def _kill_sink(store: Path, *, deliver: bool) -> str:
    """A sink that SIGKILLs its parent — the dispatcher — either before
    accepting the bytes (FM2: the crash lands ahead of the transport
    handoff) or right after appending them (FM3: the delivery is real,
    but the sending→sent rename and the deliver emit never run). $PPID
    IS the dispatcher: the pipe transport execs the command directly,
    no intermediate shell."""
    accept = f'/bin/cat >> "{store}"\n' if deliver else ""
    return "#!/bin/sh\n" + accept + "kill -9 $PPID\nexit 0\n"


def _dispatch_once(ctx: Context, *, timeout: float = 300) -> Ran:
    """One dispatcher pass over the sandbox spool — invoked directly,
    not via launchd: P13 kills this process on purpose, and a launchd
    agent would resurrect it on the next tick, destroying the exhibit."""
    return ctx.sh([_venv_bin(ctx, "python"), "-m", "email_mcp.dispatcher"],
                  timeout=timeout)


def _fm_schedule(ctx: Context, tag: str, addr: str, subject: str, *,
                 offset_s: int = 0) -> tuple[str, str]:
    send_at = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    env = _mcp_session(ctx, tag, [
        ("schedule_email", {"to": addr, "subject": subject,
                            "body": "RC P13 failure-matrix entry.",
                            "send_at": send_at.isoformat(
                                timespec="seconds")})])[0]
    ctx.require(env["ok"] is True and env.get("executor") == "launchd",
                f"{tag}: schedule_email answered "
                f"{env.get('code') or env.get('executor')!r}")
    return env["id"], env["message_id"]


def _op_events(ctx: Context, tag: str, sid: str) -> list[dict]:
    env = _mcp_session(ctx, tag,
                       [("audit", {"operation_id": sid, "limit": 50})])[0]
    ctx.require(env["ok"] is True, f"{tag}: the audit read failed: "
                                   f"{env.get('code')} {env.get('error')}")
    return env.get("events") or []


def _fm1_kill_mid_build(ctx: Context) -> None:
    """FM1 — kill -9 mid-FTS-build: rebuildable, no partial rows served,
    the interruption visible on the doctor surface."""
    email_mcp = _venv_bin(ctx, "email-mcp")
    killed = ctx.sh([
        "/bin/sh", "-c",
        f'"{email_mcp}" fts --rebuild >/dev/null 2>&1 & P=$!; '
        f"sleep {_FM1_KILL_AFTER_S}; kill -9 $P 2>/dev/null; "
        'wait $P; echo "rc=$?"'], timeout=180)
    ctx.require("rc=137" in killed.out,
                "FM1: the fuse did not interrupt the rebuild "
                f"({(killed.out or killed.err).strip()[:80]!r}) — a build "
                "that finished demonstrates nothing")
    status = json.loads(ctx.sh([email_mcp, "fts", "--status", "--json"],
                               timeout=120, check=True).out)
    ctx.require(status.get("state") == "ready",
                "FM1: the killed build left the db unreadable (state "
                f"{status.get('state')!r}) — corrupted, not resumable")
    ctx.require(status.get("built_at") is None,
                "FM1: built_at survived the kill — the build was not "
                "interrupted mid-flight")
    searched = _mcp_session(ctx, "p13-fm1", [
        ("search_emails", {"query": "the", "limit": 5})])[0]
    ctx.require(searched["ok"] is True,
                "FM1: search died over the interrupted index: "
                f"{searched.get('code')}")
    report = json.loads(ctx.sh([email_mcp, "--doctor"], timeout=300).out)
    named = (report["checks"].get("fts") or {}).get("status") or {}
    ctx.require(named.get("built_at") is None,
                "FM1: doctor's fts check does not surface the "
                "interrupted build")
    ctx.sh([email_mcp, "fts", "--rebuild", "--limit", "200"], timeout=900,
           check=True)
    probe = ctx.write(ctx.sandbox_home / "rc-p13-fm1-probe.py", _FTS_PROBE)
    hit = json.loads(ctx.sh([_venv_bin(ctx, "python"), probe], timeout=120,
                            check=True).out)
    ctx.require(hit["rowid"] is not None and hit["rowid"] in hit["hits"],
                "FM1: the rebuilt index does not round-trip a MATCH")
    ctx.note("FM1: rebuild SIGKILLed at "
             f"{_FM1_KILL_AFTER_S:.0f}s → db opens clean with built_at "
             "null (doctor's fts status names it, soft by contract); "
             "search stayed up — hits re-enter through the live Envelope "
             "Index, so a partial index cannot serve phantom rows; "
             f"--rebuild --limit 200 → MATCH {hit['token']!r} round-trips")


def _fm4_corrupt_manifest(ctx: Context, addr: str, store: Path, stamp: str,
                          started: str) -> list[dict]:
    """FM4 — a corrupt spool manifest: coded envelope, the entry parked
    where it lies, siblings deliver unaffected."""
    good_id, good_mid = _fm_schedule(ctx, "p13-fm4-good", addr,
                                     f"rc-p13 fm4-good {stamp}")
    bad_id, bad_mid = _fm_schedule(ctx, "p13-fm4-bad", addr,
                                   f"rc-p13 fm4-bad {stamp}",
                                   offset_s=3600)
    victim = _sandbox_spool(ctx) / "pending" / f"{bad_id}.json"
    torn = '{"id": "torn mid-write'
    ctx.write(victim, torn)
    ran = _dispatch_once(ctx)
    ctx.require(ran.ok, "FM4: the pass died over the corrupt manifest: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    ctx.require((_sandbox_spool(ctx) / "sent" / f"{good_id}.json").exists()
                and good_mid in store.read_text(encoding="utf-8"),
                "FM4: the healthy sibling did not deliver — the corrupt "
                "manifest took the pass down with it")
    ctx.require(victim.read_text(encoding="utf-8") == torn,
                "FM4: the pass rewrote or moved the corrupt manifest "
                "instead of parking it untouched")
    cancel, listing = _mcp_session(ctx, "p13-fm4-wire", [
        ("cancel_scheduled", {"id": bad_id}),
        ("list_scheduled", {}),
    ])
    ctx.require(cancel["ok"] is False and bool(cancel.get("code")),
                "FM4: touching the corrupt entry must yield a coded "
                f"envelope, got {cancel.get('code') or 'ok=true'}")
    ctx.require(listing["ok"] is True and bad_id not in json.dumps(listing),
                "FM4: the listing choked on the corrupt manifest")
    ctx.note(f"FM4: {bad_id} corrupted in pending/ → cancel answers coded "
             f"{cancel['code']}, the listing skips it, and sibling "
             f"{good_id} delivered — parked in place, nothing lost")
    return [
        _mint(started, SANDBOX, "schedule", "scheduled", op=good_id,
              message_id=good_mid),
        _mint(started, SANDBOX, "schedule", "scheduled", op=bad_id,
              message_id=bad_mid),
        _mint(started, SANDBOX, "deliver", "sent", op=good_id,
              message_id=good_mid),
    ]


def _fm2_kill_mid_dispatcher(ctx: Context, addr: str, store: Path,
                             stamp: str, started: str
                             ) -> tuple[float, list[dict]]:
    """FM2 — kill -9 mid-dispatcher, BEFORE the transport handoff: the
    claim strands in sending/, doctor names it, one recovery pass
    delivers exactly once. Also measures, on that healthy delivery, the
    width of the handoff→durable window FM3 then exhibits."""
    email_mcp = _venv_bin(ctx, "email-mcp")
    sid, mid = _fm_schedule(ctx, "p13-fm2", addr, f"rc-p13 fm2 {stamp}")
    _sink(ctx, _kill_sink(store, deliver=False))
    ran = _dispatch_once(ctx)
    ctx.require(not ran.ok, "FM2: the dispatcher survived its own kill — "
                            "nothing was injected")
    claimed = _sandbox_spool(ctx) / "sending" / f"{sid}.json"
    ctx.require(claimed.exists(),
                f"FM2: no stranded claim in sending/ for {sid}")
    ctx.require(mid not in store.read_text(encoding="utf-8"),
                "FM2: bytes reached the store before the handoff — the "
                "kill landed in FM3's window, not this one")
    # Staleness is judged from the manifest's own stamps, so the
    # 10-minute recovery clock is reached by aging the stamp, not by
    # waiting it out inside an RC pass.
    manifest = json.loads(claimed.read_text(encoding="utf-8"))
    manifest["next_attempt_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=15)
    ).isoformat(timespec="seconds")
    ctx.write(claimed, json.dumps(manifest, indent=2))
    report = json.loads(ctx.sh([email_mcp, "--doctor"], timeout=300).out)
    sp = report["checks"].get("spool_plans") or {}
    ctx.require(sid in (sp.get("stranded_sending") or [])
                and "dispatcher" in str(sp.get("fix")),
                "FM2: doctor does not name the stranded claim and its "
                "one-pass recovery")
    _arm_sandbox_identity(ctx)          # restore the delivering sink
    ran = _dispatch_once(ctx)
    ctx.require(ran.ok, "FM2: the recovery pass failed: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    sent = _sandbox_spool(ctx) / "sent" / f"{sid}.json"
    ctx.require(sent.exists(), f"FM2: {sid} was not recovered to sent/")
    ctx.require(store.read_text(encoding="utf-8").count(mid) == 1,
                "FM2: the recovered delivery is not exactly-once in the "
                "store — a double send")
    events = [(e.get("event"), e.get("outcome"))
              for e in _op_events(ctx, "p13-fm2-audit", sid)]
    ctx.require(events.count(("recover", "requeued")) == 1
                and events.count(("deliver", "sent")) == 1,
                "FM2: expected one recover/requeued + one deliver/sent, "
                f"got {events}")
    window_ms = max(0.0, (sent.stat().st_mtime - store.stat().st_mtime)
                    * 1000.0)
    ctx.note("FM2: killed mid-delivery (pre-handoff) → claim stranded in "
             "sending/, doctor names it; one recovery pass requeued "
             f"(attempt consumed) and delivered {mid} exactly once — no "
             "double send")
    return window_ms, [
        _mint(started, SANDBOX, "schedule", "scheduled", op=sid,
              message_id=mid),
        _mint(started, SANDBOX, "recover", "requeued", op=sid),
        _mint(started, SANDBOX, "deliver", "sent", op=sid, message_id=mid),
    ]


def _fm3_at_most_once_window(ctx: Context, addr: str, store: Path,
                             stamp: str, started: str,
                             window_ms: float) -> list[dict]:
    """FM3 — the at-most-once window, DEMONSTRATED (D11). The kill lands
    AFTER the transport handoff: the delivery is real, the system does
    not know it, and the ledger's record of it is lost — never
    duplicated. Nothing here reconciles the exhibit away."""
    sid, mid = _fm_schedule(ctx, "p13-fm3", addr, f"rc-p13 fm3 {stamp}")
    _sink(ctx, _kill_sink(store, deliver=True))
    ran = _dispatch_once(ctx)
    ctx.require(not ran.ok, "FM3: the dispatcher survived the "
                            "post-handoff kill — nothing was injected")
    ctx.require(store.read_text(encoding="utf-8").count(mid) == 1,
                "FM3: the delivery never reached the store — the kill "
                "landed before the handoff, which is FM2's window")
    spool = _sandbox_spool(ctx)
    ctx.require((spool / "sending" / f"{sid}.json").exists()
                and not (spool / "sent" / f"{sid}.json").exists(),
                "FM3: the manifest settled — the kill missed the window")
    events = [e.get("event") for e in _op_events(ctx, "p13-fm3-audit", sid)]
    ctx.require("schedule" in events,
                "FM3: even the schedule event is missing — the ledger "
                "cannot testify about this entry at all")
    ctx.require("deliver" not in events,
                "FM3: a deliver event exists for a delivery the "
                "dispatcher never recorded — the window was reconciled "
                "away, not demonstrated (D11)")
    _arm_sandbox_identity(ctx)   # sink hygiene; the stranded claim stays
    ctx.note("FM3: the at-most-once window, DEMONSTRATED — the bytes "
             f"left the sender exactly once, yet {sid} still sits in "
             "sending/ and the ledger holds no deliver event: the RECORD "
             "of the delivery is lost, never duplicated. The window "
             "(transport handoff → durable sent/ rename) measured "
             f"{window_ms:.0f} ms on FM2's healthy delivery. Left as the "
             "exhibit: a recovery pass would consume an attempt and "
             "REDELIVER (the documented at-least-once trade) — v1.0 "
             "claims exactly-once for neither, and nothing here "
             "reconciles the gap away")
    return [_mint(started, SANDBOX, "schedule", "scheduled", op=sid,
                  message_id=mid)]


def _fm5_corrupt_fts_db(ctx: Context) -> None:
    """FM5 — corrupt FTS db: search degrades to snippet-only, doctor
    offers the rebuild, and running the offered fix heals it."""
    email_mcp = _venv_bin(ctx, "email-mcp")
    ctx.write(ctx.sandbox_home / STATE_ROOT_NAME / "fts" / "fts.db",
              "FM5: garbage where an SQLite header should be\n")
    searched = _mcp_session(ctx, "p13-fm5", [
        ("search_emails", {"query": "the", "limit": 5})])[0]
    ctx.require(searched["ok"] is True,
                "FM5: search died over the corrupt index "
                f"({searched.get('code')}) instead of degrading")
    ctx.require((searched.get("fts") or {}).get("state") == "error",
                "FM5: the envelope hides the corrupt index — state "
                f"{(searched.get('fts') or {}).get('state')!r}")
    report = json.loads(ctx.sh([email_mcp, "--doctor"], timeout=300).out)
    fts_check = report["checks"].get("fts") or {}
    ctx.require(fts_check.get("ok") is False
                and "--rebuild" in str(fts_check.get("fix")),
                "FM5: doctor does not offer the rebuild")
    ctx.sh([email_mcp, "fts", "--rebuild", "--limit", "200"], timeout=900,
           check=True)
    status = json.loads(ctx.sh([email_mcp, "fts", "--status", "--json"],
                               timeout=120, check=True).out)
    ctx.require(status.get("state") == "ready",
                "FM5: the offered rebuild did not heal the index (state "
                f"{status.get('state')!r})")
    ctx.note("FM5: corrupt fts.db → search stayed ok (snippet-only, "
             "fts.state error in the envelope), doctor offered "
             "--rebuild, and running the offered fix restored ready")


def _fm6_corrupt_ledger_line(ctx: Context) -> None:
    """FM6 — a torn ledger line is skipped-and-counted, never fatal."""
    months = sorted((ctx.sandbox_home / STATE_ROOT_NAME / "audit")
                    .glob("*.jsonl"))
    ctx.require(bool(months),
                "FM6: no ledger month to corrupt — the earlier "
                "sub-checks should have written events")
    month = months[-1]
    ctx.write(month, month.read_text(encoding="utf-8")
              + 'FM6 torn line {"half":\n')
    env = _mcp_session(ctx, "p13-fm6", [("audit", {"limit": 500})])[0]
    ctx.require(env["ok"] is True and env.get("skipped_lines", 0) >= 1,
                "FM6: the torn line was not skipped-and-counted "
                f"(skipped_lines {env.get('skipped_lines')})")
    ctx.require(bool(env.get("events")),
                "FM6: the ledger stopped serving over one torn line")
    ctx.note(f"FM6: one torn line in {month.name} → audit reports "
             f"skipped_lines {env['skipped_lines']} and keeps serving "
             f"{len(env['events'])} event(s)")


def _fm7_duplicate_apply(ctx: Context, started: str) -> list[dict]:
    """FM7 — duplicate triage_apply of one plan: single-shot per
    contract §4 — no second mutation, no second event."""
    account, mailbox, _target = _triage_selection(ctx, "p13-fm7")
    plan_env = _mcp_session(ctx, "p13-fm7-plan", [
        ("triage_plan", {"account": account, "mailbox": mailbox,
                         "limit": 3,
                         "actions": [{"action": "flag", "color": 1}]})],
        extra_env=_TTL_EXPIRED)[0]
    ctx.require(plan_env["ok"] is True,
                f"FM7: triage_plan failed: {plan_env.get('code')} "
                f"{plan_env.get('error')}")
    pid = plan_env["plan_id"]
    first, second, ledger = _mcp_session(ctx, "p13-fm7-apply", [
        ("triage_apply", {"plan_id": pid}),
        ("triage_apply", {"plan_id": pid}),
        ("audit", {"plan_id": pid, "limit": 50}),
    ])
    for label, env in (("first", first), ("second", second)):
        ctx.require(env["ok"] is False
                    and env.get("code") == "plan_expired",
                    f"FM7: the {label} apply answered "
                    f"{env.get('code') or 'ok=true'}, not the coded "
                    "single-shot refusal")
    names = sorted(e.get("event") for e in ledger.get("events") or [])
    ctx.require(ledger["ok"] is True
                and names == ["plan_create", "plan_finish"],
                f"FM7: the duplicate apply changed the ledger: {names}")
    ctx.note(f"FM7: plan {pid} applied twice → plan_expired both times, "
             "and the ledger still holds exactly plan_create + one "
             "plan_finish — no second mutation, no second event "
             "(contract §4 single-shot)")
    return [
        _mint(started, SANDBOX, "plan_create", "created", op=pid),
        _mint(started, SANDBOX, "plan_finish", "expired", op=pid),
    ]


def _fm8_chmod_000_doctor_fix(ctx: Context) -> None:
    """FM8 — chmod 000, then doctor --fix proves the repair in anger.
    The leaves, not the root: an unlistable ROOT is a refused resolution
    by design (state policy) — the repair claim is about a tree doctor
    can still see into."""
    email_mcp = _venv_bin(ctx, "email-mcp")
    root = ctx.sandbox_home / STATE_ROOT_NAME
    leaves = (root / "spool", root / "plans")
    ctx.sh(["chmod", "000", *leaves], check=True)
    report = json.loads(ctx.sh([email_mcp, "--doctor"], timeout=300).out)
    sp = report["checks"].get("spool_plans") or {}
    ctx.require(sp.get("ok") is False
                and "chmod 700" in str(sp.get("fix")),
                "FM8: doctor does not name the chmod repair")
    fixed = ctx.sh([email_mcp, "doctor", "--fix"],
                   stdin_text="doctor_fix\n", timeout=300)
    ctx.require(fixed.ok, f"FM8: doctor --fix exited {fixed.rc}: "
                          f"{(fixed.err or fixed.out).strip()[:200]}")
    loose = [str(d) for d in leaves if d.stat().st_mode & 0o777 != 0o700]
    ctx.require(not loose,
                "FM8: the repair left modes loose: " + ", ".join(loose))
    after = json.loads(ctx.sh([email_mcp, "--doctor"], timeout=300).out)
    ctx.require((after["checks"].get("spool_plans") or {}).get("ok")
                is True,
                "FM8: doctor still red after its own repair")
    ctx.note("FM8: spool/ + plans/ chmod 000 → doctor names chmod 700, "
             "doctor --fix repairs to 0700 in anger, and the check is "
             "green again")


def _fm9_token_cache_removed(ctx: Context, addr: str, stamp: str) -> None:
    """FM9 — token cache removed: a coded failure naming the cache and
    the sign-in fix, never a traceback. A second identity ON the graph
    drafts lane, never signed in, is exactly the estate a removed cache
    leaves behind — the failure path is the cache's ABSENCE, and how it
    became absent does not reach it."""
    sink = ctx.sandbox_home / "rc-outbox" / "sink.sh"
    root = ctx.sandbox_home / STATE_ROOT_NAME
    ctx.write(root / "identities.toml",
              'default = "main"\n\n[main]\n'
              f'from_addr = "{addr}"\n'
              'driver = "pipe"\n'
              f'command = "{sink}"\n'
              f'allowlist = ["{addr}"]\n\n'
              '[rcgraph]\n'
              'from_addr = "rc-p13-fm9@example.invalid"\n'
              'driver = "pipe"\n'
              f'command = "{sink}"\n'
              'drafts = "graph"\n\n'
              '[rcgraph.graph]\n'
              'tenant = "organizations"\n'
              'client_id = "00000000-0000-0000-0000-000000000000"\n')
    cache = root / "graph" / "rcgraph.token.json"
    ctx.require(not cache.exists(),
                f"FM9: a token cache already sits at {cache} — the "
                "injection needs its absence")
    refused = _mcp_session(ctx, "p13-fm9", [
        ("create_draft", {"to": addr, "subject": f"rc-p13 fm9 {stamp}",
                          "body": "This must never be filed.",
                          "from_identity": "rcgraph"})])[0]
    ctx.require(refused["ok"] is False
                and refused.get("code") == "transport_unavailable",
                "FM9: the missing cache answered "
                f"{refused.get('code') or 'ok=true'}, not the graph "
                "lane's coded refusal")
    err = refused.get("error") or ""
    ctx.require("no token cache" in err and "--login rcgraph" in err,
                "FM9: the refusal does not name the cache or the "
                "sign-in fix")
    _arm_sandbox_identity(ctx)          # restore the one-identity file
    ctx.note("FM9: absent token cache → create_draft refuses coded "
             "transport_unavailable naming the cache path and "
             "`--login rcgraph` — a fix, never a traceback")


def _fm10_store_vanishes(ctx: Context) -> None:
    """FM10 — the Mail store vanishes mid-session: the sandbox-safe
    stand-in for Mail.app quitting (same mail_unavailable class; a live
    mid-osascript quit needs the prod lane's real Mail.app)."""
    plans_dir = ctx.sandbox_home / STATE_ROOT_NAME / "plans"
    before = sorted(os.listdir(plans_dir))
    ghost = ctx.sandbox_home / "rc-p13-fm10-no-store"   # never created
    searched, planned = _mcp_session(ctx, "p13-fm10", [
        ("search_emails", {"limit": 5}),
        ("triage_plan", {"limit": 3,
                         "actions": [{"action": "flag", "color": 1}]}),
    ], extra_env={"EMAIL_MCP_MAIL_DIR": str(ghost)})
    for tool, env in (("search_emails", searched),
                      ("triage_plan", planned)):
        ctx.require(env["ok"] is False
                    and env.get("code") == "mail_unavailable",
                    f"FM10: {tool} answered "
                    f"{env.get('code') or 'ok=true'}, not the coded "
                    "mail_unavailable")
    ctx.require(sorted(os.listdir(plans_dir)) == before,
                "FM10: a plan artifact was minted against a vanished "
                "store — half-applied state")
    ctx.note("FM10: the store vanished mid-session → both tools answer "
             "coded mail_unavailable and no artifact was minted — "
             "nothing half-applied (Mail.app's quit is exhibited as the "
             "store going unreadable: the same coded class, the only "
             "injection the read-only sandbox store allows)")


@implements("P13")
def _p13_failure_matrix(ctx: Context) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    started = _now()
    addr, store = _arm_sandbox_identity(ctx)
    if ctx.dry_run:
        email_mcp = _venv_bin(ctx, "email-mcp")
        python = _venv_bin(ctx, "python")
        ctx.sh(["/bin/sh", "-c",
                f'"{email_mcp}" fts --rebuild >/dev/null 2>&1 & P=$!; '
                f"sleep {_FM1_KILL_AFTER_S}; kill -9 $P 2>/dev/null; "
                'wait $P; echo "rc=$?"'])
        ctx.sh([email_mcp, "fts", "--status", "--json"])
        ctx.sh([email_mcp, "fts", "--rebuild", "--limit", "200"])
        _mcp_session(ctx, "p13-fm4-good", [
            ("schedule_email", {"to": addr,
                                "subject": "rc-p13 fm4-good <stamp>",
                                "body": "RC P13 failure-matrix entry.",
                                "send_at": "<now>"})])
        ctx.sh([python, "-m", "email_mcp.dispatcher"])
        _mcp_session(ctx, "p13-fm4-wire", [
            ("cancel_scheduled", {"id": "<corrupt-id>"}),
            ("list_scheduled", {})])
        _sink(ctx, _kill_sink(store, deliver=False))
        ctx.sh([email_mcp, "--doctor"])
        _mcp_session(ctx, "p13-fm7-apply", [
            ("triage_apply", {"plan_id": "<plan-id>"}),
            ("triage_apply", {"plan_id": "<plan-id>"}),
            ("audit", {"plan_id": "<plan-id>", "limit": 50})])
        ctx.sh(["chmod", "000",
                ctx.sandbox_home / STATE_ROOT_NAME / "spool",
                ctx.sandbox_home / STATE_ROOT_NAME / "plans"])
        ctx.sh([email_mcp, "doctor", "--fix"], stdin_text="doctor_fix\n")
        _mcp_session(ctx, "p13-fm9", [
            ("create_draft", {"to": addr,
                              "subject": "rc-p13 fm9 <stamp>",
                              "body": "This must never be filed.",
                              "from_identity": "rcgraph"})])
        _mcp_session(ctx, "p13-fm10", [
            ("search_emails", {"limit": 5}),
            ("triage_plan", {"limit": 3,
                             "actions": [{"action": "flag",
                                          "color": 1}]})])
        _mcp_session(ctx, "p13-fm3", [
            ("schedule_email", {"to": addr,
                                "subject": "rc-p13 fm3 <stamp>",
                                "body": "RC P13 failure-matrix entry.",
                                "send_at": "<now>"})])
        return
    _fm1_kill_mid_build(ctx)
    mints = _fm4_corrupt_manifest(ctx, addr, store, stamp, started)
    window_ms, fm2 = _fm2_kill_mid_dispatcher(ctx, addr, store, stamp,
                                              started)
    mints += fm2
    _fm5_corrupt_fts_db(ctx)
    _fm6_corrupt_ledger_line(ctx)
    mints += _fm7_duplicate_apply(ctx, started)
    _fm8_chmod_000_doctor_fix(ctx)
    _fm9_token_cache_removed(ctx, addr, stamp)
    _fm10_store_vanishes(ctx)
    # FM3 is deliberately LAST: its exhibit is a stranded claim nothing
    # may tick afterwards (D11 — demonstrated, never reconciled).
    mints += _fm3_at_most_once_window(ctx, addr, store, stamp, started,
                                      window_ms)
    _record_mints(ctx, mints)
    # Leave the sandbox as found: the FM exhibits (FM4's torn pending
    # pair, FM3's stranded sending claim) have banked their evidence in
    # the report — left behind, they poison ANY later phase that sweeps
    # pending/sending (a stale FM4 manifest failed P07's armed-entries
    # check on a resumed pass, 2026-08-02). One sweep, P13's own tag
    # only, after all sub-checks are done.
    spool = _sandbox_spool(ctx)
    debris: list[Path] = []
    for st in ("pending", "sending"):
        for manifest in sorted((spool / st).glob("*.json")):
            eml = manifest.with_suffix(".eml")
            if "rc-p13 " in (eml.read_text(encoding="utf-8", errors="replace")
                             if eml.exists() else ""):
                debris += [manifest, eml]
    if debris:
        ctx.sh(["rm", "-f", *[str(p) for p in debris]], check=True)
        ctx.note(f"swept {len(debris) // 2} FM exhibit(s) out of "
                 "pending/sending — evidence lives in this report, not "
                 "the spool")
    ctx.note("FM1-FM10: every injection produced its coded surface and "
             "no mail was lost — FM3's delivery reached the store, "
             "FM2's delivered exactly once, FM4's sibling delivered, "
             "and the refusal paths mutated nothing")


# The v0.9 bytes themselves freeze the spool entry — the generator
# prints the version it ran, so a PYTHONPATH slip (the current package
# answering instead) fails the phase rather than faking the upgrade.
_V09_SCHEDULE = '''\
"""P15 generator: v0.9's own code writes the state (not a fixture)."""
import json
import sys

import email_mcp
from email_mcp import sender

entry = sender.schedule_email(to=sys.argv[1], subject=sys.argv[2],
                              body="RC P15 v0.9-frozen entry.",
                              send_at=sys.argv[3])
print(json.dumps({"version": email_mcp.__version__, "id": entry.id,
                  "message_id": entry.message_id}))
'''


def _legacy_plist(ctx: Context, worktree: Path) -> str:
    """The pre-v0.8 agent the migration retires. Plan-text correction
    (S4): v0.9's own label is ALREADY the shipped one — the com.paris
    label predates v0.8, and v0.9 merely names it in LEGACY_LABELS —
    so this file reproduces the never-migrated install that the
    legacy_launchd check exists for. File only, never bootstrapped:
    loading ANY dispatcher label from the sandbox reaches the shared
    per-user launchd domain (§2). Retiring it is not domain-free
    either — doctor --fix's legacy_launchd repair BOOTS the label OUT,
    and that bootout reaches the same shared domain no matter whose
    HOME is faked — hence the body's read-only pre-gate: proving the
    label unloaded first makes the residual bootout "No such process",
    its goal state."""
    python = _venv_bin(ctx, "python")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{_LEGACY_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>email_mcp.dispatcher</string>
    </array>
    <key>WorkingDirectory</key><string>{worktree}</string>
    <key>StartInterval</key><integer>60</integer>
</dict>
</plist>
"""


@implements("P15")
def _p15_upgrade_from_v09(ctx: Context) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    addr = _fixture_addr(ctx)
    email_mcp = _venv_bin(ctx, "email-mcp")
    python = _venv_bin(ctx, "python")
    worktree = ctx.sandbox_home / "rc-v09-src"
    v09home = ctx.sandbox_home / "rc-v09-home"
    v09root = v09home / STATE_ROOT_NAME
    store = v09home / "rc-outbox" / "delivered.mbox"
    plist = (v09home / "Library" / "LaunchAgents"
             / f"{_LEGACY_LABEL}.plist")
    # Two environments over ONE estate: the v0.9 bytes (worktree on
    # PYTHONPATH) write it, the current wheel (no PYTHONPATH) operates
    # it. Its own home keeps the upgrade story clean of P13's battered
    # sandbox estate.
    v09env = {"HOME": str(v09home), "PYTHONPATH": str(worktree)}
    cur = {"HOME": str(v09home)}

    # The §2 gate for the retirement itself, BEFORE any verb (P16's
    # shape): doctor --fix's legacy_launchd repair boots the legacy
    # label OUT, and a bootout reaches the real per-user domain no
    # matter whose HOME is faked. A loaded label here is an operator's
    # live pre-v0.8 agent — refuse while it is, so the phase's one
    # residual bootout is provably "No such process", its goal state.
    loaded = ctx.sh(["launchctl", "print",
                     f"gui/{os.getuid()}/{_LEGACY_LABEL}"], timeout=60)
    ctx.require(loaded.dry or not loaded.ok,
                f"{_LEGACY_LABEL} is loaded in the real per-user "
                "launchd domain — doctor --fix from this phase would "
                "boot the operator's live agent out. Retire it on the "
                "prod estate first, then re-run P15.")

    present = ctx.sh(["git", "cat-file", "-e", _V09_REF],
                     cwd=ctx.repo_root, timeout=60)
    ctx.require(present.ok,
                f"the v0.9 ref {_V09_REF} is not in this repository — "
                "the upgrade story needs the real v0.9 bytes, not a "
                "stand-in")
    # A crashed earlier pass may have left the worktree registered.
    ctx.sh(["git", "worktree", "remove", "--force", worktree],
           cwd=ctx.repo_root, timeout=60)
    ctx.sh(["git", "worktree", "prune"], cwd=ctx.repo_root, timeout=60)
    added = ctx.sh(["git", "worktree", "add", "--detach", worktree,
                    _V09_REF], cwd=ctx.repo_root, timeout=120)
    try:
        sink = ctx.write(v09home / "rc-outbox" / "sink.sh",
                         "#!/bin/sh\n"
                         f'exec /bin/cat >> "{store}"\n', mode=0o700)
        ctx.write(v09root / "identities.toml",
                  'default = "main"\n\n[main]\n'
                  f'from_addr = "{addr}"\n'
                  'from_name = "RC v0.9"\n'
                  'driver = "pipe"\n'
                  f'command = "{sink}"\n'
                  f'allowlist = ["{addr}"]\n')
        gen = ctx.write(ctx.sandbox_home / "rc-p15-v09-schedule.py",
                        _V09_SCHEDULE)
        frozen_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        ran = ctx.sh([python, gen, addr, f"rc-p15 v09 {stamp}",
                      frozen_at], extra_env=v09env, timeout=120)
        ctx.write(plist, _legacy_plist(ctx, worktree), mode=0o644)
        migrated = ctx.sh([email_mcp, "update"], stdin_text="update\n",
                          timeout=300, extra_env=cur)
        fixed = ctx.sh([email_mcp, "doctor", "--fix"],
                       stdin_text="doctor_fix\n", timeout=300,
                       extra_env=cur)
        delivered = ctx.sh([python, "-m", "email_mcp.dispatcher"],
                           timeout=300, extra_env=cur)
    finally:
        # Unregister the worktree pass or fail — a stale registration
        # would refuse the next pass's add.
        ctx.sh(["git", "worktree", "remove", "--force", worktree],
               cwd=ctx.repo_root, timeout=60)
    if ctx.dry_run:
        return

    ctx.require(added.ok, f"git worktree add {_V09_REF} failed: "
                          f"{(added.err or added.out).strip()[:200]}")
    ctx.require(ran.ok, "the v0.9 generator failed: "
                        f"{(ran.err or ran.out).strip()[:200]}")
    frozen = json.loads(ran.out)
    ctx.require(frozen.get("version") == "0.9.0",
                f"the generator ran version {frozen.get('version')!r} — "
                "the old code did not write the state")
    sid, mid = frozen["id"], frozen["message_id"]
    ctx.require(migrated.ok,
                f"`email-mcp update` exited {migrated.rc}: "
                f"{(migrated.err or migrated.out).strip()[:200]}")
    meta = json.loads((v09root / "meta.json").read_text(encoding="utf-8"))
    ctx.require(isinstance(meta.get("state_version"), int),
                "update did not stamp meta.json — no migration ran")
    ctx.require(fixed.ok, f"doctor --fix exited {fixed.rc}: "
                          f"{(fixed.err or fixed.out).strip()[:200]}")
    ctx.require(not plist.exists(),
                f"the legacy plist survived doctor --fix: {plist}")
    ctx.require((v09root / "spool" / "pending").stat().st_mode & 0o777
                == 0o700,
                "doctor --fix left v0.9's spool subdirs loose")
    ctx.require(delivered.ok,
                f"the dispatcher pass exited {delivered.rc}: "
                f"{(delivered.err or delivered.out).strip()[:200]}")
    sent = v09root / "spool" / "sent" / f"{sid}.json"
    ctx.require(sent.exists(),
                f"the v0.9-frozen entry {sid} did not deliver")
    # Judged after the pass, so the old-shape evidence is read from the
    # settled manifest — the delivery moved it out of pending/.
    manifest = json.loads(sent.read_text(encoding="utf-8"))
    ctx.require(manifest.get("message_id") == mid
                and mid in store.read_text(encoding="utf-8"),
                f"{sid} delivered but the frozen Message-ID does not "
                "match what reached the store")
    ctx.note(f"v0.9 bytes (worktree @ {_V09_REF}, __version__ 0.9.0) "
             f"wrote the estate: a v0.9 config, frozen spool entry {sid} "
             f"(manifest keys: {', '.join(sorted(manifest))}), and the "
             "legacy plist standing in for the never-migrated pre-v0.8 "
             "install (v0.9's own label is already the shipped one — "
             "plan-text correction)")
    ctx.note("current wheel operated it: update stamped state_version "
             f"{meta['state_version']}, doctor --fix retired "
             f"{_LEGACY_LABEL} (its residual bootout DOES reach the "
             "shared per-user domain — the read-only pre-gate proved "
             "the label unloaded there, making it a goal-state no-op) "
             "and pulled v0.9's 0755 spool subdirs to 0700, and one "
             f"dispatcher pass delivered {mid} from the v0.9-frozen "
             "entry")


@implements("P16")
def _p16_uninstall_purge(ctx: Context) -> None:
    email_mcp = _venv_bin(ctx, "email-mcp")
    root = ctx.sandbox_home / STATE_ROOT_NAME
    agents = ctx.sandbox_home / "Library" / "LaunchAgents"
    logs = ctx.sandbox_home / "Library" / "Logs"
    cache = root / "graph" / "rc-sandbox.token.json"
    if ctx.dry_run:
        ctx.write(cache, '{"refresh_token": "rc-fixture"}\n')
        ctx.sh([email_mcp, "uninstall", "--yes"])
        ctx.sh([email_mcp, "uninstall", "--purge", "--yes"])
        return
    # The §2 gate, BEFORE the verb runs: uninstall boots out every
    # product plist it finds, and those labels live in the SHARED
    # per-user launchd domain — a product plist in the sandbox would
    # aim the bootout at the operator's real agents. The sandbox never
    # installs one (P07 runs under an RC-owned label), so the honest
    # sandbox reading of "removes the agents" is that the plan names no
    # launchd action at all; the real agents' story is P17's, on prod.
    strays = [f"{label}.plist" for label in PROD_LABELS
              if (agents / f"{label}.plist").exists()]
    ctx.require(not strays,
                "product agent plist(s) sit in the sandbox LaunchAgents "
                "— uninstall would boot their labels out of the shared "
                "per-user domain and displace the operator's agents: "
                + ", ".join(strays))
    # A cache for the sweep to remove — minted here because the sandbox
    # never signs into a real tenant; the claim under test is REMOVAL.
    ctx.write(cache, '{"refresh_token": "rc-fixture"}\n')
    kept = ctx.sh([email_mcp, "uninstall", "--yes"], timeout=300,
                  check=True)
    ctx.require(not cache.exists(),
                "the token cache survived the uninstall sweep")
    ctx.require(root.is_dir() and "state kept" in kept.out,
                "the no-purge uninstall did not keep the state tree")
    purged = ctx.sh([email_mcp, "uninstall", "--purge", "--yes"],
                    timeout=300, check=True)
    ctx.require(not root.exists(),
                f"the state tree survived the purge: {root}")
    leftover = sorted(p.name for p in logs.glob("email-mcp*.log*")) \
        if logs.is_dir() else []
    ctx.require(not leftover,
                "log files survived the purge: " + ", ".join(leftover))
    ctx.require("boot out launchd agent" not in kept.out + purged.out,
                "uninstall planned a launchd action from the sandbox — "
                "the shared label space (§2)")
    ctx.note("uninstall --yes removed the planted token cache and kept "
             "the tree; --purge removed the tree and the sandbox logs; "
             "no launchd action was planned (no product plists in the "
             "sandbox — the agents' story is P17's, on the prod lane)")
    ctx.note("the byte-identical claim for the REAL estate is judged by "
             "the runner's strict sentinel, immediately after this body")


@implements("P18")
def _p18_fresh_account_walk(ctx: Context) -> tuple[str, str]:
    """Once-ever, and the body IS the protocol: the runner stages
    nothing, because a fresh account only proves the roadmap's claim
    when a human walks it cold."""
    spec = PLAN_BY_ID["P18"]
    steps = (
        "The roadmap's lifecycle claim, tested the only honest way. "
        "Recorded ONCE ever — a pass here is skipped by later runs.\n"
        "1. Create a brand-new macOS user account (System Settings → "
        "Users & Groups), log into it, and START THE STOPWATCH at "
        "first login.\n"
        "2. Install with pipx: the published package, or the wheel this "
        "RC built. Nothing else crosses over from your account.\n"
        "3. Run `email-mcp setup` and follow ONLY what it prints — "
        "permissions (Full Disk Access when doctor names it), the MCP "
        "client config, the smoke test. Consulting anything the product "
        "did not print is archaeology and fails the walk.\n"
        "4. Register the printed config in an MCP client and drive one "
        "search_emails + one get_email in the new account's session.\n"
        "5. STOP THE STOPWATCH at the first successful read — the walk "
        "passes under 15 minutes.\n"
        "Answer `pass: <minutes> min, <what needed explaining>` or "
        "`fail: <where the archaeology started>`.")
    return ctx.manual(replace(spec, acceptance=steps))


# --------------------------------------------------------------------- #
# CLI                                                                    #
# --------------------------------------------------------------------- #


def _default_mail_dir(explicit: str | None) -> Path | None:
    """The Mail store the sandbox borrows, READ-ONLY (plan §1).

    Defaulted rather than demanded: the plan says the sandbox's one
    borrowing from reality is the mail, so a bare `--execute` that
    attaches nothing is not a stricter run — it is a run whose every
    read phase is testing an empty Mac (first live pass, 2026-08-02:
    P03 failed on "Library/Mail does not exist" inside the sandbox
    HOME). Resolution failure is not fatal here: the phases that need a
    store say so themselves.

    An explicit path may be either the versioned store root (V10) or
    the ~/Library/Mail above it — the plan's own §4 example passes the
    latter, while every consumer appends MailData/Envelope Index to
    this value verbatim (found live 2026-08-03: P03's sandbox doctor
    read ~/Library/Mail/MailData). Same pick as config.mail_dir():
    highest V<N> wins.
    """
    if explicit:
        root = Path(explicit).expanduser()
        if not (root / "MailData" / "Envelope Index").exists():
            versioned = [p for p in root.glob("V*")
                         if p.is_dir() and re.fullmatch(r"V\d+", p.name)]
            if versioned:
                return max(versioned, key=lambda p: int(p.name[1:]))
        return root
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from email_mcp.config import mail_dir
        return mail_dir()
    except Exception:      # noqa: BLE001 — no store on this Mac; phases judge
        return None


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
                   help="Mail store attached read-only in the sandbox lane "
                        "(default: the operator's own, resolved live)")
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
                  mail_dir=_default_mail_dir(args.mail_dir),
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
