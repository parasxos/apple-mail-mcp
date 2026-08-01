"""Local FTS5 full-body index over Apple Mail .emlx files.

The Envelope Index only carries first-line snippets, so search silently
misses message bodies. This module maintains a private SQLite database
(<state root>/fts/fts.db, 0700) with three tables:

  meta      key/value: schema_version, last_rowid high-water mark, timestamps
  docs      per-message ledger: status indexed|partial|missing|error,
            attempts, last_attempt, bytes
  body_fts  plain FTS5 over extracted body text, rowid == messages.ROWID

Design points (docs/v0.8-concept.md, movement 1):
  * Apple's store is never written; bodies are always SERVED from .emlx —
    the index only routes rowids into search.
  * ROWIDs are AUTOINCREMENT (never reused), so "new mail" is exactly
    "ROWID > last_rowid" and deletions are absent rowids (reconcile).
  * Crawls read the Envelope Index through fresh short-lived read-only
    connections per batch — never one long read txn against Mail's WAL.
  * Our own writes use WAL + busy_timeout + BEGIN IMMEDIATE; when another
    writer holds the lock, incremental() returns {"skipped": "busy"}
    instead of blocking a search.
  * db_path() never creates anything — only _open_rw() (build/sync paths)
    may mkdir/create. Read paths on a machine that never built the index
    must leave zero traces.

CLI:
  python -m email_mcp.fts --build              # initial crawl (resumable)
  python -m email_mcp.fts --sync               # catch up + retries (+ weekly reconcile)
  python -m email_mcp.fts --reconcile          # full rowid-set diff
  python -m email_mcp.fts --rebuild            # drop the db, build from scratch
  python -m email_mcp.fts --status [--json]
  python -m email_mcp.fts --install-launchd    # com.email-mcp.fts, 03:30 daily --sync
  python -m email_mcp.fts --uninstall-launchd
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, state
from .log import get_logger
from .sources.apple_mail_paths import find_emlx_path, mailbox_data_dir

SCHEMA_VERSION = 1
LAUNCHD_LABEL = "com.email-mcp.fts"

_DB_NAME = "fts.db"
_BUSY_TIMEOUT_MS = 5000
_BATCH_SIZE = 2000
_MAX_ATTEMPTS = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    rowid        INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL NOT NULL DEFAULT 0,
    bytes        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS docs_status ON docs(status);
CREATE VIRTUAL TABLE IF NOT EXISTS body_fts USING fts5(
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def match_expr(query: str) -> str:
    """Injection-proof FTS5 MATCH expression: AND of double-quoted tokens.

    Only \\w+ runs survive, each wrapped in its own quoted string, so no
    user input can reach the FTS5 query grammar (NEAR, column filters,
    ``*``, ``-``, stray quotes/parens all die at tokenization). Returns
    "" when the query holds no indexable tokens.
    """
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens)


def db_path() -> Path:
    """Where the index lives. Never creates directories (read-path purity)."""
    return config.fts_dir() / _DB_NAME


def status() -> dict:
    """Module-level convenience for soft hooks (doctor, search envelope)."""
    return FtsIndex().status()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _retry_delay(attempts: int) -> float:
    """Backoff before a `missing` doc is re-statted: 1h, 6h, then 24h×n."""
    if attempts <= 1:
        return 3600.0
    if attempts == 2:
        return 6 * 3600.0
    return 24 * 3600.0 * (attempts - 2)


class FtsIndex:
    """Build, maintain and query the body index.

    Read methods (available/status/rowids_matching) never create the db.
    Write methods (build/incremental/reconcile/rebuild) open it read-write
    and create it on first use — callers on the search path must gate on
    available() before invoking any of them.
    """

    def __init__(self, mail_base: Path | None = None) -> None:
        # Resolved lazily: status()/rowids_matching() must work (and --status
        # must print) on a machine where config.mail_dir() would raise.
        self._mail_base = mail_base
        self._data_dir_cache: dict[str, Path | None] = {}
        self._envelope_deleted: bool | None = None

    # ------------------------------------------------------------------ #
    # read side                                                          #
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        return db_path().exists()

    def status(self) -> dict:
        path = db_path()
        out: dict = {
            "state": "absent",
            "db": str(path),
            "db_bytes": 0,
            "schema_version": None,
            "last_rowid": 0,
            "docs": {"indexed": 0, "partial": 0, "missing": 0, "error": 0,
                     "total": 0},
            "built_at": None,
            "last_sync_at": None,
            "last_reconcile_at": None,
        }
        if not path.exists():
            out["remedy"] = "python -m email_mcp.fts --build"
            return out
        try:
            conn = self._open_ro()
        except sqlite3.Error as e:
            out["state"] = "error"
            out["error"] = str(e)
            return out
        try:
            counts = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM docs GROUP BY status"
                )
            }
            for key in ("indexed", "partial", "missing", "error"):
                out["docs"][key] = counts.get(key, 0)
            out["docs"]["total"] = sum(counts.values())
            out["state"] = "ready"
            out["db_bytes"] = path.stat().st_size
            raw_version = self._meta_get(conn, "schema_version")
            out["schema_version"] = int(raw_version) if raw_version else None
            out["last_rowid"] = int(self._meta_get(conn, "last_rowid", "0"))
            out["built_at"] = self._meta_get(conn, "built_at")
            out["last_sync_at"] = self._meta_get(conn, "last_sync_at")
            out["last_reconcile_at"] = self._meta_get(conn, "last_reconcile_at")
        except sqlite3.Error as e:
            out["state"] = "error"
            out["error"] = str(e)
        finally:
            conn.close()
        return out

    def rowids_matching(self, query: str, limit: int | None = None) -> list[int]:
        """Message ROWIDs whose body matches every token of `query`, newest
        (== highest rowid) first, capped at `limit` (default fts_max_hits).

        Absent index or token-less query → []. Never raises on hostile
        query text: match_expr() sanitizes, and any residual FTS error is
        logged and swallowed — search degradation must never block reads.
        """
        expr = match_expr(query)
        if not expr or not self.available():
            return []
        cap = int(limit) if limit else config.fts_max_hits()
        conn = self._open_ro()
        try:
            rows = conn.execute(
                "SELECT rowid FROM body_fts WHERE body_fts MATCH ? "
                "ORDER BY rowid DESC LIMIT ?",
                (expr, cap),
            ).fetchall()
        except sqlite3.OperationalError as e:
            get_logger().warning("fts: MATCH failed for %r: %s", expr, e)
            return []
        finally:
            conn.close()
        return [int(r["rowid"]) for r in rows]

    # ------------------------------------------------------------------ #
    # write side                                                         #
    # ------------------------------------------------------------------ #

    def build(self, limit: int | None = None) -> dict:
        """Crawl every Envelope Index row above the high-water mark.

        Resumable: commits per batch, so an interrupted build continues
        where it stopped. `limit` bounds the number of documents (CLI
        --limit; smoke runs)."""
        t0 = time.monotonic()
        conn = self._open_rw()
        try:
            stats = self._crawl(conn, max_docs=limit, deadline=None)
            if not stats.get("skipped"):
                self._stamp(conn, "built_at")
        finally:
            conn.close()
        stats["elapsed"] = round(time.monotonic() - t0, 3)
        return stats

    def incremental(self, max_docs: int | None = None,
                    budget: float | None = None) -> dict:
        """One bounded catch-up pass: crawl new rowids, then retry due
        `missing` docs. Returns {"skipped": "busy"} when another writer
        holds the db and nothing could be done — callers on the search
        path just proceed with the index as-is."""
        t0 = time.monotonic()
        deadline = (t0 + budget) if budget else None
        conn = self._open_rw()
        try:
            stats = self._crawl(conn, max_docs=max_docs, deadline=deadline)
            if stats.get("skipped") == "busy" and not stats["scanned"]:
                return {"skipped": "busy"}
            if not stats.get("skipped"):
                quota = (max_docs - stats["scanned"]) if max_docs else None
                self._retry_missing(conn, stats, max_docs=quota,
                                    deadline=deadline)
            if not stats.get("skipped"):
                self._stamp(conn, "last_sync_at")
        finally:
            conn.close()
        stats["elapsed"] = round(time.monotonic() - t0, 3)
        return stats

    def reconcile(self) -> dict:
        """Full rowid-set diff against the Envelope Index (hygiene, not
        correctness — stale rowids cannot surface phantoms because hits
        re-enter search through `m.ROWID IN (…)` against the live index).

        Vanished rowids are dropped; envelope rows missing below the
        high-water mark (anomalies — crawl covers everything above it)
        are re-indexed."""
        t0 = time.monotonic()
        conn = self._open_rw()
        try:
            envelope = self._envelope_rowid_set()
            hwm = int(self._meta_get(conn, "last_rowid", "0"))
            ours = {
                int(r["rowid"])
                for r in conn.execute("SELECT rowid FROM docs")
            }
            vanished = sorted(ours - envelope)
            holes = sorted(r for r in (envelope - ours) if r <= hwm)
            if not self._begin_immediate(conn):
                return {"skipped": "busy"}
            cur = conn.cursor()
            for rowid in vanished:
                cur.execute("DELETE FROM docs WHERE rowid = ?", (rowid,))
                cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
            recovered = 0
            if holes:
                urls = self._envelope_urls_for(holes)
                for rowid in holes:
                    if rowid in urls:
                        self._index_one(cur, rowid, urls[rowid])
                        recovered += 1
            self._meta_set(cur, "last_reconcile_at", _iso_now())
            conn.commit()
        finally:
            conn.close()
        return {
            "checked": len(envelope),
            "removed": len(vanished),
            "recovered": recovered,
            "elapsed": round(time.monotonic() - t0, 3),
        }

    def rebuild(self, limit: int | None = None) -> dict:
        """Drop the index files and build from scratch."""
        base = db_path()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{base}{suffix}").unlink(missing_ok=True)
        return self.build(limit=limit)

    # ------------------------------------------------------------------ #
    # internals: our db                                                  #
    # ------------------------------------------------------------------ #

    def _open_ro(self) -> sqlite3.Connection:
        uri = "file:" + urllib.parse.quote(str(db_path())) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _open_rw(self) -> sqlite3.Connection:
        """Open (creating on first use) the index db. The ONLY fts write
        seam: the directory comes from state adoption (the one door)."""
        path = state.State.resolve().adopt().fts / _DB_NAME
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # explicit BEGIN IMMEDIATE / COMMIT
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        if self._meta_get(conn, "schema_version") is None:
            if self._begin_immediate(conn):
                self._meta_set(conn.cursor(), "schema_version",
                               str(SCHEMA_VERSION))
                conn.commit()
        os.chmod(path, 0o600)
        return conn

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return True
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                get_logger().info("fts: writer busy, skipping (%s)", e)
                return False
            raise

    @staticmethod
    def _meta_get(conn: sqlite3.Connection, key: str,
                  default: str | None = None) -> str | None:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _meta_set(cur: sqlite3.Cursor, key: str, value: str) -> None:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _stamp(self, conn: sqlite3.Connection, key: str) -> None:
        if self._begin_immediate(conn):
            self._meta_set(conn.cursor(), key, _iso_now())
            conn.commit()

    # ------------------------------------------------------------------ #
    # internals: envelope index (read-only, fresh connection per query)  #
    # ------------------------------------------------------------------ #

    def _mail_dir(self) -> Path:
        if self._mail_base is None:
            self._mail_base = config.mail_dir()
        return self._mail_base

    def _envelope_conn(self) -> sqlite3.Connection:
        # Lazy import: apple_mail grows an fts hook in the search stage,
        # so fts must never import it at module load.
        from .sources.apple_mail import _connect_readonly

        return _connect_readonly(self._mail_dir() / "MailData" / "Envelope Index")

    def _deleted_filter(self, conn: sqlite3.Connection) -> str:
        if self._envelope_deleted is None:
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(messages)")
            }
            self._envelope_deleted = "deleted" in cols
        return "AND m.deleted = 0" if self._envelope_deleted else ""

    def _envelope_rows_after(self, hwm: int, limit: int) -> list[tuple[int, str]]:
        conn = self._envelope_conn()
        try:
            rows = conn.execute(
                f"""
                SELECT m.ROWID AS rowid, mb.url AS url
                  FROM messages m
                  JOIN mailboxes mb ON mb.ROWID = m.mailbox
                 WHERE m.ROWID > ? {self._deleted_filter(conn)}
                 ORDER BY m.ROWID
                 LIMIT ?
                """,
                (hwm, limit),
            ).fetchall()
            return [(int(r["rowid"]), r["url"] or "") for r in rows]
        finally:
            conn.close()

    def _envelope_urls_for(self, rowids: list[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        if not rowids:
            return out
        conn = self._envelope_conn()
        try:
            for i in range(0, len(rowids), 500):
                chunk = rowids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT m.ROWID AS rowid, mb.url AS url
                      FROM messages m
                      JOIN mailboxes mb ON mb.ROWID = m.mailbox
                     WHERE m.ROWID IN ({marks})
                       {self._deleted_filter(conn)}
                    """,
                    chunk,
                ).fetchall()
                for r in rows:
                    out[int(r["rowid"])] = r["url"] or ""
        finally:
            conn.close()
        return out

    def _envelope_rowid_set(self) -> set[int]:
        conn = self._envelope_conn()
        try:
            rows = conn.execute(
                "SELECT m.ROWID AS rowid FROM messages m "
                f"WHERE 1=1 {self._deleted_filter(conn)}"
            ).fetchall()
            return {int(r["rowid"]) for r in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # internals: indexing                                                #
    # ------------------------------------------------------------------ #

    def _crawl(self, conn: sqlite3.Connection, max_docs: int | None,
               deadline: float | None) -> dict:
        """Keyset-paginated pass over rowids above the high-water mark.
        One BEGIN IMMEDIATE txn per batch; hwm advances with each commit."""
        stats = {"scanned": 0, "indexed": 0, "partial": 0, "missing": 0,
                 "errors": 0, "retried": 0, "removed": 0}
        hwm = int(self._meta_get(conn, "last_rowid", "0"))
        while True:
            if max_docs is not None and stats["scanned"] >= max_docs:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            rows = self._envelope_rows_after(hwm, _BATCH_SIZE)
            if not rows:
                break
            if not self._begin_immediate(conn):
                stats["skipped"] = "busy"
                break
            cur = conn.cursor()
            done = 0
            for rowid, url in rows:
                if max_docs is not None and stats["scanned"] >= max_docs:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                outcome = self._index_one(cur, rowid, url)
                stats["errors" if outcome == "error" else outcome] += 1
                stats["scanned"] += 1
                hwm = rowid
                done += 1
            self._meta_set(cur, "last_rowid", str(hwm))
            conn.commit()
            if done < len(rows):  # quota or budget hit mid-batch
                break
        stats["last_rowid"] = hwm
        return stats

    def _retry_missing(self, conn: sqlite3.Connection, stats: dict,
                       max_docs: int | None, deadline: float | None) -> None:
        """Re-stat `missing` docs whose backoff has elapsed. A rowid that
        also vanished from the Envelope Index is dropped (mini-reconcile)."""
        now = time.time()
        rows = conn.execute(
            "SELECT rowid, attempts, last_attempt FROM docs "
            "WHERE status = 'missing' AND attempts < ? ORDER BY rowid",
            (_MAX_ATTEMPTS,),
        ).fetchall()
        due = [
            int(r["rowid"]) for r in rows
            if now - float(r["last_attempt"]) >= _retry_delay(int(r["attempts"]))
        ]
        if max_docs is not None:
            due = due[: max(0, max_docs)]
        if not due:
            return
        # A mailbox with no local store may have synced since we cached its
        # absence — drop negative entries so retries re-discover it.
        self._data_dir_cache = {
            url: d for url, d in self._data_dir_cache.items() if d is not None
        }
        urls = self._envelope_urls_for(due)
        if not self._begin_immediate(conn):
            stats["skipped"] = "busy"
            return
        cur = conn.cursor()
        for rowid in due:
            if deadline is not None and time.monotonic() >= deadline:
                break
            url = urls.get(rowid)
            if url is None:
                cur.execute("DELETE FROM docs WHERE rowid = ?", (rowid,))
                cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
                stats["removed"] += 1
                continue
            outcome = self._index_one(cur, rowid, url)
            if outcome != "missing":
                stats["errors" if outcome == "error" else outcome] += 1
            stats["retried"] += 1
        conn.commit()

    def _index_one(self, cur: sqlite3.Cursor, rowid: int, url: str) -> str:
        """Index a single message; returns its docs.status. Caller holds
        the write transaction."""
        now = time.time()
        data_dir = self._data_dir(url)
        path = find_emlx_path(data_dir, rowid) if data_dir else None
        if path is None:
            return self._record(cur, rowid, "missing", now, 0)
        try:
            text = self._extract_text(path)
        except Exception as e:  # any parse failure — never abort a crawl
            get_logger().warning("fts: cannot index rowid %s (%s): %s",
                                 rowid, path, e)
            return self._record(cur, rowid, "error", now, 0)
        status = "partial" if path.name.endswith(".partial.emlx") else "indexed"
        cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
        cur.execute("INSERT INTO body_fts(rowid, body) VALUES (?, ?)",
                    (rowid, text))
        return self._record(cur, rowid, status, now,
                            len(text.encode("utf-8", "replace")))

    @staticmethod
    def _record(cur: sqlite3.Cursor, rowid: int, status: str,
                now: float, nbytes: int) -> str:
        if status in ("missing", "error"):
            cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
        cur.execute(
            """
            INSERT INTO docs(rowid, status, attempts, last_attempt, bytes)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(rowid) DO UPDATE SET
                status = excluded.status,
                attempts = docs.attempts + 1,
                last_attempt = excluded.last_attempt,
                bytes = excluded.bytes
            """,
            (rowid, status, now, nbytes),
        )
        return status

    def _data_dir(self, url: str) -> Path | None:
        """Per-mailbox-url Data dir, cached — bulk crawls resolve each
        mailbox once instead of globbing per message."""
        if url not in self._data_dir_cache:
            try:
                self._data_dir_cache[url] = mailbox_data_dir(
                    self._mail_dir(), url)
            except (ValueError, FileNotFoundError):
                self._data_dir_cache[url] = None
        return self._data_dir_cache[url]

    def _extract_text(self, path: Path) -> str:
        # Lazy import — see _envelope_conn.
        from .sources.apple_mail import _parse_emlx

        parsed = _parse_emlx(path, config.fts_doc_cap_bytes())
        return parsed["body_text"]


# ---------------------------------------------------------------------- #
# launchd install (mirrors email_mcp.dispatcher)                          #
# ---------------------------------------------------------------------- #


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _log_path() -> Path:
    return config.fts_dir().parent / "fts.log"


def _plist_content() -> str:
    # Same PATH-baking rationale as the dispatcher: launchd's bare PATH can
    # resolve the wrong python3; the installing shell's PATH is known-good.
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
    <key>StandardOutPath</key><string>{_log_path()}</string>
    <key>StandardErrorPath</key><string>{_log_path()}</string>
</dict>
</plist>
"""


def install_launchd() -> str:
    state.State.resolve().adopt()  # the agent's log lands in the state root
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
    return f"installed {LAUNCHD_LABEL} (daily 03:30 --sync), log: {_log_path()}"


def uninstall_launchd() -> str:
    plist = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                   capture_output=True)
    if plist.exists():
        plist.unlink()
    return f"removed {LAUNCHD_LABEL}"


# ---------------------------------------------------------------------- #
# CLI                                                                    #
# ---------------------------------------------------------------------- #


def _reconcile_due(idx: FtsIndex) -> bool:
    stamp = idx.status().get("last_reconcile_at")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - last
    return age.days >= config.fts_reconcile_days()


def _sync(idx: FtsIndex, limit: int | None) -> dict:
    out = idx.incremental(max_docs=limit)
    if out.get("skipped") == "busy":
        return out
    if _reconcile_due(idx):
        out["reconcile"] = idx.reconcile()
    return out


def _print_status(st: dict, as_json: bool) -> None:
    if as_json:
        json.dump(st, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if st["state"] == "absent":
        print(f"index: {st['db']} (absent)")
        print(f"build it: {st['remedy']}")
        return
    if st["state"] == "error":
        print(f"index: {st['db']} (error: {st.get('error')})")
        return
    d = st["docs"]
    mb = st["db_bytes"] / 1_048_576
    print(f"index: {st['db']} ({st['state']}, {mb:.1f} MB, "
          f"schema v{st['schema_version']})")
    print(f"docs: {d['indexed']} indexed, {d['partial']} partial, "
          f"{d['missing']} missing, {d['error']} error "
          f"(hwm rowid {st['last_rowid']})")
    print(f"built: {st['built_at'] or '-'}  synced: {st['last_sync_at'] or '-'}  "
          f"reconciled: {st['last_reconcile_at'] or '-'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="email_mcp.fts",
        description="Local FTS5 body index over Apple Mail.",
    )
    parser.add_argument("--build", action="store_true",
                        help="crawl all unindexed messages (resumable)")
    parser.add_argument("--sync", action="store_true",
                        help="incremental catch-up + miss retries "
                             "(+ weekly reconcile)")
    parser.add_argument("--reconcile", action="store_true",
                        help="full rowid-set diff against the Envelope Index")
    parser.add_argument("--rebuild", action="store_true",
                        help="drop the index and build from scratch")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="cap documents processed this run")
    parser.add_argument("--install-launchd", action="store_true")
    parser.add_argument("--uninstall-launchd", action="store_true")
    args = parser.parse_args(argv)

    if args.install_launchd:
        print(install_launchd())
        return 0
    if args.uninstall_launchd:
        print(uninstall_launchd())
        return 0

    idx = FtsIndex()
    if args.status:
        _print_status(idx.status(), args.json)
        return 0

    try:
        if args.build:
            out = idx.build(limit=args.limit)
        elif args.rebuild:
            out = idx.rebuild(limit=args.limit)
        elif args.reconcile:
            out = idx.reconcile()
        elif args.sync:
            out = _sync(idx, args.limit)
        else:
            parser.print_help()
            return 2
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, indent=2 if args.json else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
