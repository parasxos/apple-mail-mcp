#!/usr/bin/env python3
"""Reproducible benchmark: address one known message in a large mailbox.

Compares the two mechanisms this project cares about:

  index    SELECT by ROWID against Mail's Envelope Index (what this server
           does for every read).
  applescript
           A `whose` clause against Mail.app for the same message — the
           primitive AppleScript-based tooling builds on. Skipped unless
           --applescript is passed, because on a large mailbox it takes
           minutes and requires Automation permission for Mail.

Method requirements this satisfies (so the numbers can be quoted):
  - N runs with min/median reported, not a single hot run
  - the mailbox size measured, not asserted
  - hardware printed alongside the numbers
  - everything in one script anyone can run on their own Mac

Usage:  python3 tools/bench_addressing.py [--applescript] [--runs N]
"""

from __future__ import annotations

import argparse
import platform
import sqlite3
import statistics
import subprocess
import time
import urllib.parse
from pathlib import Path

INDEX = Path.home() / "Library/Mail/V10/MailData/Envelope Index"


def connect() -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(INDEX)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only = 1")
    return conn


def pick_target(conn: sqlite3.Connection) -> tuple[int, str, int, str, int]:
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    # Use the LARGEST mailbox and a message in its middle, so the AppleScript
    # side searches the same haystack the index side addresses into. Searching
    # a small mailbox (or one not containing the target) returns fast and
    # proves nothing — an early version of this script made that mistake.
    mbox_rowid, mbox_url, mbox_n = conn.execute(
        """SELECT mb.ROWID, mb.url, COUNT(*) AS n FROM messages m
           JOIN mailboxes mb ON m.mailbox = mb.ROWID
           GROUP BY mb.ROWID ORDER BY n DESC LIMIT 1""").fetchone()
    rowid, subj = conn.execute(
        """SELECT m.ROWID, s.subject FROM messages m
           JOIN subjects s ON m.subject = s.ROWID
           WHERE m.mailbox = ? AND s.subject != ''
           ORDER BY m.ROWID LIMIT 1 OFFSET ?""",
        (mbox_rowid, mbox_n // 2)).fetchone()
    name = urllib.parse.unquote(mbox_url.rstrip("/").rsplit("/", 1)[-1])
    return rowid, subj, total, name, mbox_n


def bench_index(conn: sqlite3.Connection, rowid: int, runs: int) -> list[float]:
    out = []
    for _ in range(runs):
        t0 = time.perf_counter()
        row = conn.execute(
            """SELECT m.ROWID, s.subject, a.address, m.date_received
               FROM messages m
               LEFT JOIN subjects s ON m.subject = s.ROWID
               LEFT JOIN addresses a ON m.sender = a.ROWID
               WHERE m.ROWID = ?""", (rowid,)).fetchone()
        assert row is not None
        out.append(time.perf_counter() - t0)
    return out


def bench_applescript(subject: str, mailbox: str, runs: int) -> list[float]:
    """Whose-clause against the SAME mailbox the target lives in.

    The result must be non-empty: a fast error or not-found is a failed run,
    not a fast run. Searches every account for a mailbox with this name and
    stops at the first that yields a hit.
    """
    esc = subject.replace("\\", "\\\\").replace('"', '\\"')
    mesc = mailbox.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
    tell application "Mail"
        repeat with acc in accounts
            try
                set mb to mailbox "{mesc}" of acc
                set m to first message of mb whose subject is "{esc}"
                return id of m
            end try
        end repeat
        error "target message not found in mailbox {mesc}"
    end tell
    """
    out = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=1800)
        dt = time.perf_counter() - t0
        if r.returncode != 0 or not r.stdout.strip():
            raise SystemExit(
                f"applescript run failed after {dt:.1f}s: {r.stderr.strip()[:200]}")
        out.append(dt)
    return out


def report(label: str, samples: list[float]) -> None:
    print(f"  {label:12} min {min(samples)*1000:9.1f} ms   "
          f"median {statistics.median(samples)*1000:9.1f} ms   "
          f"runs {len(samples)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--applescript", action="store_true",
                    help="also run the Mail.app whose-clause (slow; needs Automation permission)")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    conn = connect()
    rowid, subject, total, mbox_name, mbox_n = pick_target(conn)
    mac = platform.machine()
    ver = platform.mac_ver()[0]
    print(f"host: {mac}, macOS {ver}")
    print(f"store: {total:,} messages in the Envelope Index")
    print(f"haystack: mailbox '{mbox_name}' with {mbox_n:,} messages")
    print(f"target: message ROWID {rowid} (subject of length {len(subject)})")

    report("index", bench_index(conn, rowid, args.runs))
    if args.applescript:
        report("applescript", bench_applescript(subject, mbox_name, max(1, args.runs // 5) or 1))
    else:
        print("  applescript  skipped (pass --applescript; expect minutes on a large mailbox)")
    conn.close()


if __name__ == "__main__":
    main()
