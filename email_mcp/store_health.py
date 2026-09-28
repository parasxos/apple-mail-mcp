"""Observed Mail-store access, independent of delivery and TCC internals."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import sqlite3
import stat
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, health_history

FDA_PANE = ("x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
            "?Privacy_AllFiles")
PROBE_TIMEOUT = 0.2
CACHE_SECONDS = 5.0
_STORE_TOOLS = frozenset({
    "search_emails", "get_email", "get_emails_batch", "get_thread",
    "list_mailboxes", "list_recent", "get_attachment", "reply_email",
    "triage_plan", "triage_plan_delete", "triage_apply",
})
_active: StoreHealth | _UnavailableHealth | None = None


def host_candidate() -> dict:
    executable = str(Path(sys.executable).resolve())
    bundle = next((p for p in Path(executable).parents if p.suffix == ".app"), None)
    candidate = (os.environ.get("__CFBundleIdentifier")
                 or os.environ.get("TERM_PROGRAM")
                 or (str(bundle) if bundle else executable))[:512]
    result = {"candidate": candidate, "executable": executable,
              "attribution": "unverified"}
    if bundle and candidate == str(bundle):
        result["reveal_command"] = "open -R " + shlex.quote(str(bundle))
    return result


def fda_fix(host: dict | None = None) -> str:
    candidate = (host or host_candidate())["candidate"]
    return (
        f"Check Full Disk Access for the hosting app (candidate: {candidate}; "
        "TCC attribution is unverified): System Settings → Privacy & Security "
        f"→ Full Disk Access. Open {FDA_PANE}. If you just enabled access, "
        "restart the hosting app and reconnect, then retry. Grant status is unknown."
    )


def probe_store() -> dict:
    """Read at most one index row through a new WAL-aware connection."""
    try:
        base = config.mail_dir()
        base.stat()
        index = base / "MailData" / "Envelope Index"
        try:
            fd = os.open(index, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("Envelope Index is not a regular file")
                os.read(fd, 16)
            finally:
                os.close(fd)
        except FileNotFoundError:
            return {"ok": False, "reason": "index_missing",
                    "detail": f"Envelope Index not found at {index}.",
                    "fix": "Check EMAIL_MCP_MAIL_DIR and finish Apple Mail setup."}
        uri = "file:" + urllib.parse.quote(str(index)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=PROBE_TIMEOUT)
        try:
            deadline = time.monotonic() + PROBE_TIMEOUT
            conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        finally:
            conn.close()
        return {"ok": True, "reason": "readable",
                "detail": "Envelope Index is readable (bounded read probe)."}
    except PermissionError as exc:
        return {"ok": False, "reason": "permission_denied",
                "detail": str(exc), "fix": fda_fix()}
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "reason": "store_missing", "detail": str(exc),
                "fix": "Check EMAIL_MCP_MAIL_DIR and finish Apple Mail setup."}
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        reason = {
            sqlite3.SQLITE_BUSY: "busy", sqlite3.SQLITE_LOCKED: "busy",
            sqlite3.SQLITE_INTERRUPT: "probe_timeout",
            sqlite3.SQLITE_CORRUPT: "invalid_database",
            sqlite3.SQLITE_NOTADB: "invalid_database",
            sqlite3.SQLITE_CANTOPEN: "open_failed",
            sqlite3.SQLITE_ERROR: "schema_unavailable",
        }.get(code, "io_error")
        fix = ("Retry the store check after Apple Mail finishes its database work."
               if reason in {"busy", "probe_timeout"}
               else "Check the Mail-store path and Apple Mail database health.")
        if reason == "open_failed":
            fix += " If access is denied, " + fda_fix()
        return {"ok": False, "reason": reason, "detail": str(exc), "fix": fix}
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": "io_error", "detail": str(exc),
                "fix": "Check the configured Mail-store path and filesystem access."}


class StoreHealth:
    def __init__(self, *, probe=None, monotonic=None, ttl=CACHE_SECONDS,
                 persist=True):
        self._probe = probe or probe_store
        self._clock = monotonic or time.monotonic
        self._ttl = ttl
        self._persist = persist
        self._lock = threading.RLock()
        self._expires = 0.0
        self._generation = 0
        self._snapshot: dict = {}
        self._transitions: list[dict] = []
        self._recent_hosts: list[dict] = []
        self._host = host_candidate()
        identity = [self._host, config.source_name(),
                    os.environ.get("EMAIL_MCP_MAIL_DIR", str(Path.home() / "Library/Mail"))]
        self._key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def refresh(self, *, force=False, after: int | None = None) -> dict:
        with self._lock:
            fresh = self._snapshot and self._clock() < self._expires
            if fresh and (not force or (after is not None and self._generation > after)):
                return copy.deepcopy(self._snapshot)
            try:
                result = self._probe()
            except Exception:
                result = {"ok": False, "reason": "probe_failed",
                          "detail": "The store-access probe could not complete.",
                          "fix": "Run doctor to retry the store-access check."}
            now = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "status": "readable" if result["ok"] else "unavailable",
                "reason": result["reason"], "checked_at": now,
                "last_readable_at": self._snapshot.get("last_readable_at"),
                "last_denied_at": self._snapshot.get("last_denied_at"),
                "host": self._host, "history_available": False,
                "grant_status": "unknown",
            }
            if result["ok"]:
                snapshot["last_readable_at"] = now
            elif result["reason"] == "permission_denied":
                snapshot["last_denied_at"] = now
            if not result["ok"]:
                snapshot["fix"] = result["fix"]
            if self._persist:
                previous, available, recent = health_history.observe(self._key, snapshot)
            else:
                previous, available, recent = health_history.read(self._key)
            snapshot["history_available"] = available
            self._recent_hosts = recent
            if available:
                self._transitions = previous
                for item in reversed(previous):
                    if item["status"] == "readable" and snapshot["last_readable_at"] is None:
                        snapshot["last_readable_at"] = item["checked_at"]
                    if item["reason"] == "permission_denied" and snapshot["last_denied_at"] is None:
                        snapshot["last_denied_at"] = item["checked_at"]
            transition = {name: snapshot[name] for name in (
                "status", "reason", "checked_at", "host")}
            transition["first_observed_at"] = now
            if not self._transitions or any(self._transitions[-1][name] != snapshot[name]
                                           for name in ("status", "reason")):
                self._transitions = [*self._transitions, transition][-12:]
            else:
                transition["first_observed_at"] = self._transitions[-1]["first_observed_at"]
                self._transitions[-1] = transition
            self._snapshot = snapshot
            self._detail = result["detail"]
            self._generation += 1
            self._expires = self._clock() + self._ttl
            return copy.deepcopy(snapshot)

    def doctor(self) -> dict:
        with self._lock:
            snapshot = self.refresh(force=True)
            return {"ok": snapshot["status"] == "readable", "detail": self._detail,
                    **snapshot, "transitions": copy.deepcopy(self._transitions),
                    "recent_hosts": copy.deepcopy(self._recent_hosts)}

    def decorate(self, result: dict, tool: str, generation: int) -> dict:
        with self._lock:
            relevant = tool.removeprefix("tool_") in _STORE_TOOLS
            force = relevant and (result.get("code") == "mail_unavailable"
                                 or self._snapshot.get("status") == "unavailable")
            snapshot = self.refresh(force=force, after=generation)
        result["health"] = {"mail_store": snapshot}
        if snapshot["status"] != "readable":
            result["degraded"] = ["no-store-access"]
        return result


def unavailable_snapshot() -> dict:
    return {
        "status": "unavailable", "reason": "probe_failed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "last_readable_at": None, "last_denied_at": None,
        "host": {"candidate": "unknown", "executable": sys.executable,
                 "attribution": "unverified"},
        "history_available": False, "grant_status": "unknown",
        "fix": "Restart the MCP server, then run doctor to retry the store-access check.",
    }


def unavailable_result(result: dict) -> dict:
    return {**result, "health": {"mail_store": unavailable_snapshot()},
            "degraded": ["no-store-access"]}


class _UnavailableHealth:
    generation = 0

    def decorate(self, result, tool, generation):
        return unavailable_result(result)

    def doctor(self):
        return {"ok": False, "detail": "Store-health monitoring is unavailable.",
                **unavailable_snapshot(), "transitions": [], "recent_hosts": []}


def start() -> StoreHealth | _UnavailableHealth:
    global _active
    try:
        _active = StoreHealth()
    except Exception:
        _active = _UnavailableHealth()
    try:
        _active.refresh()
    except Exception:
        pass
    return _active


def check() -> dict:
    try:
        return (_active or StoreHealth(persist=False)).doctor()
    except Exception:
        return _UnavailableHealth().doctor()
