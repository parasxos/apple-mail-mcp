"""FTS background scheduling and command-line presentation."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, state

LAUNCHD_LABEL = "com.email-mcp.fts"
BACKFILL_CAP = 500


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def log_path() -> Path:
    return config.state_dir() / "fts.log"


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
        <string>email_mcp.fts</string>
        <string>--sync</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path}</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{log_path()}</string>
    <key>StandardErrorPath</key><string>{log_path()}</string>
</dict>
</plist>
"""


def install_launchd() -> str:
    state.State.resolve().adopt()
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_content())
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist)],
        capture_output=True,
    )
    process = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True,
    )
    if process.returncode != 0:
        return f"bootstrap failed: {process.stderr.strip()}"
    return (
        f"installed {LAUNCHD_LABEL} (daily 03:30 --sync), log: {log_path()}"
    )


def uninstall_launchd() -> str:
    plist = plist_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
    )
    if plist.exists():
        plist.unlink()
    return f"removed {LAUNCHD_LABEL}"


def reconcile_due(index) -> bool:
    stamp = index.status().get("last_reconcile_at")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last).days >= config.fts_reconcile_days()


def sync(index, limit: int | None) -> dict:
    result = index.incremental(max_docs=limit)
    if result.get("skipped") == "busy":
        return result
    if reconcile_due(index):
        result["reconcile"] = index.reconcile()
    result["backfill"] = index.backfill(max_docs=BACKFILL_CAP)
    return result


def print_status(status: dict, as_json: bool) -> None:
    if as_json:
        json.dump(status, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if status["state"] == "absent":
        print(f"index: {status['db']} (absent)")
        print(f"build it: {status['remedy']}")
        return
    if status["state"] == "error":
        print(f"index: {status['db']} (error: {status.get('error')})")
        return
    docs = status["docs"]
    megabytes = status["db_bytes"] / 1_048_576
    print(
        f"index: {status['db']} ({status['state']}, {megabytes:.1f} MB, "
        f"schema v{status['schema_version']})"
    )
    print(
        f"docs: {docs['indexed']} indexed, {docs['partial']} partial, "
        f"{docs['missing']} missing, {docs['error']} error "
        f"(hwm rowid {status['last_rowid']})"
    )
    print(
        f"built: {status['built_at'] or '-'}  "
        f"synced: {status['last_sync_at'] or '-'}  "
        f"reconciled: {status['last_reconcile_at'] or '-'}  "
        f"backfilled: {status.get('last_backfill_at') or '-'}"
    )
    if status.get("last_backfill_error"):
        print(f"backfill trouble: {status['last_backfill_error']}")


def main(index_factory, database_error, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="email_mcp.fts",
        description="Local FTS5 body index over Apple Mail.",
    )
    parser.add_argument(
        "--build", action="store_true",
        help="crawl all unindexed messages (resumable)",
    )
    parser.add_argument(
        "--sync", action="store_true",
        help="incremental catch-up + miss retries (+ weekly reconcile)",
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="full rowid-set diff against the Envelope Index",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="fetch bodies Mail never downloaded from the server",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="drop the index and build from scratch",
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="cap documents processed this run",
    )
    parser.add_argument("--install-launchd", action="store_true")
    parser.add_argument("--uninstall-launchd", action="store_true")
    args = parser.parse_args(argv)

    if args.install_launchd:
        print(install_launchd())
        return 0
    if args.uninstall_launchd:
        print(uninstall_launchd())
        return 0

    index = index_factory()
    if args.status:
        print_status(index.status(), args.json)
        return 0

    try:
        if args.build:
            result = index.build(limit=args.limit)
        elif args.rebuild:
            result = index.rebuild(limit=args.limit)
        elif args.reconcile:
            result = index.reconcile()
        elif args.backfill:
            result = index.backfill(max_docs=args.limit)
        elif args.sync:
            result = sync(index, args.limit)
        else:
            parser.print_help()
            return 2
    except (FileNotFoundError, database_error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2 if args.json else None)
    sys.stdout.write("\n")
    return 0
