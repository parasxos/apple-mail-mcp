"""Runtime configuration via environment variables. All optional.

The five state-tree getters (spool_dir/plans_dir/graph_dir/fts_dir/
audit_dir) are pure path questions delegating to email_mcp.state — the
tree itself is created only through state adoption (the one effectful
door). They raise state.RefusedError with the single policy reason when
resolution refuses (retired vars, home-directory roots, ...).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import state


def _state() -> state.StateReader:
    return state.State.resolve().reader()


def mail_dir() -> Path:
    """Return the active Mail.app data directory.

    Honors EMAIL_MCP_MAIL_DIR; otherwise picks the newest ~/Library/Mail/V*.
    """
    override = os.environ.get("EMAIL_MCP_MAIL_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    base = Path.home() / "Library" / "Mail"
    if not base.exists():
        raise FileNotFoundError(
            f"{base} does not exist. Apple Mail is not configured on this Mac, "
            f"or grant Full Disk Access to the app running Claude Code."
        )
    try:
        entries = list(base.iterdir())
    except PermissionError as e:
        # The TCC case: macOS denies access with the directory very much
        # existing, so the not-exists branch above never fires — its remedy
        # was written for exactly this failure and was unreachable for it.
        # The first user read a 10-frame traceback out of `setup` instead
        # of the words "Full Disk Access" (2026-08-01).
        raise PermissionError(
            f"{base} exists but cannot be read ({e.strerror or e}) — grant "
            f"Full Disk Access to the app running Claude Code (System "
            f"Settings → Privacy & Security → Full Disk Access)."
        ) from e
    versioned = [
        p for p in entries
        if p.is_dir() and re.fullmatch(r"V\d+", p.name)
    ]
    if not versioned:
        raise FileNotFoundError(
            f"No V<N> directory found under {base}. "
            f"Grant Full Disk Access to the app running Claude Code."
        )
    # Highest version number wins (V10 > V9 > V8 …).
    return max(versioned, key=lambda p: int(p.name[1:]))


def source_name() -> str:
    return os.environ.get("EMAIL_MCP_SOURCE", "apple").strip() or "apple"


def read_only() -> bool:
    """When true, only the read-side tools register with the MCP server —
    the widest trust envelope for demos, reviews and new users. Staged
    mutation (triage_plan / triage_plan_delete) counts as mutating: intent
    plus a durable plan file. Flip with EMAIL_MCP_READ_ONLY=1.
    """
    return os.environ.get("EMAIL_MCP_READ_ONLY", "0").strip() in {
        "1", "true", "True", "yes",
    }


def max_body_bytes() -> int:
    return int(os.environ.get("EMAIL_MCP_MAX_BODY_BYTES", "2000000"))


def attach_dir() -> Path:
    raw = os.environ.get("EMAIL_MCP_ATTACH_DIR", "").strip()
    if raw:
        d = Path(raw).expanduser()
    else:
        tmp = os.environ.get("TMPDIR", "/tmp")
        d = Path(tmp) / "email-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------- #
# Sending (send_email / reply_email). All optional; defaults are empty — #
# real values live in ~/.email-mcp/identities.toml or the env vars here. #
#                                                                        #
# Rationale: macOS Mail.app's scripted-compose path wraps the body in a  #
# collapsed blockquote (renders empty in Outlook), so we do NOT send     #
# through Mail.app. Messages are composed here and delivered over the    #
# sending identity's transport (ssh_sendmail / smtp / pipe).             #
# ---------------------------------------------------------------------- #


def send_from_addr() -> str:
    """The From: address for outgoing mail. Empty (default) → no identity
    can be synthesized from the environment: sending needs either
    ~/.email-mcp/identities.toml or EMAIL_MCP_FROM_ADDR. Reads are
    unaffected — they need no sending configuration at all."""
    return os.environ.get("EMAIL_MCP_FROM_ADDR", "").strip()


def send_from_name() -> str:
    return os.environ.get("EMAIL_MCP_FROM_NAME", "").strip()


def send_allow_all() -> bool:
    """Sending is unrestricted unless a restriction is DECLARED (the one
    rule, mirrored by the TOML loader): EMAIL_MCP_SEND_ALLOW_ALL=0 engages
    the guard, and so does setting EMAIL_MCP_SEND_ALLOWLIST — a declared
    allowlist is meant to bind. Default flipped open 2026-08-01: the
    client's per-send permission prompt is the everyday checkpoint; the
    guard is the opt-in trial harness, not the product posture.
    """
    raw = os.environ.get("EMAIL_MCP_SEND_ALLOW_ALL", "").strip()
    if raw:
        return raw in {"1", "true", "True", "yes"}
    return not os.environ.get("EMAIL_MCP_SEND_ALLOWLIST", "").strip()


def send_allowlist() -> set[str]:
    """Lower-cased set of addresses sending is permitted to reach when
    allow_all is off. Empty env → defaults to just the From: address.
    """
    raw = os.environ.get("EMAIL_MCP_SEND_ALLOWLIST", "").strip()
    if not raw:
        return {send_from_addr().lower()}
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def send_bcc_self() -> bool:
    """Bcc the From: address on every send, so there's a searchable record
    in Mail (delivery does not populate Exchange Sent Items).
    """
    return os.environ.get("EMAIL_MCP_BCC_SELF", "1").strip() in {
        "1", "true", "True", "yes",
    }


def send_host() -> str:
    return os.environ.get("EMAIL_MCP_SEND_HOST", "").strip()


def send_user() -> str:
    return os.environ.get("EMAIL_MCP_SEND_USER", "").strip()


def send_ssh_socket() -> Path:
    raw = os.environ.get(
        "EMAIL_MCP_SSH_SOCKET", "~/.ssh/email-mcp-sock"
    ).strip()
    return Path(raw).expanduser()


def send_delivery_cmd() -> str:
    return os.environ.get("EMAIL_MCP_DELIVERY_CMD", "/usr/sbin/sendmail").strip()


def state_dir() -> Path:
    """Path of the managed state root itself. A path question only —
    never creates."""
    return _state().root


def spool_dir() -> Path:
    """Path of the scheduled-mail spool (frozen .eml + .json manifests):
    <state root>/spool. A path question only — never creates."""
    return _state().spool


def send_max_retries() -> int:
    """Delivery attempts per scheduled message before it parks in failed/."""
    return int(os.environ.get("EMAIL_MCP_SEND_RETRIES", "5"))


def graph_dir() -> Path:
    """Path of the Graph executor state (per-identity OAuth token caches):
    <state root>/graph. A path question only — never creates."""
    return _state().graph


# ---------------------------------------------------------------------- #
# Triage (triage_plan / triage_apply / mailbox_create)                    #
# ---------------------------------------------------------------------- #


def plans_dir() -> Path:
    """Path of the triage plan store (frozen plan JSONs):
    <state root>/plans. A path question only — never creates."""
    return _state().plans


def triage_max_messages() -> int:
    """Hard cap on messages per plan; bigger selections are rejected,
    never silently truncated."""
    return int(os.environ.get("EMAIL_MCP_TRIAGE_MAX", "200"))


def triage_delete_max() -> int:
    """Tighter cap for delete plans (triage_plan_delete) — the destructive
    verb gets its own, smaller ceiling than triage_max_messages."""
    return int(os.environ.get("EMAIL_MCP_TRIAGE_DELETE_MAX", "50"))


def triage_ttl_seconds() -> int:
    """How long a draft plan stays applicable."""
    return int(os.environ.get("EMAIL_MCP_TRIAGE_TTL", "600"))


def triage_timeout_seconds() -> float:
    """Apply's per-chunk osascript timeout; 0 (default) = auto from the
    chunk's message count."""
    return float(os.environ.get("EMAIL_MCP_TRIAGE_TIMEOUT", "0"))


