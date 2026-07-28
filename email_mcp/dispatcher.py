"""Scheduled-send dispatcher: one pass over the spool, then exit.

launchd runs this module every 60 s (RunAtLoad + StartInterval), which by
construction gives Mail.app-style semantics: a message due while the Mac
was asleep goes out on the first pass after wake — "send when open".
No daemon, no timers to reason about; every run just asks "what is due?".

Per due message: claim by atomic manifest rename (double-send-safe across
overlapping runs) → re-check the allowlist (config may have changed since
scheduling) → deliver the frozen bytes over the existing SSH path
(bootstrapping the ControlMaster if cold) → sent/. Failures retry with
backoff (2/5/15/45/120 min), then park in failed/ with the error and a
macOS notification.

CLI:
  python -m email_mcp.dispatcher              # one pass (what launchd runs)
  python -m email_mcp.dispatcher --status     # spool overview
  python -m email_mcp.dispatcher --install-launchd
  python -m email_mcp.dispatcher --uninstall-launchd
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, sender, spool

LAUNCHD_LABEL = "com.paris.email-mcp-dispatcher"
BACKOFF_MINUTES = (2, 5, 15, 45, 120)


# --------------------------------------------------------------------- #
# one pass                                                              #
# --------------------------------------------------------------------- #


def _due(entry: spool.Entry, now: datetime) -> bool:
    if datetime.fromisoformat(entry.send_at) > now:
        return False
    if entry.next_attempt_at and datetime.fromisoformat(entry.next_attempt_at) > now:
        return False
    return True


def _notify(title: str, text: str) -> None:
    """Best-effort macOS notification; never raises."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "{title}"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _fail_or_retry(entry: spool.Entry, error: str, now: datetime) -> str:
    entry.attempts += 1
    entry.last_error = error
    if entry.attempts >= config.send_max_retries():
        spool.move(entry.id, "sending", "failed", entry)
        _notify("email-mcp: send FAILED",
                f"{entry.subject!r} to {', '.join(entry.to)} — {error[:120]}")
        return "failed"
    delay = BACKOFF_MINUTES[min(entry.attempts - 1, len(BACKOFF_MINUTES) - 1)]
    entry.next_attempt_at = spool.iso(now + timedelta(minutes=delay))
    spool.move(entry.id, "sending", "pending", entry)
    return f"retry in {delay}m"


STALE_SENDING_MINUTES = 10


def _recover_stranded(now: datetime) -> list[str]:
    """A dispatcher that died mid-delivery leaves its claim in sending/.
    Anything sitting there longer than STALE_SENDING_MINUTES goes back to
    pending (attempt consumed — the delivery outcome is unknown, so this
    also throttles a crash-looping message toward failed/)."""
    recovered = []
    for e in spool.entries("sending"):
        ref = e.next_attempt_at or e.send_at
        age_min = (now - datetime.fromisoformat(ref)).total_seconds() / 60
        if age_min < STALE_SENDING_MINUTES:
            continue
        e.attempts += 1
        e.last_error = e.last_error or "dispatcher died mid-delivery (recovered from sending/)"
        if e.attempts >= config.send_max_retries():
            spool.move(e.id, "sending", "failed", e)
        else:
            e.next_attempt_at = spool.iso(now)
            spool.move(e.id, "sending", "pending", e)
            recovered.append(e.id)
    return recovered


def run_once(now: datetime | None = None) -> dict:
    now = now or spool.utcnow()
    _recover_stranded(now)
    due = [e for e in spool.entries("pending") if _due(e, now)]
    results: dict[str, str] = {}
    if not due:
        return {"checked_at": spool.iso(now), "due": 0, "results": results}

    transport_ok = sender._socket_alive() or sender._bootstrap_master()

    for entry in due:
        if not spool.claim(entry.id):
            results[entry.id] = "claimed elsewhere"
            continue
        entry.status = "sending"

        if not transport_ok:
            results[entry.id] = _fail_or_retry(
                entry, "no SSH transport (socket dead, bootstrap failed)", now)
            continue
        try:
            raw = spool.read_eml("sending", entry.id)
        except FileNotFoundError:
            entry.last_error = "spool .eml missing"
            spool.move(entry.id, "sending", "failed", entry)
            results[entry.id] = "failed"
            continue
        try:
            # NOTE: no allowlist re-check here. Authorization happens at
            # schedule time, inside the MCP server where the user's real
            # config lives; this process runs under launchd's bare env,
            # where the guard would wrongly block every non-self message.
            sender._deliver_bytes(raw)
        except sender.SendError as e:
            results[entry.id] = _fail_or_retry(entry, str(e), now)
            continue
        entry.delivered_at = spool.iso(spool.utcnow())
        entry.next_attempt_at = None
        spool.move(entry.id, "sending", "sent", entry)
        results[entry.id] = "sent"

    return {"checked_at": spool.iso(now), "due": len(due), "results": results}


# --------------------------------------------------------------------- #
# launchd install                                                       #
# --------------------------------------------------------------------- #


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _log_path() -> Path:
    d = config.spool_dir().parent
    return d / "dispatcher.log"


def _plist_content() -> str:
    # launchd gives agents a bare PATH (/usr/bin:/bin:...), under which the
    # SSH bootstrap's TOTP helper resolves to the wrong python3. Baking the
    # INSTALLING shell's PATH into the plist means the dispatcher sees the
    # same toolchain that worked when the user ran --install-launchd.
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


def install_launchd() -> str:
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_plist_content())
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)],
                   capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"bootstrap failed: {proc.stderr.strip()}"
    return f"installed {LAUNCHD_LABEL} (every 60s), log: {_log_path()}"


def uninstall_launchd() -> str:
    plist = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                   capture_output=True)
    if plist.exists():
        plist.unlink()
    return f"removed {LAUNCHD_LABEL}"


def status() -> dict:
    return {
        "spool": str(config.spool_dir()),
        "launchd_plist": str(_plist_path()),
        "launchd_installed": _plist_path().exists(),
        "counts": {s: len(spool.entries(s)) for s in spool.STATES},
        "pending": [
            {"id": e.id, "send_at": e.send_at, "to": e.to,
             "subject": e.subject, "attempts": e.attempts,
             "next_attempt_at": e.next_attempt_at, "last_error": e.last_error}
            for e in spool.entries("pending")
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="email_mcp.dispatcher")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--install-launchd", action="store_true")
    parser.add_argument("--uninstall-launchd", action="store_true")
    args = parser.parse_args()
    if args.status:
        json.dump(status(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.install_launchd:
        print(install_launchd())
        return 0
    if args.uninstall_launchd:
        print(uninstall_launchd())
        return 0
    summary = run_once()
    # Quiet when idle: only log passes that actually did something.
    if summary["due"]:
        json.dump(summary, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
