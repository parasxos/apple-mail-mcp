"""Local FTS5 full-body index over Apple Mail .emlx files.

The Envelope Index only carries first-line snippets, so search silently
misses message bodies. This module maintains a private SQLite database
(<state root>/fts/fts.db, 0700) with these tables:

  meta      key/value: schema_version, last_rowid high-water mark, timestamps
  docs      per-message ledger: status indexed|partial|missing|error,
            attempts, last_attempt, bytes,
            source local|graph|graph_miss|graph_none|imap|imap_miss
  body_fts  plain FTS5 over extracted body text, rowid == messages.ROWID
  absence   repeated absence observations before reconciliation removes a doc
  backfill_reasons  locally recorded reasons a provider lookup is unavailable

Source is the evidence ledger of the backfill lanes (Graph for ews://
mailboxes, IMAP for imap:// mailboxes whose identity declares an
[name.imap] table):
  local       the doc is (or may yet be) served from Mail's own store
  graph       body fetched from the Exchange mailbox's server copy
  imap        body fetched from the IMAP mailbox's server copy
  graph_miss  every configured graph identity answered and none holds it
              — CONFIRMED absent, relative to the identity set that was
              asked; changing that set revokes every graph_miss stamp
  imap_miss   the IMAP lane's graph_miss, under the same revocation rule
  graph_none  no configured lane can ask for this doc (a mailbox no lane
              covers, or a partial file with no Message-ID header) —
              scoped, like the misses, to the lane set that concluded
              it: changing the set revokes and re-derives these stamps
A stamp is only ever placed on confirmed evidence. Errors, unreadable
files and unanswered identities DEFER a doc — absence of evidence must
never read as evidence of absence.

Design points (docs/v0.8-concept.md, movement 1):
  * Apple's store is never written. Bodies come from .emlx first; where
    Mail never downloaded one (a .partial.emlx ceiling the crawler
    cannot raise — first-user body-gap report, 2026-08-06), backfill()
    fetches the mailbox's own server copy — Graph for Exchange, IMAP
    for declared imap identities (the Gmail 15k-partials report,
    2026-08-24) — and records source='graph'/'imap', so provenance is
    never ambiguous. get_email may serve that text with a declared
    body_source; search covers it like any other doc.
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
  python -m email_mcp.fts --sync               # catch up + retries (+ weekly reconcile + backfill)
  python -m email_mcp.fts --backfill           # server-side bodies for local holes
  python -m email_mcp.fts --reconcile          # full rowid-set diff
  python -m email_mcp.fts --rebuild            # fresh build; useful cached bodies carried over
  python -m email_mcp.fts --status [--json]
  python -m email_mcp.fts --install-launchd    # com.email-mcp.fts, 03:30 daily --sync
  python -m email_mcp.fts --uninstall-launchd
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, state
from . import fts_runtime
from .log import get_logger
from .fts_reporting import coverage_report
from .sources.apple_mail_paths import find_emlx_path, mailbox_data_dir

SCHEMA_VERSION = 3
LAUNCHD_LABEL = "com.email-mcp.fts"

_DB_NAME = "fts.db"
_BUSY_TIMEOUT_MS = 5000
_BATCH_SIZE = 2000
_MAX_ATTEMPTS = 6
_ABSENCE_GRACE_SECONDS = 24 * 3600
_TRANSLATE_CHUNK = 100  # EWS ids per translateExchangeIds call
# Per-pass identity health (backfill): an identity that keeps erroring
# and has answered NOTHING is dead for the pass (revoked token, outage);
# one that answered before is given more rope (a poisoned doc or chunk
# is the doc's problem, not the identity's) but still caps out.
_IDENT_FAIL_FAST = 3
_IDENT_ERROR_CAP = 25

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
    bytes        INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS docs_status ON docs(status);
CREATE TABLE IF NOT EXISTS absence (
    rowid INTEGER PRIMARY KEY,
    first_absent REAL NOT NULL,
    last_absent REAL NOT NULL,
    observations INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_reasons (
    rowid INTEGER PRIMARY KEY,
    reason TEXT NOT NULL
);
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

    def __init__(self, mail_base: Path | None = None,
                 db: Path | None = None) -> None:
        # Resolved lazily: status()/rowids_matching() must work (and --status
        # must print) on a machine where config.mail_dir() would raise.
        self._mail_base = mail_base
        # An index instance addresses ONE db file — the canonical one by
        # default; rebuild()'s scratch instance overrides the path.
        self._db = db
        self._data_dir_cache: dict[str, Path | None] = {}
        self._envelope_deleted: bool | None = None

    # ------------------------------------------------------------------ #
    # read side                                                          #
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        return (self._db or db_path()).exists()

    def status(self) -> dict:
        path = self._db or db_path()
        out: dict = {
            "state": "absent",
            "db": str(path),
            "db_bytes": 0,
            "schema_version": None,
            "last_rowid": 0,
            "docs": {"indexed": 0, "partial": 0, "missing": 0, "error": 0,
                     "total": 0, "backfilled": 0, "local_retry_exhausted": 0},
            "backlog": None,
            "recovery": {
                "state": "not_attempted", "last_attempt_at": None,
                "last_error": None, "no_lane": 0, "no_message_id": 0,
                "no_remote_id": 0, "unavailable_mailbox": 0, "unclassified_unavailable": 0,
                "confirmed_misses": 0,
            },
            "cleanup": {
                "grace_seconds": _ABSENCE_GRACE_SECONDS, "pending_removal": 0,
                "removed_total": 0, "last_removed_at": None,
                "recovered_total": 0, "last_recovered_at": None,
                "reappeared_total": 0, "last_reappeared_at": None,
                "retry_deferred_total": 0,
            },
            "built_at": None,
            "last_sync_at": None,
            "last_reconcile_at": None,
            "last_backfill_at": None,
            "last_backfill_error": None,
        }
        if not path.exists():
            out["remedy"] = "python -m email_mcp.fts --build"
            out.update(coverage_report(out))
            return out
        try:
            conn = self._open_ro()
        except sqlite3.Error as e:
            out["state"] = "error"
            out["error"] = str(e)
            out.update(coverage_report(out))
            return out
        try:
            conn.execute("BEGIN")
            counts = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM docs GROUP BY status"
                )
            }
            for key in ("indexed", "partial", "missing", "error"):
                out["docs"][key] = counts.get(key, 0)
            out["docs"]["total"] = sum(counts.values())
            out["docs"]["local_retry_exhausted"] = int(conn.execute(
                "SELECT COUNT(*) FROM docs WHERE status IN "
                "('missing', 'partial', 'error') AND attempts >= ?",
                (_MAX_ATTEMPTS,),
            ).fetchone()[0])
            try:
                sources = {r[0]: int(r[1]) for r in conn.execute(
                    "SELECT source, COUNT(*) FROM docs GROUP BY source")}
                out["docs"]["backfilled"] = sum(
                    sources.get(key, 0) for key in ("graph", "imap"))
            except sqlite3.OperationalError:  # pre-v2 db: no source column
                sources = {}
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            recovery = out["recovery"]
            reasons = {}
            if "backfill_reasons" in tables:
                reasons = {r[0]: int(r[1]) for r in conn.execute(
                    "SELECT r.reason, COUNT(*) FROM backfill_reasons r "
                    "JOIN docs d ON d.rowid=r.rowid WHERE d.status != 'indexed' "
                    "GROUP BY r.reason")}
            for reason in ("no_lane", "no_message_id", "no_remote_id", "unavailable_mailbox"):
                recovery[reason] = reasons.get(reason, 0)
            recovery["unclassified_unavailable"] = max(
                0, sources.get("graph_none", 0)
                - recovery["no_lane"] - recovery["no_message_id"])
            recovery["confirmed_misses"] = sum(
                sources.get(key, 0) for key in ("graph_miss", "imap_miss"))
            recovery["last_attempt_at"] = self._meta_get(conn, "last_backfill_attempt_at")
            recovery["state"] = self._meta_get(conn, "last_backfill_state", "not_attempted")
            if "absence" in tables:
                out["cleanup"]["pending_removal"] = int(conn.execute(
                    "SELECT COUNT(*) FROM absence a JOIN docs d ON d.rowid=a.rowid"
                ).fetchone()[0])
            for name in ("removed", "recovered", "reappeared"):
                out["cleanup"][f"{name}_total"] = int(self._meta_get(
                    conn, f"{name}_total", "0"))
                out["cleanup"][f"last_{name}_at"] = self._meta_get(
                    conn, f"last_{name}_at")
            out["cleanup"]["retry_deferred_total"] = int(self._meta_get(
                conn, "retry_deferred_total", "0"))
            out["state"] = "ready"
            out["db_bytes"] = path.stat().st_size
            raw_version = self._meta_get(conn, "schema_version")
            out["schema_version"] = int(raw_version) if raw_version else None
            out["last_rowid"] = int(self._meta_get(conn, "last_rowid", "0"))
            out["built_at"] = self._meta_get(conn, "built_at")
            out["last_sync_at"] = self._meta_get(conn, "last_sync_at")
            out["last_reconcile_at"] = self._meta_get(conn, "last_reconcile_at")
            out["last_backfill_at"] = self._meta_get(conn, "last_backfill_at")
            out["last_backfill_error"] = self._meta_get(
                conn, "last_backfill_error")
            recovery["last_error"] = out["last_backfill_error"]
            if recovery["last_error"]:
                recovery["state"] = "error"
        except sqlite3.Error as e:
            out["state"] = "error"
            out["error"] = str(e)
        finally:
            conn.close()
        if out["state"] == "ready":
            try:
                out["backlog"] = self._envelope_backlog(out["last_rowid"])
            except (OSError, ValueError, sqlite3.Error):
                pass
        out.update(coverage_report(out))
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
        except sqlite3.Error as e:
            # The whole class, not just OperationalError: a corrupt db
            # raises plain DatabaseError, and the docstring's promise is
            # that search degrades to snippet-only, never dies (RC FM5).
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
        # A full crawl must never trust cached absence: a mailbox with no
        # local store when this instance last looked may have one now
        # (rebuild-after-backfill hit exactly this, 2026-08-07).
        self._data_dir_cache = {
            url: d for url, d in self._data_dir_cache.items()
            if d is not None
        }
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
        """Repair gaps and prune only repeated absence across the grace period."""
        t0 = time.monotonic()
        conn = self._open_rw()
        try:
            if not self._begin_immediate(conn):
                return {"skipped": "busy"}
            envelope = self._envelope_rowid_set()
            hwm = int(self._meta_get(conn, "last_rowid", "0"))
            ours = {
                int(r["rowid"])
                for r in conn.execute("SELECT rowid FROM docs")
            }
            vanished = sorted(ours - envelope)
            holes = sorted(r for r in (envelope - ours) if r <= hwm)
            cur = conn.cursor()
            pending = {int(r["rowid"]): r for r in conn.execute(
                "SELECT rowid, first_absent, last_absent, observations FROM absence")}
            reappeared = 0
            for rowid in pending.keys() & envelope:
                cur.execute("DELETE FROM absence WHERE rowid=?", (rowid,))
                reappeared += 1
            now = time.time()
            removed = 0
            for rowid in vanished:
                previous = pending.get(rowid)
                if (previous is not None
                        and now - previous["first_absent"] >= _ABSENCE_GRACE_SECONDS
                        and now > previous["last_absent"]):
                    for table in ("docs", "body_fts", "absence", "backfill_reasons"):
                        cur.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
                    removed += 1
                else:
                    cur.execute(
                        "INSERT INTO absence VALUES (?, ?, ?, 1) "
                        "ON CONFLICT(rowid) DO UPDATE SET last_absent=excluded.last_absent, "
                        "observations=absence.observations+1", (rowid, now, now))
            recovered = 0
            if holes:
                urls = self._envelope_urls_for(holes)
                for rowid in holes:
                    if rowid in urls:
                        self._index_one(cur, rowid, urls[rowid])
                        recovered += 1
            for name, count in (("removed", removed), ("recovered", recovered),
                                ("reappeared", reappeared)):
                self._count_event(cur, name, count)
            self._meta_set(cur, "last_reconcile_at", _iso_now())
            waiting = int(cur.execute("SELECT COUNT(*) FROM absence").fetchone()[0])
            conn.commit()
        finally:
            conn.close()
        return {
            "checked": len(envelope),
            "removed": removed,
            "recovered": recovered,
            "reappeared": reappeared,
            "pending_removal": waiting,
            "elapsed": round(time.monotonic() - t0, 3),
        }

    def backfill(self, max_docs: int | None = None) -> dict:
        """Fill index holes from the mailbox's own server copy.

        Mail.app's local store has a ceiling no crawling raises
        (first-user body-gap report, 2026-08-06): bodies it never
        downloaded exist as headers-only .partial.emlx files — or, for
        EWS accounts, as Envelope rows with NO file at all (97% of a
        live Exchange account). Two lanes recover them, each answering only
        its own mailboxes:

          graph (ews:// mailboxes, any graph-enabled identity)
            partial  — keyed by the RFC Message-ID read from the partial
                       file's OWN headers (the Envelope Index stores
                       only a hash);
            missing  — keyed by the Envelope row's EWS remote_id,
                       translated to a Graph REST id in bulk
                       (translateExchangeIds), then fetched directly.
          imap (imap:// mailboxes, any identity with an [name.imap]
                table — the Gmail 15k-partials report, 2026-08-24)
            partial only — same Message-ID key, searched over IMAP. A
            storeless imap row has no local Message-ID to join on, so
            that class stays out of the lane's reach.

        Hits are indexed with source='graph'/'imap'. Verdicts follow
        the evidence rules of the module docstring: a miss is stamped
        source='graph_miss'/'imap_miss' only when EVERY identity in the
        lane answered without error and none holds the message (empty
        lookup / 404 / untranslatable id) — and every stamp, graph_none
        included, is scoped to the lane set that placed it: when the
        set changes (an identity added or removed, a NEW LANE
        configured), all three stamp kinds are revoked and re-derived,
        so yesterday's "no lane can ask" never outlives today's lanes.
        Everything else — a GraphError/ImapError, an unreadable partial
        file, an identity that could not be asked — DEFERS the doc
        untouched; an identity that keeps erroring and answers nothing
        is retired for the pass, and its trouble is recorded in meta
        last_backfill_error so status() and doctor can surface it.
        Network calls happen OUTSIDE write transactions: server latency
        must never hold the index lock a search is waiting on."""
        t0 = time.monotonic()
        stats = {"candidates": 0, "backfilled": 0, "misses": 0,
                 "no_message_id": 0, "no_remote_id": 0,
                 "no_lane": 0, "deferred": 0}
        self._backfill_config_error = None
        idents = self._graph_identities()
        imap_idents = self._imap_identities()
        if not idents and not imap_idents:
            stats["skipped"] = "no_backfill_identity"
            if self.available():
                conn = self._open_rw()
                try:
                    self._note_backfill_health(
                        conn, self._backfill_config_error, state="no_identities")
                finally:
                    conn.close()
            return stats
        conn = self._open_rw()
        try:
            fingerprint = ";".join((
                "graph:" + ",".join(sorted(
                    str(getattr(i, "name", "")) for i in idents)),
                "imap:" + ",".join(sorted(
                    str(getattr(i, "name", "")) for i in imap_idents)),
            ))
            if self._meta_get(conn, "backfill_identities") != fingerprint:
                # A miss means "absent from every mailbox asked", a
                # graph_none "no lane could ask". A different lane set
                # can answer differently — revoke every conclusion so
                # the new set gets asked (or re-concluded, cheaply, for
                # the truly unanswerable).
                if not self._begin_immediate(conn):
                    stats["skipped"] = "busy"
                    return stats
                cur = conn.cursor()
                cur.execute("UPDATE docs SET source = 'local' "
                            "WHERE source IN ('graph_miss', 'imap_miss', "
                            "'graph_none')")
                cur.execute("DELETE FROM backfill_reasons")
                self._meta_set(cur, "backfill_identities", fingerprint)
                conn.commit()
            try:
                classes = {
                    status: [int(r["rowid"]) for r in conn.execute(
                        "SELECT rowid FROM docs WHERE status = ? "
                        "AND source = 'local' ORDER BY rowid DESC",
                        (status,))]
                    for status in ("partial", "missing")
                }
            except sqlite3.OperationalError:  # pre-v2 db mid-migration
                stats["skipped"] = "schema_not_migrated"
                return stats
            expected = {rid: status for status, rowids in classes.items() for rid in rowids}

            def _eligible(rid: int) -> bool:
                row = conn.execute("SELECT status, source FROM docs WHERE rowid=?", (rid,)).fetchone()
                return (row is not None and row["source"] == "local"
                        and row["status"] == expected[rid]
                        and self._meta_get(conn, "backfill_identities") == fingerprint)

            meta = self._envelope_backfill_meta(
                classes["partial"] + classes["missing"])
            p_graph: list[tuple[int, str]] = []      # (rowid, mailbox url)
            p_imap: list[tuple[int, str]] = []       # (rowid, mailbox url)
            m_todo: list[tuple[int, str]] = []       # (rowid, ews id)
            no_lane: list[int] = []
            no_remote: list[int] = []
            unavailable: list[int] = []
            for rid in classes["partial"]:
                url, _ = meta.get(rid, ("", None))
                if not url:
                    unavailable.append(rid)
                elif url.startswith("ews://") and idents:
                    p_graph.append((rid, url))
                elif url.startswith("imap://") and imap_idents:
                    p_imap.append((rid, url))
                else:
                    no_lane.append(rid)
            for rid in classes["missing"]:
                url, remote_id = meta.get(rid, ("", None))
                if not url:
                    unavailable.append(rid)
                elif not (url.startswith("ews://") and idents):
                    no_lane.append(rid)
                elif not remote_id:
                    no_remote.append(rid)
                else:
                    m_todo.append((rid, remote_id))
            resolved = [rid for rid, _ in p_graph + p_imap + m_todo]
            if no_lane or no_remote or resolved or unavailable:
                # No configured lane's to answer: stamp once (one
                # set-based txn), so no later pass re-derives the whole
                # estate to re-conclude it — revocable, like every
                # stamp, by the lane-set fingerprint above.
                if not self._begin_immediate(conn):
                    stats["skipped"] = "busy"
                    return stats
                cur = conn.cursor()
                eligible = {int(r[0]) for r in cur.execute(
                    "SELECT rowid, status FROM docs WHERE source='local' "
                    "AND status IN ('partial', 'missing')") if expected.get(int(r[0])) == r[1]}
                if self._meta_get(conn, "backfill_identities") != fingerprint:
                    eligible.clear()
                stats["deferred"] += len(set(no_lane + no_remote + resolved) - eligible) + len(unavailable)
                no_lane = [rid for rid in no_lane if rid in eligible]
                no_remote = [rid for rid in no_remote if rid in eligible]
                unavailable = [rid for rid in unavailable if rid in eligible]
                resolved = [rid for rid in resolved if rid in eligible]
                p_graph = [(rid, url) for rid, url in p_graph if rid in eligible]
                p_imap = [(rid, url) for rid, url in p_imap if rid in eligible]
                m_todo = [(rid, remote) for rid, remote in m_todo if rid in eligible]
                cur.executemany("DELETE FROM backfill_reasons WHERE rowid=?",
                                [(rid,) for rid in resolved])
                for i in range(0, len(no_lane), 500):
                    chunk = no_lane[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    cur.execute(f"UPDATE docs SET source = 'graph_none' "
                                f"WHERE rowid IN ({marks})", chunk)
                for reason, rowids in (("no_lane", no_lane), ("no_remote_id", no_remote),
                                      ("unavailable_mailbox", unavailable)):
                    cur.executemany(
                        "INSERT INTO backfill_reasons VALUES (?, ?) "
                        "ON CONFLICT(rowid) DO UPDATE SET reason=excluded.reason",
                        [(rid, reason) for rid in rowids])
                conn.commit()
            stats["no_lane"] = len(no_lane)
            stats["no_remote_id"] = len(no_remote)
            if max_docs is not None:
                budget = max(0, max_docs)
                p_graph = p_graph[:budget]
                p_imap = p_imap[: budget - len(p_graph)]
                m_todo = m_todo[: budget - len(p_graph) - len(p_imap)]
            stats["candidates"] = len(p_graph) + len(p_imap) + len(m_todo)

            # Per-pass identity health: evidence accumulates, verdicts
            # follow it (constants _IDENT_FAIL_FAST / _IDENT_ERROR_CAP).
            errors: dict[str, int] = {}
            successes: dict[str, int] = {}
            last_error: dict[str, str] = {}

            def _name(ident) -> str:
                return str(getattr(ident, "name", ""))

            def _dead(ident) -> bool:
                n = _name(ident)
                e = errors.get(n, 0)
                return (e >= _IDENT_ERROR_CAP
                        or (e >= _IDENT_FAIL_FAST
                            and not successes.get(n, 0)))

            def _any_live(lane) -> bool:
                return any(not _dead(i) for i in lane)

            def _count_error(ident, what: str, e: Exception) -> None:
                n = _name(ident)
                errors[n] = errors.get(n, 0) + 1
                last_error[n] = str(e)
                get_logger().warning(
                    "fts backfill: identity %r %s failed: %s", n, what, e)

            def _fetch_confirmed(fetch, lane) -> tuple[dict | None, bool]:
                """Ask every identity in the lane. (body, confirmed):
                body on a hit; confirmed only when EVERY identity
                answered without error and none had it — the sole
                evidence that justifies a miss stamp."""
                confirmed = True
                for ident in lane:
                    if _dead(ident):
                        confirmed = False
                        continue
                    try:
                        body = fetch(ident)
                    except Exception as e:  # Graph/ImapError: no evidence
                        _count_error(ident, "lookup", e)
                        confirmed = False
                        continue
                    successes[_name(ident)] = \
                        successes.get(_name(ident), 0) + 1
                    if body is not None:
                        return body, True
                return None, confirmed

            def _store_hit(rid: int, body: dict, source: str) -> bool:
                text = self._remote_text(body)
                if not self._begin_immediate(conn):
                    stats["skipped"] = "busy"
                    return False
                if not _eligible(rid):
                    conn.rollback()
                    stats["deferred"] += 1
                    return True
                cur = conn.cursor()
                cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rid,))
                cur.execute(
                    "INSERT INTO body_fts(rowid, body) VALUES (?, ?)",
                    (rid, text))
                self._record(cur, rid, "indexed", time.time(),
                             len(text.encode("utf-8", "replace")),
                             source=source)
                conn.commit()
                stats["backfilled"] += 1
                if stats["backfilled"] % 500 == 0:
                    get_logger().info(
                        "fts backfill: %s bodies fetched, %s to go",
                        stats["backfilled"],
                        stats["candidates"] - stats["backfilled"]
                        - stats["misses"] - stats["no_message_id"])
                return True

            def _stamp_doc(rid: int, source: str, counter: str) -> bool:
                reason = "no_message_id" if counter == "no_message_id" else None
                if not self._begin_immediate(conn):
                    stats["skipped"] = "busy"
                    return False
                if not _eligible(rid):
                    conn.rollback()
                    stats["deferred"] += 1
                    return True
                conn.execute("UPDATE docs SET source=?, last_attempt=? WHERE rowid=?",
                             (source, time.time(), rid))
                conn.execute("DELETE FROM backfill_reasons WHERE rowid=?", (rid,))
                if reason:
                    conn.execute("INSERT INTO backfill_reasons VALUES (?, ?)", (rid, reason))
                conn.commit()
                stats[counter] += 1
                return True

            def _run_partials(todo, lane, fetch, hit_src, miss_src) -> None:
                """One lane's partial pass — Message-ID from the file's
                own headers, then ask the lane's identities."""
                for rid, url in todo:
                    if not _any_live(lane) or "skipped" in stats:
                        return
                    try:
                        mid = self._message_id_from_partial(rid, url)
                    except Exception as e:  # unreadable now ≠ unaskable
                        get_logger().info(
                            "fts backfill: cannot read partial %s (%s) — "
                            "deferred", rid, e)
                        stats["deferred"] += 1
                        continue
                    if mid is None:  # the file says: no join key, ever
                        if not _stamp_doc(rid, "graph_none",
                                          "no_message_id"):
                            return
                        continue
                    body, confirmed = _fetch_confirmed(
                        lambda i, mid=mid: fetch(i, mid), lane)
                    if body is not None:
                        if not _store_hit(rid, body, hit_src):
                            return
                    elif confirmed:
                        if not _stamp_doc(rid, miss_src, "misses"):
                            return
                    else:
                        stats["deferred"] += 1

            # class 1: partials, one pass per lane
            _run_partials(p_graph, idents, self._fetch_remote_body,
                          "graph", "graph_miss")
            _run_partials(p_imap, imap_idents, self._fetch_imap_body,
                          "imap", "imap_miss")

            # class 2: storeless — remote_id → REST id → body (graph only)
            for i in range(0, len(m_todo), _TRANSLATE_CHUNK):
                if not _any_live(idents) or "skipped" in stats:
                    break
                chunk = m_todo[i:i + _TRANSLATE_CHUNK]
                mapping: dict[str, str] = {}
                translated_by_all = True
                for ident in idents:
                    if _dead(ident):
                        translated_by_all = False
                        continue
                    left = [e for _, e in chunk if e not in mapping]
                    if not left:
                        break
                    try:
                        mapping.update(self._translate_ews_ids(ident, left))
                    except Exception as e:  # chunk deferred, not the pass
                        _count_error(ident, "translate", e)
                        translated_by_all = False
                        continue
                    successes[_name(ident)] = \
                        successes.get(_name(ident), 0) + 1
                for rid, ews_id in chunk:
                    if "skipped" in stats:
                        break
                    rest = mapping.get(ews_id)
                    if rest is None:
                        if translated_by_all:  # unaddressable by every one
                            if not _stamp_doc(rid, "graph_miss", "misses"):
                                break
                        else:
                            stats["deferred"] += 1
                        continue
                    body, confirmed = _fetch_confirmed(
                        lambda i, rest=rest:
                        self._fetch_remote_body_by_id(i, rest), idents)
                    if body is not None:
                        if not _store_hit(rid, body, "graph"):
                            break
                    elif confirmed:
                        if not _stamp_doc(rid, "graph_miss", "misses"):
                            break
                    else:
                        stats["deferred"] += 1

            if errors:
                stats["identity_errors"] = dict(errors)
                if not _any_live(idents + imap_idents):
                    stats["aborted"] = "every backfill identity failing: " \
                        + "; ".join(f"{n}: {m}"
                                    for n, m in sorted(last_error.items()))
            summary = "; ".join(
                f"identity {n!r}: {errors[n]} error(s), last: {last_error[n]}"
                for n in sorted(errors)) or self._backfill_config_error
            self._note_backfill_health(conn, summary)
            if stats["backfilled"] or stats["misses"] \
                    or stats["no_message_id"]:
                self._stamp(conn, "last_backfill_at")
        finally:
            conn.close()
        stats["elapsed"] = round(time.monotonic() - t0, 3)
        return stats

    def _note_backfill_health(self, conn: sqlite3.Connection,
                              summary: str | None, state: str = "ready") -> None:
        """Record (or clear) the pass's identity trouble in meta — the
        one place status() and doctor read, so a backfill that silently
        does nothing every night is a visible state, not a log line."""
        if not self._begin_immediate(conn):
            return
        cur = conn.cursor()
        if summary is None:
            cur.execute("DELETE FROM meta WHERE key = 'last_backfill_error'")
        else:
            self._meta_set(cur, "last_backfill_error", summary)
        self._meta_set(cur, "last_backfill_state", "error" if summary else state)
        self._meta_set(cur, "last_backfill_attempt_at", _iso_now())
        conn.commit()

    def backfilled_text(self, rowid: int) -> str | None:
        """Server-fetched body text for a doc whose local .emlx has none
        — the ONE read get_email may serve, with declared provenance
        (body_source). None for local-sourced docs, absent rowids, or a
        pre-v2 index."""
        if not self.available():
            return None
        conn = self._open_ro()
        try:
            try:
                row = conn.execute(
                    "SELECT f.body AS body FROM docs d "
                    "JOIN body_fts f ON f.rowid = d.rowid "
                    "WHERE d.rowid = ? AND d.source IN ('graph', 'imap')",
                    (rowid,)).fetchone()
            except sqlite3.OperationalError:  # pre-v2 db: no source column
                return None
            return str(row["body"]) if row and row["body"] else None
        finally:
            conn.close()

    def rebuild(self, limit: int | None = None) -> dict:
        """Build beside the live index, then merge in one SQLite transaction.

        The writer lock covers salvage and promotion. Updating the existing
        database preserves connections opened by a concurrent backfill.
        Only an unreadable, corrupt database needs file replacement.
        """
        base = self._db or db_path()
        if not base.exists():
            return self.build(limit=limit)
        with tempfile.TemporaryDirectory(
            dir=base.parent, prefix=f".{base.name}.rebuild-",
        ) as directory:
            scratch = Path(directory) / _DB_NAME
            fresh = FtsIndex(mail_base=self._mail_base, db=scratch)
            out = fresh.build(limit=limit)
            try:
                live = self._open_rw()
            except sqlite3.DatabaseError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    out["skipped"] = "busy"
                    return out
                if code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    raise
                out["salvage_skipped"] = str(exc)
                with sqlite3.connect(scratch) as checkpoint:
                    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                checkpoint.close()
                os.replace(scratch, base)
                for suffix in ("-wal", "-shm"):
                    Path(f"{base}{suffix}").unlink(missing_ok=True)
                return out
            try:
                live.execute("ATTACH DATABASE ? AS rebuilt", (str(scratch),))
                if not self._begin_immediate(live):
                    out["skipped"] = "busy"
                    return out
                live.execute("SAVEPOINT salvage")
                try:
                    live.execute(
                        "INSERT INTO rebuilt.absence SELECT a.* FROM main.absence a "
                        "WHERE NOT EXISTS (SELECT 1 FROM rebuilt.docs d WHERE d.rowid=a.rowid)")
                    out["salvaged"] = self._salvage_graph_rows(live)
                except sqlite3.DatabaseError as exc:
                    if getattr(exc, "sqlite_errorcode", 0) & 0xff not in (
                        sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB,
                    ):
                        raise
                    live.execute("ROLLBACK TO salvage")
                    out["salvage_skipped"] = str(exc)
                live.execute("RELEASE salvage")
                self._carry_diagnostics(live)
                live.execute("DELETE FROM rebuilt.absence WHERE rowid NOT IN "
                             "(SELECT rowid FROM rebuilt.docs)")
                reappeared = int(live.execute(
                    "SELECT COUNT(*) FROM main.absence a JOIN rebuilt.docs d ON d.rowid=a.rowid "
                    "WHERE a.rowid NOT IN (SELECT rowid FROM rebuilt.absence)").fetchone()[0])
                if reappeared:
                    live.execute(
                        "INSERT INTO rebuilt.meta VALUES ('reappeared_total', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+excluded.value",
                        (str(reappeared),))
                    live.execute(
                        "INSERT INTO rebuilt.meta VALUES ('last_reappeared_at', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_iso_now(),))
                live.execute("DROP TABLE main.body_fts")
                live.execute("CREATE VIRTUAL TABLE main.body_fts USING fts5("
                             "body, tokenize='unicode61 remove_diacritics 2')")
                for table, columns in (
                    ("body_fts", "rowid, body"),
                    ("docs", "rowid, status, attempts, last_attempt, bytes, source"),
                    ("meta", "key, value"),
                    ("backfill_reasons", "rowid, reason"),
                    ("absence", "rowid, first_absent, last_absent, observations"),
                ):
                    live.execute(f"DELETE FROM main.{table}")
                    live.execute(
                        f"INSERT INTO main.{table} ({columns}) "
                        f"SELECT {columns} FROM rebuilt.{table}")
                live.commit()
            finally:
                live.close()
            return out

    def _salvage_graph_rows(self, conn: sqlite3.Connection) -> int:
        """Retain useful cached records when a fresh scan cannot replace them."""
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TEMP TABLE salv AS
            SELECT o.rowid AS rowid, o.attempts AS attempts,
                   o.last_attempt AS last_attempt, o.bytes AS bytes,
                   o.source AS source, o.status AS status
              FROM main.docs o
              LEFT JOIN rebuilt.docs n ON n.rowid = o.rowid
             WHERE n.rowid IS NULL
                OR (n.status != 'indexed' AND o.source IN ('graph', 'imap'))
                OR (n.status IN ('missing', 'error') AND o.status IN ('indexed', 'partial'))
                OR (n.status = 'partial' AND o.status = 'indexed')
            """)
        cur.execute("DELETE FROM rebuilt.body_fts WHERE rowid IN "
                    "(SELECT rowid FROM salv)")
        cur.execute(
            """
            INSERT INTO rebuilt.body_fts(rowid, body)
            SELECT s.rowid, ob.body
              FROM salv s JOIN main.body_fts ob ON ob.rowid = s.rowid
            """)
        cur.execute(
            """
            INSERT INTO rebuilt.docs(rowid, status, attempts, last_attempt,
                                     bytes, source)
            SELECT rowid, status, attempts, last_attempt, bytes, source
              FROM salv
             WHERE 1
            ON CONFLICT(rowid) DO UPDATE SET
                status = excluded.status, bytes = excluded.bytes,
                source = excluded.source
            """)
        cur.execute(
            """
            UPDATE rebuilt.docs SET source = (
                SELECT o.source FROM main.docs o
                 WHERE o.rowid = rebuilt.docs.rowid)
             WHERE source = 'local' AND status IN ('partial', 'missing')
               AND rowid IN (SELECT rowid FROM main.docs
                              WHERE source IN ('graph_miss', 'imap_miss',
                                               'graph_none'))
            """)
        salvaged = int(cur.execute("SELECT COUNT(*) FROM salv").fetchone()[0])
        cur.execute("DROP TABLE salv")
        return salvaged

    @staticmethod
    def _carry_diagnostics(conn: sqlite3.Connection) -> None:
        keys = ["backfill_identities", "last_backfill_attempt_at", "last_backfill_state",
                "last_backfill_error", "last_backfill_at"]
        for name in ("removed", "recovered", "reappeared", "retry_deferred"):
            keys.extend((f"{name}_total", f"last_{name}_at"))
        marks = ",".join("?" * len(keys))
        conn.execute(
            f"INSERT INTO rebuilt.meta SELECT key, value FROM main.meta WHERE key IN ({marks}) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", keys)
        conn.execute(
            "INSERT INTO rebuilt.backfill_reasons SELECT r.rowid, r.reason "
            "FROM main.backfill_reasons r JOIN rebuilt.docs d ON d.rowid=r.rowid "
            "WHERE d.status != 'indexed' "
            "ON CONFLICT(rowid) DO UPDATE SET reason=excluded.reason")

    # ------------------------------------------------------------------ #
    # internals: our db                                                  #
    # ------------------------------------------------------------------ #

    def _open_ro(self) -> sqlite3.Connection:
        uri = ("file:" + urllib.parse.quote(str(self._db or db_path()))
               + "?mode=ro")
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _open_rw(self) -> sqlite3.Connection:
        """Open (creating on first use) the index db. The ONLY fts write
        seam: the directory comes from state adoption (the one door); a
        rebuild's scratch instance uses a temporary file under that
        directory before transactional promotion."""
        path = self._db or (state.State.resolve().adopt().fts / _DB_NAME)
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None  # explicit BEGIN IMMEDIATE / COMMIT
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # v1 → v2 in place: docs gains `source` (ADD COLUMN backfills
            # 'local' onto every existing row — exactly right, they all came
            # from .emlx). CREATE IF NOT EXISTS above leaves a v1 table
            # untouched, so the column check is the actual migration gate.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
            if "source" not in cols and self._begin_immediate(conn):
                conn.execute("ALTER TABLE docs ADD COLUMN source TEXT "
                             "NOT NULL DEFAULT 'local'")
                self._meta_set(conn.cursor(), "schema_version",
                               str(SCHEMA_VERSION))
                conn.commit()
            # Not in _SCHEMA: on a v1 db the column above must land first.
            # status() counts backfilled docs by source on every search — a
            # 300k-row scan per call without this index. Busy writer: skip,
            # the next write-open creates it.
            have_idx = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                "AND name = 'docs_source'").fetchone()
            if not have_idx and self._begin_immediate(conn):
                conn.execute("CREATE INDEX IF NOT EXISTS docs_source "
                             "ON docs(source)")
                conn.commit()
            if int(self._meta_get(conn, "schema_version", "0")) < SCHEMA_VERSION:
                if self._begin_immediate(conn):
                    self._meta_set(conn.cursor(), "schema_version",
                                   str(SCHEMA_VERSION))
                    conn.commit()
            os.chmod(path, 0o600)
            return conn
        except BaseException:
            conn.close()
            raise

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

    @classmethod
    def _count_event(cls, cur: sqlite3.Cursor, name: str, count: int) -> None:
        if not count:
            return
        previous = int(cls._meta_get(cur.connection, f"{name}_total", "0"))
        cls._meta_set(cur, f"{name}_total", str(previous + count))
        cls._meta_set(cur, f"last_{name}_at", _iso_now())

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

    def _envelope_backlog(self, hwm: int) -> int:
        conn = self._envelope_conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM messages m WHERE m.ROWID > ? "
                f"{self._deleted_filter(conn)}", (hwm,),
            ).fetchone()[0])
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
                 "errors": 0, "retried": 0, "removed": 0, "deferred": 0}
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
        """Retry local files; unresolved mailbox metadata never proves deletion."""
        now = time.time()
        rows = conn.execute(
            # 'error' retries too: an extraction error can be as transient
            # as a missing file (a crawl raced Mail mid-write), but error
            # docs were never re-attempted — 8 one-shot verdicts sat
            # permanent on a live estate until RC P04 refused the index
            # (2026-08-02). Same backoff, same attempt cap.
            # Longest-waiting first, NOT ascending rowid: under a budget
            # deadline, rowid order let ~95k storeless Exchange docs
            # permanently starve any late-materializing recent message
            # (RC P04, live 2026-08-03 — a body that arrived on disk 21h
            # after its last retry was still unindexed).
            # 'partial' retries too: the crawl only advances past the
            # high-water mark, so headers-only text read from a
            # .partial.emlx stayed the doc's text forever once Mail
            # finished the download (2026-09-12 review).
            "SELECT rowid, attempts, last_attempt FROM docs "
            "WHERE status IN ('missing', 'error', 'partial') "
            "AND attempts < ? "
            "ORDER BY last_attempt, rowid",
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
            if not url:
                cur.execute("UPDATE docs SET attempts=attempts+1, last_attempt=? "
                            "WHERE rowid=?", (now, rowid))
                stats["deferred"] += 1
                continue
            outcome = self._index_one(cur, rowid, url)
            if outcome != "missing":
                stats["errors" if outcome == "error" else outcome] += 1
            stats["retried"] += 1
        self._count_event(cur, "retry_deferred", stats["deferred"])
        conn.commit()

    def _index_one(self, cur: sqlite3.Cursor, rowid: int, url: str) -> str:
        """Index a single message; returns its docs.status. Caller holds
        the write transaction.

        Local truth wins, but never regresses: a full .emlx always
        overwrites (source back to 'local'); a partial file, a vanished
        file or a parse failure never clobbers a server-sourced body —
        headers-only text replacing a real graph/imap-fetched body would
        be the index un-learning what it already knows."""
        now = time.time()
        data_dir = self._data_dir(url)
        path = find_emlx_path(data_dir, rowid) if data_dir else None
        stamps = ("graph_miss", "graph_none", "imap_miss")
        served = ("graph", "imap")
        if path is None:
            src = self._source_of(cur, rowid)
            if src in served:
                return "indexed"  # remote body still serves this rowid
            # A stamped doc keeps its stamp — but the attempt is RECORDED:
            # frozen attempts/last_attempt held stamped docs permanently
            # 'due' at the head of the retry queue, starving every
            # genuinely late-materializing body (the P04 starvation,
            # re-introduced by the stamp early-returns, 2026-08-07).
            return self._record(cur, rowid, "missing", now, 0,
                                source=src if src in stamps else "local")
        try:
            text = self._extract_text(path)
        except Exception as e:  # any parse failure — never abort a crawl
            src = self._source_of(cur, rowid)
            if src in served:
                return "indexed"
            get_logger().warning("fts: cannot index rowid %s (%s): %s",
                                 rowid, path, e)
            return self._record(cur, rowid, "error", now, 0,
                                source=src if src in stamps else "local")
        status = "partial" if path.name.endswith(".partial.emlx") else "indexed"
        src = self._source_of(cur, rowid)
        if status == "partial" and src in served:
            return "indexed"
        cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
        cur.execute("INSERT INTO body_fts(rowid, body) VALUES (?, ?)",
                    (rowid, text))
        # A still-partial file is no new evidence: the stamp stays, so a
        # retry never re-enters the doc into the backfill's candidates.
        return self._record(cur, rowid, status, now,
                            len(text.encode("utf-8", "replace")),
                            source=src if status == "partial"
                            and src in stamps else "local")

    @staticmethod
    def _source_of(cur: sqlite3.Cursor, rowid: int) -> str | None:
        row = cur.execute("SELECT source FROM docs WHERE rowid = ?",
                          (rowid,)).fetchone()
        return str(row["source"]) if row else None

    @staticmethod
    def _record(cur: sqlite3.Cursor, rowid: int, status: str,
                now: float, nbytes: int, source: str = "local") -> str:
        if status in ("missing", "error"):
            cur.execute("DELETE FROM body_fts WHERE rowid = ?", (rowid,))
        cur.execute("DELETE FROM absence WHERE rowid=?", (rowid,))
        FtsIndex._count_event(cur, "reappeared", cur.rowcount)
        if status == "indexed":
            cur.execute("DELETE FROM backfill_reasons WHERE rowid=?", (rowid,))
        cur.execute(
            """
            INSERT INTO docs(rowid, status, attempts, last_attempt, bytes,
                             source)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(rowid) DO UPDATE SET
                status = excluded.status,
                attempts = docs.attempts + 1,
                last_attempt = excluded.last_attempt,
                bytes = excluded.bytes,
                source = excluded.source
            """,
            (rowid, status, now, nbytes, source),
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

    # ------------------------------------------------------------------ #
    # internals: backfill (server-side bodies for local holes)           #
    # ------------------------------------------------------------------ #

    def _graph_identities(self) -> list:
        """Graph-enabled identities, default first — the mailboxes a
        backfill may ask. Unreadable identities read as none: backfill
        silently skips rather than crashing the nightly sync."""
        from . import identities as ident_mod

        try:
            idents, default = ident_mod.load()
        except Exception as exc:
            if not str(exc).startswith("no sending identity configured"):
                self._backfill_config_error = f"backfill identity configuration: {exc}"
            return []
        names = sorted(idents, key=lambda n: (n != default, n))
        return [idents[n] for n in names
                if getattr(idents[n], "executor", "launchd") == "graph"
                or getattr(idents[n], "drafts", "none") == "graph"]

    def _imap_identities(self) -> list:
        """Identities with an [name.imap] table, default first — the
        IMAP lane's mailboxes. Same soft contract as
        _graph_identities."""
        from . import identities as ident_mod

        try:
            idents, default = ident_mod.load()
        except Exception as exc:
            if not str(exc).startswith("no sending identity configured"):
                self._backfill_config_error = f"backfill identity configuration: {exc}"
            return []
        names = sorted(idents, key=lambda n: (n != default, n))
        return [idents[n] for n in names
                if dict(getattr(idents[n], "imap", {}) or {})]

    def _fetch_remote_body(self, ident, message_id: str) -> dict | None:
        """Network seam (partial class): tests monkeypatch this symbol."""
        from . import graph

        return graph.fetch_body_by_message_id(ident, message_id)

    def _fetch_imap_body(self, ident, message_id: str) -> dict | None:
        """Network seam (imap lane): tests monkeypatch this symbol."""
        from . import imap

        return imap.fetch_body_by_message_id(ident, message_id)

    def _translate_ews_ids(self, ident, ews_ids: list[str]) -> dict[str, str]:
        """Network seam (storeless class): bulk id translation."""
        from . import graph

        return graph.translate_ews_ids(ident, ews_ids)

    def _fetch_remote_body_by_id(self, ident, rest_id: str) -> dict | None:
        """Network seam (storeless class): body by Graph REST id."""
        from . import graph

        return graph.fetch_body_by_graph_id(ident, rest_id)

    def _envelope_backfill_meta(
        self, rowids: list[int],
    ) -> dict[int, tuple[str, str | None]]:
        """{rowid: (mailbox url, EWS remote_id)} — remote_id is None when
        the Envelope Index generation has no such column."""
        out: dict[int, tuple[str, str | None]] = {}
        if not rowids:
            return out
        conn = self._envelope_conn()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            rid_col = "m.remote_id" if "remote_id" in cols else "NULL"
            for i in range(0, len(rowids), 500):
                chunk = rowids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT m.ROWID AS rowid, mb.url AS url,
                           {rid_col} AS remote_id
                      FROM messages m
                      JOIN mailboxes mb ON mb.ROWID = m.mailbox
                     WHERE m.ROWID IN ({marks})
                       {self._deleted_filter(conn)}
                    """,
                    chunk,
                ).fetchall()
                for r in rows:
                    remote = r["remote_id"]
                    out[int(r["rowid"])] = (
                        r["url"] or "",
                        str(remote) if remote else None,
                    )
        finally:
            conn.close()
        return out

    def _message_id_from_partial(self, rowid: int, url: str) -> str | None:
        """RFC Message-ID read from the partial file's own headers — the
        only place it exists locally (the Envelope Index keeps a hash).
        None means the file parses and simply HAS no Message-ID header
        (a doc that can never be asked); read and parse failures RAISE —
        an unreadable file is absence of evidence, and the caller defers
        the doc instead of stamping it."""
        import email as email_mod

        from .sources.apple_mail import _read_emlx_bytes

        data_dir = self._data_dir(url)
        path = find_emlx_path(data_dir, rowid) if data_dir else None
        if path is None:
            raise FileNotFoundError(f"no partial file for rowid {rowid}")
        raw = _read_emlx_bytes(path)
        msg = email_mod.message_from_bytes(raw)
        # str() first: an unencoded 8-bit value arrives as
        # email.header.Header, not str. Then collapse ALL whitespace:
        # folding can leave '\r\n ' inside the angle-addr, Graph rejects
        # a $filter containing it, and no real Message-ID holds spaces.
        mid = re.sub(r"\s+", "", str(msg.get("Message-ID") or ""))
        return mid or None

    @staticmethod
    def _remote_text(body: dict) -> str:
        """Graph body → index text, same shape as the emlx path: HTML is
        stripped by the ONE stripper, and the same doc cap applies."""
        content = str(body.get("content") or "")
        if str(body.get("contentType") or "").lower() == "html":
            from .sources.apple_mail import _html_to_text

            content = _html_to_text(content)
        cap = config.fts_doc_cap_bytes()
        if cap and len(content) > cap:
            content = content[:cap] + "\n[…body truncated…]"
        return content

# ---------------------------------------------------------------------- #
# launchd install and CLI compatibility facade                           #
# ---------------------------------------------------------------------- #


def _plist_path() -> Path:
    return fts_runtime.plist_path()


def _log_path() -> Path:
    return fts_runtime.log_path()


def _plist_content() -> str:
    return fts_runtime.plist_content()


def install_launchd() -> str:
    return fts_runtime.install_launchd()


def uninstall_launchd() -> str:
    return fts_runtime.uninstall_launchd()


def _reconcile_due(idx: FtsIndex) -> bool:
    return fts_runtime.reconcile_due(idx)


_BACKFILL_CAP = fts_runtime.BACKFILL_CAP


def _sync(idx: FtsIndex, limit: int | None) -> dict:
    return fts_runtime.sync(idx, limit)


def _print_status(st: dict, as_json: bool) -> None:
    fts_runtime.print_status(st, as_json)


def main(argv: list[str] | None = None) -> int:
    return fts_runtime.main(FtsIndex, sqlite3.Error, argv)


if __name__ == "__main__":
    raise SystemExit(main())
