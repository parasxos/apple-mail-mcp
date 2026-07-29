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

Entries whose manifest says executor="graph" belong to Exchange (deferred
send armed at schedule time) and never enter the local claim/deliver path;
each pass reconciles their outcome instead — see _reconcile_graph.

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

from . import config, identities, sender, spool
from .log import get_logger

_log = get_logger()

LAUNCHD_LABEL = "com.email-mcp.dispatcher"
# Labels this agent shipped under before v0.8. install/uninstall boot these
# out and remove their plists — migrate, never strand a second dispatcher
# ticking over the same spool.
LEGACY_LABELS = ("com.paris.email-mcp-dispatcher",)
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


def _fail_or_retry(entry: spool.Entry, error: str, now: datetime,
                   src: str = "sending") -> str:
    """Consume an attempt: park in failed/ or back off in pending/.
    `src` is where the entry currently lives — "sending" for claimed local
    deliveries, "pending" for graph entries (which are never claimed)."""
    entry.attempts += 1
    entry.last_error = error
    if entry.attempts >= config.send_max_retries():
        spool.move(entry.id, src, "failed", entry)
        _notify("email-mcp: send FAILED",
                f"{entry.subject!r} to {', '.join(entry.to)} — {error[:120]}")
        return "failed"
    delay = BACKOFF_MINUTES[min(entry.attempts - 1, len(BACKOFF_MINUTES) - 1)]
    entry.next_attempt_at = spool.iso(now + timedelta(minutes=delay))
    spool.move(entry.id, src, "pending", entry)
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


# Exchange owns a graph entry until send_at + this grace: normal deferred
# transmission needs no help from us, so the reconcile pass only starts
# asking questions once Exchange is convincingly late.
GRAPH_GRACE_MINUTES = 10


def _graph_mark_sent(entry: spool.Entry, now: datetime) -> str:
    entry.delivered_at = spool.iso(now)
    entry.next_attempt_at = None
    entry.last_error = None
    spool.move(entry.id, "pending", "sent", entry)
    return "sent (delivered by Exchange)"


def _graph_leave(entry: spool.Entry, error: str, note: str) -> str:
    """F4/F6: no evidence either way — the entry stays on graph, untouched
    except for last_error, and the next pass retries."""
    entry.last_error = error
    spool.update("pending", entry)
    return note


def _graph_apply_status(entry: spool.Entry, status: str, now: datetime) -> str:
    """Fold a draft_status verdict into the spool. NEVER local-delivers:
    'sent' and 'cancelled_externally' are terminal moves, anything
    ambiguous leaves the entry for the next pass (F3/F9)."""
    if status == "sent":
        return _graph_mark_sent(entry, now)
    if status == "cancelled_externally":
        entry.next_attempt_at = None
        entry.last_error = (
            "deferred draft was discarded outside the spool (e.g. in "
            "Outlook/OWA Drafts) — not sent, not sendable locally")
        spool.move(entry.id, "pending", "cancelled", entry)
        return "cancelled externally (draft discarded in Outlook/OWA)"
    # 'held' (shouldn't recur here) / 'unknown': leave + retry next pass.
    return f"graph: status {status} — left for next pass"


