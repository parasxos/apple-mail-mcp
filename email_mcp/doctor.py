"""Environment diagnostics: every permission, path and transport the MCP
needs, checked in one pass with remediation hints.

`run()` returns {ok, read_only, checks: {name: {ok, detail, fix?, ...}}} —
`ok` is the AND of every check; `fix` appears only when there is a
concrete next step (a Settings pane or a command). Since v0.11 the audit
ledger check is the tenth member of `checks` (the fold this module's
v0.10 docstring scheduled for the outputSchema freeze; contract §1 row 10
declares `{ok, read_only, checks}`); the top-level `audit` key remains as
a deprecated mirror of checks["audit"] so v0.10 readers keep working —
kept additively (§8), to be dropped no earlier than v2.
Checks never mutate anything:
transports are healthchecked but never bootstrapped, the FTS index is
statted but never created, and the osascript probes are benign reads.

Surfaced as the `doctor` MCP tool (registered in BOTH normal and READ_ONLY
modes) and as ``python -m email_mcp.server --doctor``; the old
``--transport-check`` flag lives on as a deprecated alias for the
`transports` check alone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, identities
from .log import get_logger
from .transports import SendError, get_transport

_log = get_logger()

_OSA_TIMEOUT = 15.0

# osascript error codes. Deliberately duplicated minimally (third small
# copy alongside server._run_mail_check_for_new and triage._osa_error_code)
# rather than refactored mid-stage — unifying the three is a post-v0.8
# cleanup, and the refresh path's tests must keep binding server's copy.
_OSA_ERR_NO_APP = -1728
_OSA_ERR_NOT_AUTHORIZED = -1743
# Accessibility denials observed live from the System Events UI path.
_OSA_ERR_ACCESSIBILITY = (-1719, -25211)

_FDA_FIX = (
    "Grant Full Disk Access to the app running this server: System Settings "
    "→ Privacy & Security → Full Disk Access, then restart it."
)
_AUTOMATION_FIX = (
    "Authorise Mail.app automation for the app running this server: System "
    "Settings → Privacy & Security → Automation → <your terminal> → Mail."
)
_ACCESSIBILITY_FIX = (
    "Grant Accessibility permission to the app running this server: System "
    "Settings → Privacy & Security → Accessibility."
)
_ACCESSIBILITY_NOTE = "only needed for mailbox_delete's UI fallback"


def _osascript(line: str, timeout: float = _OSA_TIMEOUT) -> subprocess.CompletedProcess:
    """THE seam: tests monkeypatch this one symbol (mirrors triage's)."""
    return subprocess.run(
        ["osascript", "-e", line],
        capture_output=True, text=True, timeout=timeout,
    )


def _osa_error_code(stderr: str) -> int | None:
    """osascript stderr looks like: '...: execution error: ... (-1743)'."""
    stderr = (stderr or "").strip()
    if "(-" in stderr and stderr.endswith(")"):
        try:
            return int(stderr.rsplit("(", 1)[1].rstrip(")"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------- #
# checks — each returns {ok, detail, fix?, ...extras}                     #
# ---------------------------------------------------------------------- #


def check_mail_store() -> dict:
    """Can we resolve and read the Envelope Index? Failure = missing Mail
    setup or (far more often) missing Full Disk Access."""
    try:
        base = config.mail_dir()
    except FileNotFoundError as e:
        return {"ok": False, "detail": str(e), "fix": _FDA_FIX}
    index = base / "MailData" / "Envelope Index"
    if not index.exists():
        return {"ok": False,
                "detail": f"Envelope Index not found at {index}.",
                "fix": _FDA_FIX}
    uri = "file:" + urllib.parse.quote(str(index)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"ok": False,
                "detail": f"cannot read {index}: {e}",
                "fix": _FDA_FIX}
    return {"ok": True, "detail": f"{total} messages in {index}"}


def check_automation() -> dict:
    """Benign AppleScript read against Mail.app — the permission behind
    refresh_mail, triage_apply and mailbox_create/delete."""
    probe = 'tell application "Mail" to get name'
    try:
        proc = _osascript(probe)
    except FileNotFoundError:
        return {"ok": False,
                "detail": "osascript not found — this MCP is macOS-only."}
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "detail": f"osascript timed out after {_OSA_TIMEOUT:g}s "
                          "(Mail.app unresponsive?)."}
    if proc.returncode == 0:
        return {"ok": True, "detail": "Mail.app reachable via AppleScript."}
    code = _osa_error_code(proc.stderr)
    if code == _OSA_ERR_NOT_AUTHORIZED:
        return {"ok": False, "error_code": code,
                "detail": "Mail.app automation is not authorised for this "
                          "process.",
                "fix": _AUTOMATION_FIX}
    if code == _OSA_ERR_NO_APP:
        return {"ok": False, "error_code": code,
                "detail": "Mail.app is not installed or not reachable via "
                          "AppleScript."}
    out: dict = {"ok": False,
                 "detail": (proc.stderr or "").strip()[:200]
                 or f"osascript failed with exit code {proc.returncode}."}
    if code is not None:
        out["error_code"] = code
    return out


def check_accessibility() -> dict:
    """Is UI scripting (System Events) available? Advisory in practice —
    it is only needed for mailbox_delete's UI fallback."""
    probe = 'tell application "System Events" to get UI elements enabled'
    try:
        proc = _osascript(probe)
    except FileNotFoundError:
        return {"ok": False,
                "detail": "osascript not found — this MCP is macOS-only."}
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "detail": f"System Events probe timed out after "
                          f"{_OSA_TIMEOUT:g}s ({_ACCESSIBILITY_NOTE})."}
    if proc.returncode != 0:
        code = _osa_error_code(proc.stderr)
        out = {"ok": False,
               "detail": f"System Events not scriptable "
                         f"({(proc.stderr or '').strip()[:150]}; "
                         f"{_ACCESSIBILITY_NOTE}).",
               "fix": _ACCESSIBILITY_FIX}
        if code is not None:
            out["error_code"] = code
            if code == _OSA_ERR_NOT_AUTHORIZED:
                out["fix"] = (_ACCESSIBILITY_FIX +
                              " Also authorise System Events under "
                              "Automation.")
        return out
    if (proc.stdout or "").strip().lower() == "true":
        return {"ok": True,
                "detail": f"UI scripting enabled ({_ACCESSIBILITY_NOTE})."}
    return {"ok": False,
            "detail": f"UI scripting not authorised for this process "
                      f"({_ACCESSIBILITY_NOTE}).",
            "fix": _ACCESSIBILITY_FIX}


def _identities_mode_note() -> str | None:
    """A loose mode on the identities file, wherever it lives.

    `doctor --fix` only re-modes it inside a directory we manage — but both
    the security document and repairs.py's own docstring promised that
    doctor would still REPORT a loose mode on a file named by
    EMAIL_MCP_IDENTITIES anywhere else, and nothing did. A user pointing
    that variable at a world-readable file holding SMTP credentials got no
    signal at all.
    """
    path = config.identities_file()
    try:
        if not path.is_file():
            return None
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    if mode == 0o600:
        return None
    return (f"{path} is mode {mode:o} — it holds sending credentials and "
            "wants 600")


def check_identities() -> dict:
    """Does the identities file parse? The load error is surfaced verbatim
    — it already names the file and the offending key."""
    mode_note = _identities_mode_note()
    try:
        idents, default = identities.load()
    except SendError as e:  # IdentityError subclasses SendError
        # Verbatim, deliberately: the loader's message already names the
        # file and the offending key, and a test pins that it reaches the
        # user unaltered. A malformed file is the headline; its mode is
        # reported on the parse-success path below.
        return {"ok": False, "detail": str(e),
                "fix": f"edit {config.identities_file()}"}
    detail = (f"{len(idents)} identity(ies): "
              f"{', '.join(sorted(idents))}; default {default!r}")
    if mode_note:
        # Report, do not repair: a file the user merely NAMED is not ours
        # to re-mode (repairs._managed_identities_file), but staying silent
        # about credentials at 644 is the gap that pairing left open.
        return {"ok": False, "detail": f"{detail}; {mode_note}",
                "fix": f"chmod 600 {config.identities_file()}"}
    return {"ok": True, "detail": detail}


def check_transports() -> dict:
    """Healthcheck every identity's transport independently — one broken
    identity must not hide the others. Never bootstraps anything;
    ok:false can be a state (a cold SSH socket), not necessarily a bug.

    This is the old ``--transport-check`` loop, moved here; the flag is now
    a deprecated alias that prints exactly this check.
    """
    try:
        idents, default = identities.load()
    except SendError as e:
        return {"ok": False,
                "detail": f"identities unreadable: {e}",
                "identities": {}}
    report: dict[str, dict] = {}
    all_ok = True
    for name in sorted(idents):
        ident = idents[name]
        try:
            result = get_transport(ident).healthcheck()
        except SendError as e:
            result = {"ok": False, "error": str(e)}
        result["from_addr"] = ident.from_addr
        all_ok = all_ok and bool(result.get("ok"))
        report[name] = result
    healthy = sum(1 for r in report.values() if r.get("ok"))
    return {"ok": all_ok,
            "detail": f"{healthy}/{len(report)} transport(s) healthy; "
                      f"default {default!r}",
            "default": default,
            "identities": report}


def check_dispatcher() -> dict:
    """Scheduled-send dispatcher: plist installed under the current label,
    stray legacy com.paris.* plists flagged, log freshness, pending count."""
    from . import spool
    from .dispatcher import LAUNCHD_LABEL, _log_path, _plist_path

    plist = _plist_path()
    installed = plist.exists()
    agents = plist.parent
    legacy: list[str] = []
    if agents.is_dir():
        legacy = sorted(
            p.name for p in agents.glob("com.paris.*.plist")
            if "email-mcp" in p.name and p.name != plist.name
        )
    log = _log_path()
    log_mtime = None
    if log.exists():
        log_mtime = datetime.fromtimestamp(
            log.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    pending = len(spool.entries("pending"))
    # An uncountable spool must not read as "0 pending, no dispatcher
    # needed": entries() returns [] for a spool it cannot see, and this
    # check would then green-light a missing dispatcher over queued mail.
    countable = not spool.unreadable("pending")

    bits = [f"label {LAUNCHD_LABEL}: "
            f"{'installed' if installed else 'NOT installed'}",
            f"{pending} pending",
            f"log mtime {log_mtime or 'never'}"]
    fixes: list[str] = []
    if not installed:
        fixes.append("python -m email_mcp.dispatcher --install-launchd")
    if legacy:
        bits.append(f"legacy plist(s): {', '.join(legacy)}")
        fixes.append("boot out the legacy agent(s): launchctl bootout "
                     "gui/$UID ~/Library/LaunchAgents/<legacy>.plist")
    out: dict = {
        # Not installed only bites once something is waiting to send.
        "ok": installed or (pending == 0 and countable),
        "detail": "; ".join(bits),
        "installed": installed,
        "label": LAUNCHD_LABEL,
        "legacy_plists": legacy,
        "pending": pending,
        "log_mtime": log_mtime,
    }
    if fixes:
        out["fix"] = "; ".join(fixes)
    return out


def check_spool_plans() -> dict:
    """Spool + plan stores: directories exist with 0700, per-state counts,
    and no delivery claims stranded in sending/."""
    from . import spool
    from .dispatcher import STALE_SENDING_MINUTES

    # Resolve only — doctor checks stat, it never creates (the purity rule
    # _graph_token_dir and check_fts already follow). Creating here let a
    # read-only diagnostic build a spool tree wherever the state root
    # pointed, ~/Library/Mail included.
    spool_root = config.spool_dir(create=False)
    plans_root = config.plans_dir(create=False)
    problems: list[str] = []
    fixes: list[str] = []
    for label, d in (("spool", spool_root), ("plans", plans_root)):
        try:
            mode = d.stat().st_mode & 0o777
        except FileNotFoundError:
            # Absent is a fresh install, not a fault — the same rule
            # check_fts applies to a missing index. The first write creates
            # it 0700; a read-side check must not create it just to stat it.
            continue
        if mode != 0o700:
            problems.append(f"{label} dir {d} is mode {mode:o} (want 700)")
            fixes.append(f"chmod 700 {d}")

    # A symlink on one of the five spool state subdirectories is refused at
    # write time (config.spool_dir), so scheduling fails outright — but
    # this check never looked, and reported green. The designated
    # pre-flight tool must not certify a tree that cannot accept mail.
    linked = [str(spool_root / sub) for sub in spool.STATES
              if (spool_root / sub).is_symlink()]
    if linked:
        problems.append(
            "spool state dir(s) are symlinks, so scheduling will be refused: "
            + ", ".join(linked))
        fixes.append("remove the link(s), or relocate the whole tree with "
                     "EMAIL_MCP_STATE_DIR")

    counts = {s: len(spool.entries(s)) for s in spool.STATES}
    # entries() skips a manifest it cannot parse so a foreign file never
    # breaks a read — but a silent skip made "pending 0" mean both "nothing
    # queued" and "a queued message we cannot read". Say which.
    bad = {s: spool.unreadable(s) for s in spool.STATES}
    bad = {s: names for s, names in bad.items() if names}
    if bad:
        total = sum(len(n) for n in bad.values())
        where = "; ".join(f"{s}/: {', '.join(sorted(n))}"
                          for s, n in sorted(bad.items()))
        problems.append(f"{total} unreadable manifest(s) not counted above "
                        f"({where})")
        fixes.append("inspect those files; a half-written manifest can be "
                     "removed, a foreign file does not belong in the spool")
    now = spool.utcnow()
    stranded: list[str] = []
    for e in spool.entries("sending"):
        try:
            ref = datetime.fromisoformat(e.next_attempt_at or e.send_at)
        except ValueError:
            stranded.append(e.id)
            continue
        if (now - ref).total_seconds() / 60 >= STALE_SENDING_MINUTES:
            stranded.append(e.id)
    if stranded:
        problems.append(f"{len(stranded)} stranded claim(s) in sending/: "
                        f"{', '.join(sorted(stranded))}")
        fixes.append("python -m email_mcp.dispatcher   # one pass recovers "
                     "stranded claims")

    counts_txt = ", ".join(f"{s} {n}" for s, n in counts.items())
    out: dict = {
        "ok": not problems,
        "detail": counts_txt if not problems
        else f"{counts_txt}; " + "; ".join(problems),
        "counts": counts,
        "stranded_sending": sorted(stranded),
    }
    if fixes:
        out["fix"] = "; ".join(fixes)
    return out


def check_fts() -> dict:
    """SOFT hook on the body index — index trouble must not redden the
    doctor beyond what the search envelope already reports. An absent
    index is a fresh install, not a fault."""
    if not config.fts_enabled():
        return {"ok": True,
                "detail": "body index disabled (EMAIL_MCP_FTS_ENABLED=0)"}
    try:
        from . import fts
        st = fts.status()
    except Exception as e:  # soft by contract: never let fts crash doctor
        return {"ok": True, "detail": f"index status unavailable: {e}"}
    state = st.get("state")
    if state == "absent":
        return {"ok": True, "detail": "not built",
                "fix": "python -m email_mcp.fts --build", "status": st}
    if state == "error":
        return {"ok": False, "detail": f"index error: {st.get('error')}",
                "fix": "python -m email_mcp.fts --rebuild", "status": st}
    d = st.get("docs", {})
    return {"ok": True,
            "detail": f"ready: {d.get('indexed', 0)} indexed, "
                      f"{d.get('partial', 0)} partial, "
                      f"{d.get('missing', 0)} missing, "
                      f"{d.get('error', 0)} error",
            "status": st}


def check_audit() -> dict:
    """Audit ledger: directory exists with 0700 and the current month is
    appendable — probed with os.access, NEVER by writing an event (doctor
    is side-effect free; a probe event would be a lie in the ledger). An
    absent directory is a fresh install, not a fault: emit() creates it
    on the first mutation. Reports the last recorded event via tail(1)."""
    from . import audit, ids

    root = config.audit_dir(create=False)  # purity: never create here
    if root.exists() and not root.is_dir():
        # Pathological: a regular file where the ledger dir belongs. emit()
        # would silently drop every event (mkdir over a file raises) — the
        # one state the fresh-install branch must not mistake for healthy.
        return {
            "ok": False,
            "detail": f"{root} exists but is not a directory — every audit "
                      "event is being dropped.",
            "fix": f"move it aside: mv {root} {root}.bak && re-run doctor",
        }
    if not root.is_dir():
        probe = root.parent
        while not probe.exists():
            probe = probe.parent
        creatable = os.access(probe, os.W_OK | os.X_OK)
        out: dict = {
            "ok": creatable,
            "detail": (f"no ledger yet at {root} — created on the first "
                       "mutation" if creatable else
                       f"cannot create {root}: {probe} is not writable — "
                       "events would be dropped"),
            "last_event": None,
        }
        if not creatable:
            out["fix"] = f"chmod u+wx {probe}"
        return out

    problems: list[str] = []
    fixes: list[str] = []
    mode = root.stat().st_mode & 0o777
    if mode != 0o700:
        problems.append(f"dir mode {mode:o} (want 700 — events carry "
                        "recipients and subjects)")
        fixes.append(f"chmod 700 {root}")
    month = root / f"{ids.iso(ids.utcnow())[:7]}.jsonl"
    target = month if month.exists() else root
    writable = (os.access(month, os.W_OK) if month.exists()
                else os.access(root, os.W_OK | os.X_OK))
    if not writable:
        problems.append(f"{target} not writable — events are being "
                        "dropped (emit is log-and-continue)")
        fixes.append(f"chmod u+w {target}")

    last = audit.tail(1)
    last_txt = (f"last event {last[0].get('ts')} {last[0].get('event')}/"
                f"{last[0].get('outcome')}" if last else "no events yet")
    months = sum(1 for p in root.glob("*.jsonl"))
    detail = f"{months} monthly file(s); {last_txt}"
    if problems:
        detail += "; " + "; ".join(problems)
    out = {
        "ok": not problems,
        "detail": detail,
        "last_event": last[0].get("ts") if last else None,
    }
    if fixes:
        out["fix"] = "; ".join(fixes)
    return out


def _graph_token_dir() -> Path:
    """config.graph_dir()'s path WITHOUT its mkdir side effect — doctor
    checks stat, they never create (same purity rule as check_fts)."""
    return config.state_root(create=False) / "graph"


def _graph_token_report(name: str, path: Path) -> dict:
    """One identity's token cache: exists, refreshable shape, age."""
    fix = f"python -m email_mcp.graph --login {name}"
    try:
        cache = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {"ok": False, "detail": f"no token cache at {path} — never "
                                       "logged in (schedules silently fall "
                                       "back to launchd)", "fix": fix}
    except (ValueError, OSError) as e:
        return {"ok": False,
                "detail": f"unreadable token cache {path}: {e}", "fix": fix}
    if not cache.get("refresh_token"):
        return {"ok": False,
                "detail": f"token cache {path} has no refresh_token — "
                          "silent refresh is impossible", "fix": fix}
    out: dict = {"ok": True, "detail": "token cache present, refreshable"}
    try:
        obtained = float(cache.get("obtained_at") or path.stat().st_mtime)
        age_days = max(0.0, (time.time() - obtained) / 86400)
        out["age_days"] = round(age_days, 1)
        out["detail"] += f" (obtained {age_days:.1f}d ago)"
        # Entra refresh tokens die after ~90 idle days; flag well before.
        if age_days > 60:
            out["detail"] += " — aging; re-login before it expires"
            out["fix"] = fix
    except (TypeError, ValueError, OSError):
        pass
    return out


def check_graph() -> dict:
    """Graph executor readiness: for every identity with executor="graph",
    the token cache must exist and hold a refresh token, or reconcile and
    schedule-time deferral cannot work — red with the --login fix. Still
    soft (green, one line) when no identity opts in."""
    try:
        idents, _ = identities.load()
    except SendError:
        return {"ok": True,
                "detail": "identities unreadable — see the identities check"}
    graph_idents = sorted(
        name for name, ident in idents.items()
        if getattr(ident, "executor", "launchd") == "graph"
    )
    if not graph_idents:
        return {"ok": True, "detail": "no identities use the graph executor"}
    d = _graph_token_dir()
    report = {name: _graph_token_report(name, d / f"{name}.token.json")
              for name in graph_idents}
    bad = sorted(n for n, r in report.items() if not r["ok"])
    healthy = len(report) - len(bad)
    out: dict = {
        "ok": not bad,
        "detail": f"{healthy}/{len(report)} graph identity(ies) ready: "
                  f"{', '.join(graph_idents)}",
        "identities": report,
    }
    if bad:
        out["fix"] = "; ".join(report[n]["fix"] for n in bad
                               if report[n].get("fix"))
    return out


# ---------------------------------------------------------------------- #
# the one-call report                                                     #
# ---------------------------------------------------------------------- #


def check_state_root() -> dict:
    """The configured state root is one this tool may manage.

    A refused root is not a cosmetic problem: every mutation drops its
    receipts, scheduling has nowhere to freeze a message, and the triage
    plan store is unreachable. Without this check the doctor went GREEN in
    exactly that state — the resolver refuses at write time, and no
    read-side check ever asked why the tree was absent.

    Read-only, like every check: it asks config for the refusal reason,
    which stats and never creates.
    """
    root = config.state_root(create=False)
    reason = config.state_root_refusal()
    if reason:
        out = {"ok": False, "detail": reason, "root": str(root)}
        if config.retired_state_vars():
            out["fix"] = ("unset " + ", ".join(config.retired_state_vars())
                          + "; set EMAIL_MCP_STATE_DIR instead")
        else:
            out["fix"] = ("unset EMAIL_MCP_STATE_DIR, or point it at a new "
                          "or empty directory of its own")
        return out
    if not root.exists():
        return {"ok": True, "root": str(root),
                "detail": f"{root} — created on first use"}
    mode = root.stat().st_mode & 0o777
    marked = (root / config.STATE_MARKER).is_file()
    detail = f"{root} (mode {mode:o}{'' if marked else ', unmarked'})"
    if mode != 0o700:
        return {"ok": False, "root": str(root),
                "detail": detail + " — wants 700; state holds recipients, "
                                   "subjects and token caches",
                "fix": f"email-mcp doctor --fix   # chmod 700 {root}"}
    return {"ok": True, "root": str(root), "detail": detail}


_CHECKS = (
    ("state_root", check_state_root),
    ("mail_store", check_mail_store),
    ("automation", check_automation),
    ("accessibility", check_accessibility),
    ("identities", check_identities),
    ("transports", check_transports),
    ("dispatcher", check_dispatcher),
    ("spool_plans", check_spool_plans),
    ("fts", check_fts),
    ("graph", check_graph),
    ("audit", check_audit),  # folded into checks at v0.11 (as scheduled)
)


def _guarded(name: str, fn) -> dict:
    """Doctor must work precisely when everything else is broken — a check
    that blows up becomes a red entry, never a crashed tool."""
    try:
        return fn()
    except OSError as e:
        # An OSError has a readable story (strerror + filename); the bare
        # repr — "check crashed: NotADirectoryError(20, 'Not a directory')"
        # — told an operator nothing and named no path.
        _log.exception("doctor: check %s failed", name)
        where = f" ({e.filename})" if getattr(e, "filename", None) else ""
        return {"ok": False,
                "detail": f"{e.strerror or e}{where}",
                "fix": "run `email-mcp doctor` again after fixing the path "
                       "above; `--fix` repairs the safe cases"}
    except Exception as e:
        _log.exception("doctor check %s crashed", name)
        return {"ok": False, "detail": f"check crashed: {e!r}"}


def run() -> dict:
    """Run every check. Returns {ok, read_only, checks} — the ledger
    check is a member of `checks` since v0.11 and gates `ok` like any
    other: a ledger that silently drops events is a red doctor. The
    top-level `audit` key mirrors checks["audit"] for v0.10 readers
    (deprecated; see the module docstring)."""
    checks = {name: _guarded(name, fn) for name, fn in _CHECKS}
    # The mirror falls back to its own run when `checks` was narrowed
    # (tests monkeypatch _CHECKS) — the ledger check must always report.
    audit_check = checks.get("audit") or _guarded("audit", check_audit)
    return {
        "ok": all(c["ok"] for c in checks.values()) and audit_check["ok"],
        "read_only": config.read_only(),
        "checks": checks,
        "audit": audit_check,
    }
