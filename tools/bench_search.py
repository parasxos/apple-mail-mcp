#!/usr/bin/env python3
"""Reproducible benchmark: full-text search over the local body index.

Times FTS5 MATCH queries against the index this server builds
(~/.email-mcp/fts/fts.db). Reports p50/p95 over N runs for a few terms of
different selectivity. Run it on your own index; sizes differ.

Usage:  python3 tools/bench_search.py [--runs N]
"""

from __future__ import annotations

import argparse
import platform
import sqlite3
import statistics
import time
from pathlib import Path

DB = Path.home() / ".email-mcp/fts/fts.db"
TERMS = ["invoice", "meeting agenda", "stride_view", "reconnection"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    n = conn.execute("SELECT COUNT(*) FROM body_fts").fetchone()[0]
    print(f"host: {platform.machine()}, macOS {platform.mac_ver()[0]}")
    print(f"index: {n:,} message bodies in {DB.name}")
    for term in TERMS:
        samples = []
        hits = 0
        for _ in range(args.runs):
            t0 = time.perf_counter()
            rows = conn.execute(
                "SELECT rowid FROM body_fts WHERE body_fts MATCH ? LIMIT 50",
                (term,)).fetchall()
            samples.append(time.perf_counter() - t0)
            hits = len(rows)
        samples.sort()
        p50 = statistics.median(samples) * 1000
        p95 = samples[int(len(samples) * 0.95) - 1] * 1000
        print(f"  {term!r:20} p50 {p50:7.1f} ms   p95 {p95:7.1f} ms   "
              f"hits {hits}{'+' if hits == 50 else ''}   runs {args.runs}")
    conn.close()


if __name__ == "__main__":
    main()
