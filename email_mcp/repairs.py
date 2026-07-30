"""Safe self-repairs behind ``doctor --fix``: a frozen whitelist registry.

Every repair executes a remedy the doctor already prints — nothing here
invents new mutations. Each Repair pairs a pure ``detect()`` probe (never
mutates, so a dry run truly touches nothing) with an ``apply()`` that
performs the fix. The registry is a CLOSED set by design and a test pins
its ids: repairs never run auth flows, never build or rebuild derived
state (the FTS index), never install a launchd agent that was never
installed, and never delete user data — the audit-path repair renames the
offending file aside with a UTC stamp, exactly what the doctor's remedy
says to do by hand.

``run_fixes()`` drives one pass under a single fresh operation id: detect
each repair in registry order, apply what is broken, and record ONE
``doctor_fix`` audit event per applied/failed repair (an additive event
type, contract §8). Apply failures are caught and reported, never raised
— the fixer must work precisely when things are broken.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import audit, config, dispatcher, ids, plans, spool
from .log import get_logger

_log = get_logger()


@dataclass(frozen=True)
class Repair:
    """One whitelisted self-repair.

    ``detect()`` returns a one-line finding when the fault is present,
    None when healthy — and never mutates anything. ``apply()`` executes
    the remedy and returns a one-line account of what it did; anything it
    raises is caught by run_fixes and reported, never propagated.
    """

    id: str
    heals: str
    detect: Callable[[], str | None]
    apply: Callable[[], str]


# --------------------------------------------------------------------- #
# side-effect-free path resolution                                       #
# --------------------------------------------------------------------- #
# config.spool_dir()/plans_dir()/graph_dir() mkdir+chmod on every call,
# so detect() resolves the same env-driven paths WITHOUT touching the
# filesystem (the purity rule doctor's _graph_token_dir follows).


def _state_root() -> Path:
    return Path.home() / ".email-mcp"


def _env_dir(var: str, default: Path) -> Path:
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser() if raw else default


def _spool_root() -> Path:
    return _env_dir("EMAIL_MCP_SPOOL_DIR", _state_root() / "spool")


def _plans_root() -> Path:
    return _env_dir("EMAIL_MCP_PLANS_DIR", _state_root() / "plans")


def _graph_root() -> Path:
    return _env_dir("EMAIL_MCP_GRAPH_DIR", _state_root() / "graph")


def _degenerate(path: Path) -> bool:
    """True when a resolved state path IS the home directory or an
    ancestor of it (an EMAIL_MCP_*_DIR pointed at ``$HOME`` or ``/``).
    Repairs never manage such a path: mkdir would scatter spool
    subdirectories through the home directory and chmod 0700 would
    retighten the home directory itself — neither is this fixer's to do.
    """
    home = Path.home()
    return path == home or path in home.parents


def _state_dirs() -> list[Path]:
    """Every directory the state tree owns, root first (creation order).

    The FTS dir is DERIVED state: it is listed only when an index file
    already exists (i.e. the dir is live) — repairs never create it, and
    ``python -m email_mcp.fts --build`` remains the only builder.
    Degenerate env overrides (``$HOME`` and above) are dropped: see
    :func:`_degenerate`.
    """
    spool_root = _spool_root()
    dirs = [_state_root(), spool_root]
    if not _degenerate(spool_root):
        # A spool override at $HOME must not drag its five state
        # subdirectories into the home directory either.
        dirs += [spool_root / s for s in spool.STATES]
    dirs += [_plans_root(), config.audit_dir(create=False), _graph_root()]
    from . import fts  # lazy, like doctor's checks
    if fts.db_path().exists():
        dirs.append(config.fts_dir(create=False))
    return [d for d in dict.fromkeys(dirs) if not _degenerate(d)]


# --------------------------------------------------------------------- #
# repairs 1-3: state tree exists, dirs 0700, files 0600                  #
# --------------------------------------------------------------------- #


def _tree_links() -> set[Path]:
    """Symlinks squatting on managed state-dir paths. The path AT a link
    and every path BEHIND one is off-limits to every repair: mkdir and
    chmod both resolve through the redirection and would act on whatever
    the link points at (an arbitrary victim directory)."""
    return {d for d in _state_dirs() if d.is_symlink()}


def _off_limits(path: Path, links: set[Path]) -> bool:
    return any(link in path.parents for link in links)


def _missing_state_dirs() -> list[Path]:
    links = _tree_links()
    return [d for d in _state_dirs()
            if not d.exists() and not d.is_symlink()
            and not _off_limits(d, links)]


def _detect_state_dir_missing() -> str | None:
    # exists() (not is_dir): a file squatting on a dir path is NOT
    # "missing" — that is audit_path_is_file's fault to heal, and mkdir
    # over it could only fail. A dangling symlink is not "missing"
    # either: mkdir there would fail, and healing it means following a
    # redirection.
    missing = _missing_state_dirs()
    if not missing:
        return None
    return "missing state dir(s): " + ", ".join(str(d) for d in missing)


def _apply_state_dir_missing() -> str:
    created: list[str] = []
    for d in _missing_state_dirs():
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        # chmod the dir itself only: an env-overridden dir's parents are
        # not ours to touch (config.audit_dir's rule).
        d.chmod(0o700)
        created.append(str(d))
    return "created 0700: " + ", ".join(created)


def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def _dirs_not_0700() -> list[Path]:
    """Real state dirs (not symlinks, not behind one) with a wrong mode
    — the only ones a chmod may touch. chmod FOLLOWS symlinks, so a link
    squatting on a state-dir path (or on a parent) would redirect the
    fix onto an arbitrary victim directory; leaf symlinks are flagged
    (below), never chmodded through."""
    links = _tree_links()
    return [d for d in _state_dirs()
            if d.is_dir() and not d.is_symlink()
            and not _off_limits(d, links) and _mode(d) != 0o700]


def _symlinked_bad_dirs() -> list[Path]:
    """Symlinks squatting on state-dir paths whose TARGET mode is wrong —
    exactly what the pre-fence code would have chmodded through."""
    return [d for d in _state_dirs()
            if d.is_symlink() and d.is_dir() and _mode(d) != 0o700]


def _detect_state_dir_perms() -> str | None:
    bad = _dirs_not_0700()
    links = _symlinked_bad_dirs()
    if not bad and not links:
        return None
    parts = []
    if bad:
        parts.append("state dir(s) not 0700: " + ", ".join(
            f"{d} ({_mode(d):o})" for d in bad))
    if links:
        parts.append(
            "symlinked state dir(s) with target not 0700 (never chmod "
            "through a link): " + ", ".join(str(d) for d in links))
    return "; ".join(parts)


def _apply_state_dir_perms() -> str:
    fixed = []
    for d in _dirs_not_0700():
        d.chmod(0o700)
        fixed.append(str(d))
    links = _symlinked_bad_dirs()
    if links:
        done = f" (did chmod 0700: {', '.join(fixed)})" if fixed else ""
        raise RuntimeError(  # caught by run_fixes → failed[], flagged
            "refusing to chmod through symlinked state dir(s): "
            + ", ".join(str(d) for d in links)
            + " — remove the link or fix the target yourself" + done)
    return "chmod 0700: " + ", ".join(fixed)


def _state_files() -> list[Path]:
    """The secret-adjacent files the tree owns: the identities TOML,
    ledger months, Graph token caches. All must be 0600. A symlinked
    ROOT is never descended into — the files behind it belong to
    whatever the link points at, and chmodding them would reach through
    the redirection (the symlinked-dir repair flags the root itself)."""
    links = _tree_links()
    files: list[Path] = []
    ident = config.identities_file()
    if ident.is_file() and not _off_limits(ident, links):
        files.append(ident)
    for root, pattern in ((config.audit_dir(create=False), "*.jsonl"),
                          (_graph_root(), "*.token.json")):
        if (root.is_dir() and not root.is_symlink()
                and not _off_limits(root, links)):
            files += sorted(root.glob(pattern))
    return files


def _files_not_0600() -> list[Path]:
    """Real (non-symlink) files only — chmod follows symlinks, and a
    link squatting on identities.toml (or planted in the ledger) must
    never redirect the fix onto a victim file."""
    return [f for f in _state_files()
            if not f.is_symlink() and _mode(f) != 0o600]


def _symlinked_bad_files() -> list[Path]:
    return [f for f in _state_files()
            if f.is_symlink() and _mode(f) != 0o600]


def _detect_state_file_perms() -> str | None:
    bad = _files_not_0600()
    links = _symlinked_bad_files()
    if not bad and not links:
        return None
    parts = []
    if bad:
        parts.append("state file(s) not 0600: " + ", ".join(
            f"{f} ({_mode(f):o})" for f in bad))
    if links:
        parts.append(
            "symlinked state file(s) with target not 0600 (never chmod "
            "through a link): " + ", ".join(str(f) for f in links))
    return "; ".join(parts)


def _apply_state_file_perms() -> str:
    fixed = []
    for f in _files_not_0600():
        f.chmod(0o600)
        fixed.append(str(f))
    links = _symlinked_bad_files()
    if links:
        done = f" (did chmod 0600: {', '.join(fixed)})" if fixed else ""
        raise RuntimeError(  # caught by run_fixes → failed[], flagged
            "refusing to chmod through symlinked state file(s): "
            + ", ".join(str(f) for f in links)
            + " — remove the link or fix the target yourself" + done)
    return "chmod 0600: " + ", ".join(fixed)


# --------------------------------------------------------------------- #
# repair 4: a regular file squatting on the audit ledger directory       #
# --------------------------------------------------------------------- #


def _detect_audit_path_is_file() -> str | None:
    root = config.audit_dir(create=False)
    if root.exists() and not root.is_dir():
        return (f"{root} is a regular file, not the ledger directory — "
                "every audit event is being dropped")
    return None


def _apply_audit_path_is_file() -> str:
    """The doctor's own printed remedy (move it aside), executed — with a
    UTC stamp so repeated repairs can never overwrite an earlier .bak
    (rename-aside, NEVER deletion). emit() recreates the directory on the
    next event — including this repair's own doctor_fix."""
    root = config.audit_dir(create=False)
    stamp = ids.utcnow().strftime("%Y%m%dT%H%M%SZ")
    aside = root.with_name(f"{root.name}.bak-{stamp}")
    n = 1
    while aside.exists():
        aside = root.with_name(f"{root.name}.bak-{stamp}-{n}")
        n += 1
    root.rename(aside)
    return f"renamed aside: {root} -> {aside}"


