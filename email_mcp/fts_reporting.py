"""Pure explanations of recorded index coverage and recovery evidence."""
from __future__ import annotations


def coverage_report(status: dict) -> dict:
    docs = status.get("docs", {})
    recovery = status.get("recovery", {})
    backlog = status.get("backlog")
    gaps = sum(docs.get(key, 0) for key in ("missing", "partial", "error"))
    remedies = []

    def remedy(cause: str, action: str) -> None:
        remedies.append({"cause": cause, "action": action})

    state = status.get("state", "absent")
    if state != "ready":
        coverage = "unavailable"
        note = "Body coverage is unavailable; envelope matches can still be returned."
        if state == "absent":
            remedy("index_absent", "python -m email_mcp.fts --build")
        elif state == "error":
            remedy("index_error", "python -m email_mcp.fts --rebuild")
        elif state == "disabled":
            remedy("index_disabled", "Enable EMAIL_MCP_FTS_ENABLED to search indexed bodies.")
    else:
        coverage = ("incomplete" if gaps or backlog else
                    "unknown" if backlog is None else "no_known_gaps")
        note = (
            "Counts cover all accounts, regardless of the search filters. "
            "Missing, partial, failed or unscanned documents can omit body matches. "
            "Partial documents may already have searchable text. "
            "Document and result limits still apply."
        )
        if backlog:
            remedy("unscanned", "python -m email_mcp.fts --sync")
        if backlog is None:
            remedy("backlog_unknown", "Restore local Mail index access to measure unscanned messages.")
        if docs.get("error"):
            remedy("extraction_error", "Check local message file readability, then run python -m email_mcp.fts --sync.")
        if recovery.get("last_error"):
            remedy("provider_error", "Inspect the named provider's authentication and configuration, then run python -m email_mcp.fts --backfill.")
        if recovery.get("state") == "no_identities":
            remedy("no_identities", "Configure a supported Graph or IMAP backfill identity, or download the bodies in Mail.")
        if recovery.get("no_lane"):
            remedy("no_lane", "Configure a supported backfill provider for the affected accounts, or download their bodies in Mail.")
        if recovery.get("unavailable_mailbox"):
            remedy("mailbox_metadata", "Mailbox metadata was unavailable at the last lookup. Let Mail finish syncing, then retry backfill.")
        if recovery.get("no_message_id") or recovery.get("no_remote_id"):
            remedy("missing_identifier", "The recorded provider lookup lacks a message identifier; download the affected bodies in Mail.")
        if recovery.get("unclassified_unavailable"):
            remedy("unavailable", "Earlier recovery could not find an applicable provider or identifier; check backfill configuration and local downloads.")
        if recovery.get("confirmed_misses"):
            remedy("confirmed_miss", "The providers asked did not hold these messages; check account coverage or restore a local copy.")
        if docs.get("missing") or docs.get("partial"):
            remedy("body_gaps", "For eligible configured accounts run python -m email_mcp.fts --backfill; otherwise download the bodies in Mail. Recovery is not guaranteed.")
        if docs.get("local_retry_exhausted"):
            remedy("local_retry_exhausted", "Local retries have stopped for these documents. After restoring local bodies, run python -m email_mcp.fts --rebuild to retry them.")

    return {
        "coverage": {"scope": "all_accounts", "state": coverage, "note": note},
        "remedies": remedies,
    }
