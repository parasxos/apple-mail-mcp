"""Scheduled-send process facade over composed application use cases."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import audit, dispatcher_runtime
from .application.background import (
    BACKOFF_MINUTES,
    GRAPH_GRACE_MINUTES,
    STALE_SENDING_MINUTES,
    SUPERSEDED,
    is_due,
    parse_timestamp,
)

LAUNCHD_LABEL = dispatcher_runtime.LAUNCHD_LABEL
LEGACY_LABELS = dispatcher_runtime.LEGACY_LABELS
_SUPERSEDED = SUPERSEDED
_parse_iso = parse_timestamp
_due = is_due


def _application():
    """Compose a fresh worker so runtime config and test seams are observed."""
    from .bootstrap import build_application

    return build_application()


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
    return _application().dispatch_scheduled(now).to_wire()


def _plist_path() -> Path:
    return dispatcher_runtime.plist_path()


def _log_path() -> Path:
    return dispatcher_runtime.log_path()


def _plist_content() -> str:
    return dispatcher_runtime.plist_content()


def _remove_legacy_plists() -> list[str]:
    return dispatcher_runtime._remove_legacy_plists()


def install_launchd() -> str:
    return dispatcher_runtime.install_launchd()


def uninstall_launchd() -> str:
    return dispatcher_runtime.uninstall_launchd()


def status() -> dict:
    return dispatcher_runtime.status()


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