# --------------------------------------------------------------------- #
# repairs 5-6: launchd agents (legacy bootout; stale re-bootstrap)       #
# --------------------------------------------------------------------- #


def _service_loaded(label: str) -> bool | None:
    """True/False when launchctl answered; None when it cannot be asked
    (absent binary, timeout) — an unknown must never trigger a repair."""
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=10)
    except Exception:
        return None
    return proc.returncode == 0


def _detect_legacy_launchd() -> str | None:
    agents = dispatcher._plist_path().parent
    found = [
        label for label in dispatcher.LEGACY_LABELS
        if (agents / f"{label}.plist").exists() or _service_loaded(label)
    ]
    if not found:
        return None
    return "legacy launchd agent(s) present: " + ", ".join(found)


def _apply_legacy_launchd() -> str:
    removed = dispatcher.remove_legacy_plists()
    if not removed:
        return "no legacy agent found (already gone)"
    return "booted out + removed: " + ", ".join(removed)


def _plist_render_pure() -> bool:
    """dispatcher._plist_content() resolves its log path through
    config.spool_dir(), which mkdirs and chmods on every call. Render
    only when those side effects are provably no-ops — detect() must
    stay pure. A non-dry run heals the tree (repairs 1-2) BEFORE this
    detect runs, so the content comparison is only ever skipped inside
    a dry run over a broken tree — where the load-state probe below
    still detects."""
    spool_root = _spool_root()
    if not (spool_root.is_dir() and _mode(spool_root) == 0o700):
        return False
    parent = spool_root.parent
    if not (parent.is_dir() and _mode(parent) == 0o700):
        return False
    return all((spool_root / s).is_dir() for s in spool.STATES)


