"""One human-facing installation and recovery overview.

The detailed doctor and dispatcher reports remain the machine-readable
sources of truth.  This module groups them into the questions a user asks:
can I read mail, can I send, will scheduled mail run, and what should I do
next?  ``build`` and ``render`` are pure so the text and JSON views cannot
disagree about readiness.
"""
from __future__ import annotations

from . import __version__, checks, dispatcher, doctor


_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Mail access", (
        ("mail_store", "Read mail"),
        ("automation", "Mail automation"),
        ("accessibility", "Mailbox deletion fallback"),
    )),
    ("Sending", (
        ("identities", "Sending identity"),
        ("transports", "Sending connection"),
        ("graph", "Exchange extras"),
    )),
    ("Search and history", (
        ("fts", "Body search"),
        ("audit", "Activity history"),
    )),
)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _next_pending(pending: list[dict]) -> dict | None:
    usable = [item for item in pending if item.get("send_at")]
    return min(usable, key=lambda item: str(item["send_at"])) \
        if usable else None


def build(report: dict, findings: list[checks.Finding],
          scheduling: dict) -> dict:
    """Combine diagnostic sources into one stable status snapshot."""
    all_checks = dict(report.get("checks") or {})
    audit_check = report.get("audit")
    if isinstance(audit_check, dict):
        all_checks["audit"] = audit_check

    finding_rows = [
        {"check": finding.check, "detail": finding.detail,
         "repairable": checks.repairable(finding)}
        for finding in findings
    ]
    counts = dict(scheduling.get("counts") or {})
    pending = list(scheduling.get("pending") or [])
    sending = list(scheduling.get("sending") or [])
    failed = list(scheduling.get("failed") or [])
    failed_count = int(counts.get("failed", len(failed)) or 0)

    ready = (bool(report.get("ok")) and not finding_rows
             and bool(scheduling.get("ok")) and failed_count == 0)

    next_steps: list[str] = []
    if any(row["repairable"] for row in finding_rows):
        next_steps.append("email-mcp doctor --fix")
    for check in all_checks.values():
        if (not check.get("ok") and not check.get("advisory")
                and check.get("fix")):
            next_steps.append(str(check["fix"]))
    if not scheduling.get("ok"):
        next_steps.append("email-mcp dispatcher --status")
    if failed_count:
        next_steps += [
            "email-mcp dispatcher --status",
            ("Fix the reported cause, then reschedule the message from your "
             "MCP client; keep the failed record until the replacement is "
             "confirmed."),
        ]
    if not ready and not next_steps:
        next_steps.append("email-mcp doctor")

    return {
        "version": __version__,
        "ready": ready,
        "mode": "read-only" if report.get("read_only") else "read and write",
        "findings": finding_rows,
        "checks": all_checks,
        "scheduling": {
            "ok": bool(scheduling.get("ok")),
            "background_sender_installed": bool(
                scheduling.get("launchd_installed")),
            "counts": counts,
            "pending": pending,
            "sending": sending,
            "failed": failed,
            **({"error": str(scheduling["error"])}
               if scheduling.get("error") else {}),
        },
        "next_scheduled": _next_pending(pending),
        "next_steps": _unique(next_steps),
    }


def snapshot() -> dict:
    """Run all read-only probes; never fail instead of explaining status."""
    findings = checks.findings()
    report = doctor.run()
    try:
        scheduling = dispatcher.status()
    except Exception as exc:  # status is a recovery surface, never a traceback
        scheduling = {
            "ok": False,
            "counts": {},
            "pending": [],
            "sending": [],
            "failed": [],
            "error": f"scheduled-mail status unavailable: {exc}",
        }
    return build(report, findings, scheduling)


def _tag(check: dict) -> str:
    if check.get("ok"):
        return "OK"
    return "OPTIONAL" if check.get("advisory") else "FIX"


def render(status: dict) -> list[str]:
    """Render the concise terminal view from the same snapshot JSON uses."""
    lines = [
        f"Email MCP {status['version']}",
        f"Status: {'READY' if status['ready'] else 'NEEDS ATTENTION'}",
        f"Mode: {status['mode']}",
    ]

    findings = status.get("findings") or []
    if findings:
        lines += ["", "Installation"]
        for finding in findings:
            lines.append(
                f"  [FIX] {finding['check']}: {finding['detail']}")

    check_map = status.get("checks") or {}
    for heading, members in _GROUPS:
        present = [(key, label) for key, label in members if key in check_map]
        if not present:
            continue
        lines += ["", heading]
        for key, label in present:
            check = check_map[key]
            lines.append(f"  [{_tag(check)}] {label}: {check['detail']}")

    schedule = status.get("scheduling") or {}
    lines += ["", "Scheduled mail"]
    dispatcher_check = check_map.get("dispatcher")
    if dispatcher_check:
        lines.append(
            f"  [{_tag(dispatcher_check)}] Background delivery: "
            f"{dispatcher_check['detail']}")
    elif schedule.get("error"):
        lines.append(f"  [FIX] Background delivery: {schedule['error']}")
    counts = schedule.get("counts") or {}
    lines.append(
        "  Queue: "
        f"{counts.get('pending', 0)} pending, "
        f"{counts.get('sending', 0)} sending, "
        f"{counts.get('failed', 0)} failed"
    )
    next_item = status.get("next_scheduled")
    if next_item:
        lines.append(
            f"  Next: {next_item['send_at']} — "
            f"{next_item.get('subject') or '(no subject)'} "
            f"[{next_item['id']}]"
        )
    failed = schedule.get("failed") or []
    if failed:
        lines.append("  Failed messages:")
        for item in failed:
            recipients = ", ".join(item.get("to") or []) or "unknown recipient"
            lines.append(
                f"    - {item.get('subject') or '(no subject)'} "
                f"[{item.get('id', 'unknown id')}] to {recipients}"
            )
            lines.append(
                f"      {item.get('last_error') or 'No error recorded'} "
                f"({item.get('attempts', 0)} attempt(s))"
            )

    steps = status.get("next_steps") or []
    lines += ["", "Next steps"]
    if steps:
        lines += [f"  {number}. {step}"
                  for number, step in enumerate(steps, start=1)]
    else:
        lines.append("  No action needed — ready to use.")
    return lines