def _reconcile_graph(now: datetime) -> dict[str, str]:
    """One reconcile pass over pending graph-executor entries.

    Exchange is the executor of record for these — they NEVER enter the
    local claim/deliver path. Past send_at + GRAPH_GRACE_MINUTES we ask
    what happened: confirmed sent → sent/, confirmed external cancel →
    cancelled/, draft still held → revoke it (delete FIRST) and only on a
    CONFIRMED delete flip to launchd for local delivery next pass. Every
    ambiguous answer leaves the entry on graph for the next pass — the
    double-send fence is "never deliver locally while Exchange may still
    hold (or have sent) the message"."""
    entries = [e for e in spool.entries("pending") if e.executor == "graph"]
    if not entries:
        return {}
    from . import graph  # lazy: spools without graph entries never import it

    results: dict[str, str] = {}
    grace = timedelta(minutes=GRAPH_GRACE_MINUTES)
    for entry in entries:
        try:
            send_at = datetime.fromisoformat(entry.send_at)
        except ValueError:
            send_at = now  # malformed stamp: reconcile now rather than never
        if now < send_at + grace:
            continue  # Exchange's window — nothing to reconcile yet
        if (entry.next_attempt_at
                and datetime.fromisoformat(entry.next_attempt_at) > now):
            continue  # backing off after _fail_or_retry (e.g. F12)

        try:
            ident = identities.get(entry.identity)
        except identities.IdentityError as e:
            # F12: identity removed/renamed since scheduling — retry with
            # backoff (time to fix the file); the pass continues.
            results[entry.id] = _fail_or_retry(entry, str(e), now,
                                               src="pending")
            continue

        if not entry.graph_draft_id:
            # Crash window recovery (F1/F2): the manifest was written but
            # no draft id recorded. The frozen Message-ID is the key.
            try:
                draft_id = graph.find_draft_by_message_id(
                    ident, entry.message_id)
            except graph.GraphError as e:
                results[entry.id] = _graph_leave(
                    entry, str(e), "graph: drafts lookup failed — retrying")
                continue
            if draft_id is not None:
                # F2: the draft exists — adopt it; Exchange keeps the job.
                entry.graph_draft_id = draft_id
                entry.last_error = None
                spool.update("pending", entry)
                _log.info("graph: adopted orphan draft %s for %s",
                          draft_id, entry.id)
                results[entry.id] = "graph: adopted existing draft"
                continue
            try:
                sent = graph.sent_by_message_id(ident, entry.message_id)
            except graph.GraphError as e:
                results[entry.id] = _graph_leave(
                    entry, str(e), "graph: sent-items lookup failed — retrying")
                continue
            if sent:
                results[entry.id] = _graph_mark_sent(entry, now)
                continue
            # F1: no draft was ever armed — the local spool takes over.
            entry.executor = "launchd"
            entry.next_attempt_at = spool.iso(now)
            entry.last_error = None
            spool.update("pending", entry)
            _log.warning("graph: no draft found for %s — flipped to "
                         "launchd for local delivery", entry.id)
            results[entry.id] = "graph: no draft found — local delivery next pass"
            continue

        try:
            status = graph.draft_status(
                ident, entry.graph_draft_id, entry.message_id)
        except graph.GraphError as e:
            results[entry.id] = _graph_leave(
                entry, str(e), "graph: unreachable — retrying next pass")
            continue

        if status != "held":
            results[entry.id] = _graph_apply_status(entry, status, now)
            continue

        # Held past grace (F3): Exchange is late. Revoke the draft FIRST;
        # only a CONFIRMED delete releases the message to local delivery.
        try:
            outcome = graph.delete_draft(ident, entry.graph_draft_id)
        except graph.GraphError as e:
            # F4: delete failed/timed out — entry stays on graph, untouched.
            results[entry.id] = _graph_leave(
                entry, str(e), "graph: draft revoke failed — retrying")
            continue
        if outcome == "deleted":
            entry.executor = "launchd"
            entry.graph_draft_id = None
            entry.next_attempt_at = spool.iso(now)
            entry.last_error = None
            spool.update("pending", entry)
            _log.warning("graph: revoked late draft for %s — flipped to "
                         "launchd for local delivery", entry.id)
            results[entry.id] = ("graph: draft revoked — local delivery "
                                 "next pass")
        else:
            # 'gone': Exchange may have won the race between our status
            # probe and the delete — re-disambiguate, never guess (F3).
            try:
                status = graph.draft_status(
                    ident, entry.graph_draft_id, entry.message_id)
            except graph.GraphError as e:
                results[entry.id] = _graph_leave(
                    entry, str(e),
                    "graph: draft gone, outcome ambiguous — retrying")
                continue
            results[entry.id] = _graph_apply_status(entry, status, now)
    return results