def _detect_launchd_stale() -> str | None:
    plist = dispatcher._plist_path()
    if not plist.exists():
        # Never installed → nothing to repair. Installing an agent the
        # user never asked for is setup's decision, not a repair.
        return None
    try:
        current = plist.read_text()
    except OSError as e:
        return f"{plist} unreadable ({e}) — needs a re-render"
    if _plist_render_pure() and current != dispatcher._plist_content():
        return (f"{plist} differs from a fresh render "
                "(stale interpreter/PATH baked in)")
    if _service_loaded(dispatcher.LAUNCHD_LABEL) is False:
        return (f"{dispatcher.LAUNCHD_LABEL} plist present but the "
                "service is not loaded")
    return None


def _apply_launchd_stale() -> str:
    msg = dispatcher.install_launchd()  # re-render + bootout + bootstrap
    if "bootstrap failed" in msg:
        raise RuntimeError(msg)  # caught by run_fixes → failed[]
    return msg


# --------------------------------------------------------------------- #
# repairs 7-8: stranded sending/ claims; stale plan claims               #
# --------------------------------------------------------------------- #


def _detect_stranded_sending() -> str | None:
    sending = _spool_root() / "sending"
    if not sending.is_dir():
        return None
    now = ids.utcnow()
    stranded: list[str] = []
    for manifest in sorted(sending.glob("*.json")):
        try:
            data = json.loads(manifest.read_bytes())
        except (OSError, ValueError):
            continue  # half-written or foreign file — not ours to judge
        if not isinstance(data, dict):
            continue
        ref = (dispatcher._parse_iso(data.get("next_attempt_at"))
               or dispatcher._parse_iso(data.get("send_at")))
        # Unparseable reference stamp reads as infinitely stale — the
        # same rule _recover_stranded applies.
        age_min = (now - ref).total_seconds() / 60 if ref else float("inf")
        if age_min >= dispatcher.STALE_SENDING_MINUTES:
            stranded.append(manifest.stem)
    if not stranded:
        return None
    return (f"{len(stranded)} stranded claim(s) in sending/: "
            + ", ".join(stranded))