def triage_verify_polls() -> int:
    return int(os.environ.get("EMAIL_MCP_TRIAGE_VERIFY_POLLS", "3"))


def triage_verify_interval() -> float:
    return float(os.environ.get("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "2.0"))


# ---------------------------------------------------------------------- #
# FTS body index (email_mcp.fts)                                          #
# ---------------------------------------------------------------------- #


def fts_dir() -> Path:
    """Path of the local FTS5 body index: <state root>/fts. A path
    question only — never creates."""
    return _state().fts


def fts_enabled() -> bool:
    """Master switch for FTS body hits in search. On by default; flip
    EMAIL_MCP_FTS_ENABLED=0 to fall back to snippet-only search."""
    return os.environ.get("EMAIL_MCP_FTS_ENABLED", "1").strip() in {
        "1", "true", "True", "yes",
    }


def fts_max_hits() -> int:
    """Cap on FTS rowid hits folded into one search (newest kept)."""
    return int(os.environ.get("EMAIL_MCP_FTS_MAX_HITS", "2000"))


def fts_inline_batch() -> int:
    """Max documents the inline (search-time) incremental pass indexes."""
    return int(os.environ.get("EMAIL_MCP_FTS_INLINE_BATCH", "500"))


def fts_inline_budget() -> float:
    """Wall-clock budget in seconds for the inline incremental pass."""
    return float(os.environ.get("EMAIL_MCP_FTS_INLINE_BUDGET", "2.0"))


def fts_doc_cap_bytes() -> int:
    """Per-document cap on extracted body text handed to the index."""
    return int(os.environ.get("EMAIL_MCP_FTS_DOC_CAP", "524288"))


def fts_reconcile_days() -> int:
    """How often --sync folds in a full rowid-set reconciliation."""
    return int(os.environ.get("EMAIL_MCP_FTS_RECONCILE_DAYS", "7"))


# ---------------------------------------------------------------------- #
# Audit ledger (email_mcp.audit)                                          #
# ---------------------------------------------------------------------- #


def audit_dir() -> Path:
    """Path of the append-only audit ledger (monthly JSONL event files):
    <state root>/audit. A path question only — never creates."""
    return _state().audit


def send_max_attach_mb() -> float:
    """Total attachment budget per message, in MB (pre-base64 file bytes).

    Default 20 MB — comfortably under common 25-50 MB server caps once the
    ~33% base64 overhead is added. Override with EMAIL_MCP_MAX_ATTACH_MB.
    """
    return float(os.environ.get("EMAIL_MCP_MAX_ATTACH_MB", "20"))


def log_file() -> Path | None:
    """Where the MCP writes its debug log (delivery pipeline, SSH health).

    EMAIL_MCP_LOG_FILE overrides the path; the value 'off' disables file
    logging. Default is ~/Library/Logs/email-mcp.log (macOS convention,
    visible in Console.app).
    """
    raw = os.environ.get("EMAIL_MCP_LOG_FILE", "").strip()
    if raw.lower() in {"off", "none", "0"}:
        return None
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "Library" / "Logs" / "email-mcp.log"


def log_level() -> str:
    return os.environ.get("EMAIL_MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO"


def send_bootstrap_cmd() -> str:
    """Shell command that (re)establishes the ControlMaster socket headlessly.

    Empty (default) → no bootstrap: a cold socket is reported as a clear
    transport error instead of being repaired. Point EMAIL_MCP_SSH_BOOTSTRAP
    (or an identity's `bootstrap` param) at your own script — the repo's
    tools/lxplus_mail_master.sh is a documented example.
    """
    return os.environ.get("EMAIL_MCP_SSH_BOOTSTRAP", "").strip()


def identities_file() -> Path:
    """The identities TOML routing From: addresses to transports.

    EMAIL_MCP_IDENTITIES overrides the path; the default lives in the
    RESOLVED state root — identities.toml is part of the managed tree
    (setup writes it, checks hold it to 0600, --purge removes it), so the
    one root override moves the whole tree, this file included. Pinning
    it to the default spelling made setup write one file and sending read
    another whenever EMAIL_MCP_STATE_DIR was set. A refused root falls
    back to the default spelling: identity ROUTING must keep answering
    (immediate sends need no managed state), and every write that needs
    the tree still refuses on its own.

    Absent file → a single identity is synthesized from the send_*
    getters above (see email_mcp.identities).
    """
    raw = os.environ.get("EMAIL_MCP_IDENTITIES", "").strip()
    if raw:
        return Path(raw).expanduser()
    r = state.State.resolve()
    root = r.root if isinstance(r, state.Resolved) else state.default_root()
    return root / "identities.toml"