def run_once(now: datetime | None = None) -> dict:
    now = now or spool.utcnow()
    _recover_stranded(now)
    # Partition: graph-executor entries NEVER enter the local claim/deliver
    # path — Exchange holds the armed draft. The due snapshot is taken
    # BEFORE reconciling, so an entry flipped back to launchd delivers on
    # the NEXT pass, after the flip is durably on disk.
    due = [e for e in spool.entries("pending")
           if e.executor != "graph" and _due(e, now)]
    results: dict[str, str] = _reconcile_graph(now)
    if not due:
        return {"checked_at": spool.iso(now), "due": 0, "results": results}

    # Per-run memo: ensure each identity's transport once, however many
    # messages ride it. An unknown identity at fire time is retried with
    # backoff — time for the user to fix the identities file.
    ready: dict[str, tuple[bool, str | None]] = {}

    def _transport_ready(name: str) -> tuple[bool, str | None]:
        if name not in ready:
            try:
                ident = identities.get(name)
            except identities.IdentityError as e:
                ready[name] = (False, str(e))
            else:
                ok, _ = sender.preflight(ident)
                ready[name] = (
                    ok, None if ok else sender._transport_unavailable(ident))
        return ready[name]

    for entry in due:
        if not spool.claim(entry.id):
            results[entry.id] = "claimed elsewhere"
            continue
        entry.status = "sending"

        transport_ok, transport_err = _transport_ready(entry.identity)
        if not transport_ok:
            results[entry.id] = _fail_or_retry(
                entry, transport_err or "transport unavailable", now)
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
            sender.deliver_for(
                identities.get(entry.identity), raw,
                rcpt_to=entry.to + entry.cc + entry.bcc,
            )
        except sender.SendError as e:
            results[entry.id] = _fail_or_retry(entry, str(e), now)
            continue
        entry.delivered_at = spool.iso(spool.utcnow())
        entry.next_attempt_at = None
        entry.last_error = None  # succeeded — a stale error reads as failure
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


def _remove_legacy_plists() -> list[str]:
    """Boot out and delete every LEGACY_LABELS agent. Returns the labels
    actually found (loaded or on disk); best-effort, never raises."""
    uid = os.getuid()
    removed: list[str] = []
    for label in LEGACY_LABELS:
        plist = _plist_path().parent / f"{label}.plist"
        # Service-target form works whether or not the plist still exists.
        proc = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                              capture_output=True)
        found = proc.returncode == 0 or plist.exists()
        plist.unlink(missing_ok=True)
        if found:
            removed.append(label)
    return removed


def install_launchd() -> str:
    migrated = _remove_legacy_plists()
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
    note = f" (migrated legacy: {', '.join(migrated)})" if migrated else ""
    return f"installed {LAUNCHD_LABEL} (every 60s){note}, log: {_log_path()}"


def uninstall_launchd() -> str:
    removed_legacy = _remove_legacy_plists()
    plist = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                   capture_output=True)
    if plist.exists():
        plist.unlink()
    note = f" (+ legacy: {', '.join(removed_legacy)})" if removed_legacy else ""
    return f"removed {LAUNCHD_LABEL}{note}"


def status() -> dict:
    return {
        "spool": str(config.spool_dir()),
        "launchd_plist": str(_plist_path()),
        "launchd_installed": _plist_path().exists(),
        "counts": {s: len(spool.entries(s)) for s in spool.STATES},
        "pending": [
            {"id": e.id, "send_at": e.send_at, "to": e.to,
             "subject": e.subject, "attempts": e.attempts,
             "next_attempt_at": e.next_attempt_at, "last_error": e.last_error,
             "executor": e.executor}
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
    # Quiet when idle: only log passes that actually did something
    # (local deliveries due, or graph reconcile activity in results).
    if summary["due"] or summary["results"]:
        json.dump(summary, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