def _apply_stranded_sending() -> str:
    requeued = dispatcher.recover_stranded(ids.utcnow())
    if requeued:
        return "recovery pass: requeued " + ", ".join(requeued)
    return "recovery pass: stranded claim(s) finalised (retries exhausted)"


def _detect_stale_plan_claims() -> str | None:
    root = _plans_root()
    if not root.is_dir():
        return None
    cutoff = ids.utcnow() - timedelta(
        seconds=2 * config.triage_ttl_seconds())
    stale: list[str] = []
    for path in sorted(root.glob("*.json.applying")):
        try:
            mtime = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            stale.append(path.name[: -len(".json.applying")])
    if not stale:
        return None
    return (f"{len(stale)} stale apply claim(s) in plans/: "
            + ", ".join(stale))


def _apply_stale_plan_claims() -> str:
    removed = plans.gc()
    return (f"plans.gc(): stale claim(s) finalised as failed "
            f"(plan_finish on record); {removed} aged file(s) removed")


# --------------------------------------------------------------------- #
# the FROZEN registry                                                    #
# --------------------------------------------------------------------- #
# A closed whitelist: tests pin these ids exactly. Growing it is a
# deliberate act — and NEVER toward auth flows, FTS builds, installing
# never-installed agents, or deleting user data.

