"""Scheduled-send process facade and launchd integration.

The retry, recovery and Exchange hand-off rules live in the application
layer. This module keeps the established CLI and compatibility functions
while owning only process presentation and macOS launchd installation.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import audit, config, spool, state
from .adapters.background import DefaultBackgroundGateway, _macos_notification
from .application.background import (
    BACKOFF_MINUTES,
    GRAPH_GRACE_MINUTES,
    STALE_SENDING_MINUTES,
    SUPERSEDED,
    is_due,
    parse_timestamp,
)

LAUNCHD_LABEL = "com.email-mcp.dispatcher"
LEGACY_LABELS = ("com.paris.email-mcp-dispatcher",)
_SUPERSEDED = SUPERSEDED
_parse_iso = parse_timestamp
_due = is_due


def _notify(title: str, text: str) -> None:
    _macos_notification(title, text)


def _application():
    """Compose a fresh worker so runtime config and test seams are observed."""
    from .bootstrap import build_application

    return build_application(
        background=DefaultBackgroundGateway(notifier=_notify),
    )


def _fail_or_retry(entry, error: str, now: datetime,
                   src: str = "sending") -> str:
    return _application()._fail_or_retry(entry, error, now, source=src)


def _recover_stranded(now: datetime) -> list[str]:
    return _application().recover_stranded(now)


def _graph_current(entry) -> bool:
    return _application().graph_current(entry)


def _graph_mark_sent(entry, now: datetime) -> str:
    return _application().graph_mark_sent(entry, now)


def _graph_adopt(entry, draft_id: str) -> str:
    return _application().graph_adopt(entry, draft_id)


def _graph_flip_to_launchd(entry, now: datetime, reason: str,
                           clear_draft: bool) -> str:
    return _application().graph_flip_to_local(
        entry, now, reason, clear_draft,
    )


def _graph_leave(entry, error: str, note: str) -> str:
    return _application().graph_leave(entry, error, note)


def _graph_apply_status(entry, status: str, now: datetime) -> str:
    return _application().graph_apply_status(entry, status, now)


def _reconcile_graph(now: datetime) -> dict[str, str]:
    return _application().reconcile_deferred(now)


def run_once(now: datetime | None = None) -> dict:
    """Run one idempotent scheduled-delivery application pass."""
    return _application().dispatch_scheduled(now)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _log_path() -> Path:
    return config.state_dir() / "dispatcher.log"


def _plist_content() -> str:
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>email_mcp.dispatcher</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path}</string>
    </dict>
    <key>StartInterval</key><integer>60</integer>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{_log_path()}</string>
    <key>StandardErrorPath</key><string>{_log_path()}</string>
</dict>
</plist>
"""


def _remove_legacy_plists() -> list[str]:
    uid = os.getuid()
    removed: list[str] = []
    for label in LEGACY_LABELS:
        plist = _plist_path().parent / f"{label}.plist"
        proc = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True,
        )
        found = proc.returncode == 0 or plist.exists()
        plist.unlink(missing_ok=True)
        if found:
            removed.append(label)
    return removed


def install_launchd() -> str:
    state.State.resolve().adopt()
    migrated = _remove_legacy_plists()
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_plist_content())
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist)],
        capture_output=True,
    )
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return f"bootstrap failed: {proc.stderr.strip()}"
    note = f" (migrated legacy: {', '.join(migrated)})" if migrated else ""
    return f"installed {LAUNCHD_LABEL} (every 60s){note}, log: {_log_path()}"


def uninstall_launchd() -> str:
    removed_legacy = _remove_legacy_plists()
    plist = _plist_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
    )
    if plist.exists():
        plist.unlink()
    note = (
        f" (+ legacy: {', '.join(removed_legacy)})"
        if removed_legacy else ""
    )
    return f"removed {LAUNCHD_LABEL}{note}"


def _status_entry(entry: spool.Entry) -> dict:
    return {
        "id": entry.id,
        "send_at": entry.send_at,
        "to": entry.to,
        "subject": entry.subject,
        "attempts": entry.attempts,
        "next_attempt_at": entry.next_attempt_at,
        "last_error": entry.last_error,
        "executor": entry.executor,
    }


def status() -> dict:
    scans = spool.scan_all()
    integrity = spool.integrity(scans)
    by_state = {result.state: result for result in scans}
    out = {
        "ok": integrity["ok"],
        "spool": str(config.spool_dir()),
        "launchd_plist": str(_plist_path()),
        "launchd_installed": _plist_path().exists(),
        "counts": integrity["counts"],
        "pending": [_status_entry(e) for e in by_state["pending"].entries],
        "sending": [_status_entry(e) for e in by_state["sending"].entries],
        "failed": [_status_entry(e) for e in by_state["failed"].entries],
    }
    if not integrity["ok"]:
        out["integrity"] = integrity
    return out


def main(argv: list[str] | None = None) -> int:
    audit.set_process("dispatcher")
    parser = argparse.ArgumentParser(prog="email_mcp.dispatcher")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--install-launchd", action="store_true")
    parser.add_argument("--uninstall-launchd", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        report = status()
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if report["ok"] else 1
    if args.install_launchd:
        print(install_launchd())
        return 0
    if args.uninstall_launchd:
        print(uninstall_launchd())
        return 0
    summary = run_once()
    if summary["due"] or summary["results"] or "integrity" in summary:
        json.dump(summary, sys.stdout)
        sys.stdout.write("\n")
    return 1 if "integrity" in summary else 0


if __name__ == "__main__":
    raise SystemExit(main())
