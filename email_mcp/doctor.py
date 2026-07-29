"""Environment diagnostics: every permission, path and transport the MCP
needs, checked in one pass with remediation hints.

`run()` returns {ok, read_only, checks: {name: {ok, detail, fix?, ...}}} —
`ok` is the AND of every check; `fix` appears only when there is a concrete
next step (a Settings pane or a command). Checks never mutate anything:
transports are healthchecked but never bootstrapped, the FTS index is
statted but never created, and the osascript probes are benign reads.

Surfaced as the `doctor` MCP tool (registered in BOTH normal and READ_ONLY
modes) and as ``python -m email_mcp.server --doctor``; the old
``--transport-check`` flag lives on as a deprecated alias for the
`transports` check alone.
"""
from __future__ import annotations

import sqlite3
import subprocess
import urllib.parse
from datetime import datetime, timezone

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


def check_identities() -> dict:
    """Does the identities file parse? The load error is surfaced verbatim
    — it already names the file and the offending key."""
    try:
        idents, default = identities.load()
    except SendError as e:  # IdentityError subclasses SendError
        return {"ok": False, "detail": str(e),
                "fix": f"edit {config.identities_file()}"}
    return {"ok": True,
            "detail": f"{len(idents)} identity(ies): "
                      f"{', '.join(sorted(idents))}; default {default!r}"}


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
        "ok": installed or pending == 0,
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

    spool_root = config.spool_dir()   # creates + chmods, like every caller
    plans_root = config.plans_dir()
    problems: list[str] = []
    fixes: list[str] = []
    for label, d in (("spool", spool_root), ("plans", plans_root)):
        mode = d.stat().st_mode & 0o777
        if mode != 0o700:
            problems.append(f"{label} dir {d} is mode {mode:o} (want 700)")
            fixes.append(f"chmod 700 {d}")

    counts = {s: len(spool.entries(s)) for s in spool.STATES}
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


def check_graph() -> dict:
    """SOFT hook on the Graph executor. Substance (token age, draft
    reachability) lands with the executor itself (S7); identities without
    an `executor` attribute are plain launchd and report nothing."""
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
    return {"ok": True,
            "detail": f"graph executor on: {', '.join(graph_idents)} "
                      "(token diagnostics land with the graph executor)"}


# ---------------------------------------------------------------------- #
# the one-call report                                                     #
# ---------------------------------------------------------------------- #


_CHECKS = (
    ("mail_store", check_mail_store),
    ("automation", check_automation),
    ("accessibility", check_accessibility),
    ("identities", check_identities),
    ("transports", check_transports),
    ("dispatcher", check_dispatcher),
    ("spool_plans", check_spool_plans),
    ("fts", check_fts),
    ("graph", check_graph),
)


def _guarded(name: str, fn) -> dict:
    """Doctor must work precisely when everything else is broken — a check
    that blows up becomes a red entry, never a crashed tool."""
    try:
        return fn()
    except Exception as e:
        _log.exception("doctor check %s crashed", name)
        return {"ok": False, "detail": f"check crashed: {e!r}"}


def run() -> dict:
    """Run every check. Returns {ok, read_only, checks}."""
    checks = {name: _guarded(name, fn) for name, fn in _CHECKS}
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "read_only": config.read_only(),
        "checks": checks,
    }