REPAIRS: tuple[Repair, ...] = (
    Repair("state_dir_missing",
           "recreate the ~/.email-mcp state tree, 0700",
           _detect_state_dir_missing, _apply_state_dir_missing),
    Repair("state_dir_perms",
           "tighten state directory modes to 0700",
           _detect_state_dir_perms, _apply_state_dir_perms),
    Repair("state_file_perms",
           "tighten identities/ledger/token file modes to 0600",
           _detect_state_file_perms, _apply_state_file_perms),
    Repair("audit_path_is_file",
           "rename aside a file squatting on the audit ledger dir",
           _detect_audit_path_is_file, _apply_audit_path_is_file),
    Repair("legacy_launchd",
           "boot out + remove pre-v0.8 dispatcher launchd agents",
           _detect_legacy_launchd, _apply_legacy_launchd),
    Repair("launchd_stale",
           "re-render + re-bootstrap a stale installed dispatcher agent",
           _detect_launchd_stale, _apply_launchd_stale),
    Repair("stranded_sending",
           "recover delivery claims stranded in spool/sending",
           _detect_stranded_sending, _apply_stranded_sending),
    Repair("stale_plan_claims",
           "finalise stale triage apply claims via plans.gc()",
           _detect_stale_plan_claims, _apply_stale_plan_claims),
)


def run_fixes(*, dry_run: bool = False) -> dict:
    """One repair pass over the registry, in order.

    Returns ``{op, applied[], skipped[], failed[]}`` — one fresh
    operation id threads every ``doctor_fix`` event of the run. A healthy
    repair lands in skipped ("healthy"); with ``dry_run`` a detected one
    lands in skipped too ("dry-run: would fix", finding attached) and
    NOTHING is touched — no fix, no audit event. Apply (and detect)
    exceptions are caught and reported in failed[]; run_fixes itself
    never raises past a repair.
    """
    audit.set_process("cli")
    op = ids.new_id()
    applied: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for repair in REPAIRS:
        try:
            finding = repair.detect()
        except Exception as e:  # noqa: BLE001 — a broken probe is a
            # finding about the fixer, never a crashed fix pass
            _log.exception("doctor_fix: %s detect crashed", repair.id)
            msg = f"detect crashed: {type(e).__name__}: {e}"
            failed.append({"repair": repair.id, "finding": None,
                           "error": msg})
            if not dry_run:
                audit.emit("doctor_fix", outcome="failed", operation_id=op,
                           detail={"repair": repair.id, "finding": None,
                                   "action": msg})
            continue
        if finding is None:
            skipped.append({"repair": repair.id, "reason": "healthy"})
            continue
        if dry_run:
            skipped.append({"repair": repair.id,
                            "reason": "dry-run: would fix",
                            "finding": finding})
            continue
        try:
            action = repair.apply()
        except Exception as e:  # noqa: BLE001 — the fixer must keep
            # working precisely when things are broken
            _log.exception("doctor_fix: %s apply failed", repair.id)
            msg = f"{type(e).__name__}: {e}"
            failed.append({"repair": repair.id, "finding": finding,
                           "error": msg})
            audit.emit("doctor_fix", outcome="failed", operation_id=op,
                       detail={"repair": repair.id, "finding": finding,
                               "action": f"failed: {msg}"})
            continue
        _log.info("doctor_fix: %s — %s", repair.id, action)
        applied.append({"repair": repair.id, "finding": finding,
                        "action": action})
        audit.emit("doctor_fix", outcome="fixed", operation_id=op,
                   detail={"repair": repair.id, "finding": finding,
                           "action": action})
    return {"op": op, "applied": applied, "skipped": skipped,
            "failed": failed}
