"""macOS process plumbing for the scheduled-delivery entry point.

This module owns launchd installation and operator status. It composes no
application services and contains no delivery policy.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config, spool, state

LAUNCHD_LABEL = config.LAUNCHD_LABEL
LEGACY_LABELS = config.LEGACY_LAUNCHD_LABELS


def plist_path() -> Path:
    return config.dispatcher_plist()


def log_path() -> Path:
    return config.state_dir() / "dispatcher.log"


def plist_content() -> str:
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
    <key>StandardOutPath</key><string>{log_path()}</string>
    <key>StandardErrorPath</key><string>{log_path()}</string>
</dict>
</plist>
"""


def _remove_legacy_plists() -> list[str]:
    uid = os.getuid()
    removed: list[str] = []
    for label in LEGACY_LABELS:
        plist = plist_path().parent / f"{label}.plist"
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
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_content())
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
    return f"installed {LAUNCHD_LABEL} (every 60s){note}, log: {log_path()}"


def uninstall_launchd() -> str:
    removed_legacy = _remove_legacy_plists()
    plist = plist_path()
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
        "launchd_plist": str(plist_path()),
        "launchd_installed": plist_path().exists(),
        "counts": integrity["counts"],
        "pending": [_status_entry(e) for e in by_state["pending"].entries],
        "sending": [_status_entry(e) for e in by_state["sending"].entries],
        "failed": [_status_entry(e) for e in by_state["failed"].entries],
    }
    if not integrity["ok"]:
        out["integrity"] = integrity
    return out
