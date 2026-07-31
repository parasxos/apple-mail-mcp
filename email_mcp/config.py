"""Runtime configuration via environment variables. All optional."""
from __future__ import annotations

import os
import re
from pathlib import Path


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
    versioned = [
        p for p in base.iterdir()
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
    _refuse_degenerate(d, bool(raw))
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
    """When false (default), recipients are restricted to the allowlist —
    the trial-safety guard so a mistake can only reach your own address.

    Flip EMAIL_MCP_SEND_ALLOW_ALL=1 to send to anyone.
    """
    return os.environ.get("EMAIL_MCP_SEND_ALLOW_ALL", "0").strip() in {
        "1", "true", "True", "yes",
    }


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


class StateDirRefused(OSError):
    """An EMAIL_MCP_*_DIR resolves to $HOME or an ancestor of it.

    The fence lives HERE, in the getter that performs the mkdir and chmod,
    rather than only in `lifecycle._degenerate_overrides` / `repairs._degenerate`
    — those cover `setup` and `doctor --fix`, but the MCP server, the launchd
    dispatcher, the FTS agent and the Graph token cache all reach the getters
    directly. Refusing loudly beats silently tightening a directory the user
    did not name: `EMAIL_MCP_SPOOL_DIR=/Users` would otherwise `chmod 0700
    /Users`, breaking every other account's traversal.
    """


def _is_same_dir(a: Path, b: Path) -> bool:
    """Directory identity by inode, not by spelling."""
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _spelling(p: Path) -> str:
    """Last-resort key for a path that does not exist yet.

    NFC + casefold, because the comparison is only reached when neither
    path can be stat'd and macOS stores filenames in NFD.
    """
    import unicodedata

    return unicodedata.normalize("NFC", str(p)).casefold()


def _degenerate(d: Path) -> bool:
    """True when `d` is $HOME or an ancestor of it, however it is spelled.

    Compared by INODE, not by resolved string. macOS ships a
    case-insensitive APFS volume and firmlinks `/Users` into
    `/System/Volumes/Data/Users`, so `/users/paris`, `/USERS/PARIS`,
    `/System/Volumes/Data/Users/paris` and `$HOME` are one directory with
    four spellings — `Path.resolve()` preserves the spelling it was given
    and returns four different strings. A string compare therefore passed
    every one of them straight through the fence and chmodded the real
    home directory; only the exact spelling was ever refused.
    """
    home = Path.home()
    if _is_same_dir(d, home):
        return True
    for ancestor in home.resolve().parents:
        if _is_same_dir(d, ancestor):
            return True
    # Neither path exists yet (nothing to stat): fall back to a normalized
    # textual compare, which at least catches case and unicode variants.
    if not d.exists():
        try:
            target, root = _spelling(d.resolve()), _spelling(home.resolve())
        except OSError:
            return False
        if target == root:
            return True
        return any(target == _spelling(a) for a in home.resolve().parents)
    return False


def _refuse_degenerate(d: Path, overridden: bool) -> None:
    """Raise BEFORE any filesystem effect when an override names $HOME or above.

    Order is the point: an earlier version of this fence lived inside the
    chmod helper, which ran after spool_dir's five-subdir mkdir loop — the
    refusal fired, and the directories were already created at the refused
    location. Every getter must call this before its first mkdir.
    """
    if overridden and _degenerate(d):
        raise StateDirRefused(
            f"{d} is your home directory or above it — refusing to create "
            "state there or change its mode. Point the EMAIL_MCP_*_DIR "
            "override at a directory of its own."
        )
    # NOTE: a symlink at ~/.email-mcp is NOT refused. Relocating the state
    # root onto another volume with a link is a supported shape (see
    # tests/test_audit.py::test_default_path_never_chmods_a_symlinked_state_root):
    # the link is followed, the dirs under it are ours to create 0700, and
    # the link's TARGET keeps its own mode. The rule is "never change modes
    # behind a link", not "never follow one". A squat AT a managed leaf —
    # ~/.email-mcp/audit — is a different case and is refused
    # (AuditDirRefused).


def _lock_down(d: Path, overridden: bool) -> None:
    """Tighten a state dir to 0700, and its parent only when we own it.

    ``~/.email-mcp`` is ours to lock down. The parent of an
    EMAIL_MCP_*_DIR the operator named is NOT: pointing a state dir at
    ``/somewhere/mine/spool`` must not take ``/somewhere/mine`` to 0700
    behind their back, and a symlinked parent would land the chmod on its
    target. Same rule audit_dir already applies.
    """
    _refuse_degenerate(d, overridden)  # belt: holds even for a new caller
    if not overridden and not d.parent.is_symlink():
        d.parent.chmod(0o700)
    if not d.is_symlink():
        d.chmod(0o700)


def spool_dir(create: bool = True) -> Path:
    """Root of the scheduled-mail spool (frozen .eml + .json manifests).

    Default ~/.email-mcp/spool, override with EMAIL_MCP_SPOOL_DIR. The tree
    holds fully-composed outgoing mail (bodies + attachments), so it is
    created 0700 and kept that way.

    Pass create=False to resolve the path without touching the filesystem:
    read-side callers (doctor) must never create. Without that, a plain
    `email-mcp doctor` materialised the five spool subdirectories wherever
    EMAIL_MCP_SPOOL_DIR pointed — including inside ~/Library/Mail.
    """
    raw = os.environ.get("EMAIL_MCP_SPOOL_DIR", "").strip()
    d = Path(raw).expanduser() if raw else Path.home() / ".email-mcp" / "spool"
    if create:
        _refuse_degenerate(d, bool(raw))  # BEFORE the mkdir loop, not after
        # _lock_down spares `d` itself when it is a link, but this loop runs
        # BEFORE it and mkdir/chmod resolve straight through — a link
        # squatting on the spool root had its target's five pre-existing
        # subdirectories chmodded 0700.
        linked_root = d.is_symlink()
        for sub in ("pending", "sending", "sent", "failed", "cancelled"):
            s = d / sub
            if linked_root:
                continue
            s.mkdir(parents=True, exist_ok=True)
            if not s.is_symlink():
                # The subdirs hold composed outgoing mail too — the 0700
                # parent already blocks traversal, but the invariant is
                # stated per-directory, so make it true per-directory.
                s.chmod(0o700)
        _lock_down(d, bool(raw))
    return d


def send_max_retries() -> int:
    """Delivery attempts per scheduled message before it parks in failed/."""
    return int(os.environ.get("EMAIL_MCP_SEND_RETRIES", "5"))


def graph_dir() -> Path:
    """Root of the Graph executor state (per-identity OAuth token caches).

    Default ~/.email-mcp/graph, override with EMAIL_MCP_GRAPH_DIR. Token
    files grant delegated mailbox access, so the dir is created 0700 and
    kept that way (same pattern as spool_dir).
    """
    raw = os.environ.get("EMAIL_MCP_GRAPH_DIR", "").strip()
    d = Path(raw).expanduser() if raw else Path.home() / ".email-mcp" / "graph"
    _refuse_degenerate(d, bool(raw))
    d.mkdir(parents=True, exist_ok=True)
    _lock_down(d, bool(raw))
    return d


# ---------------------------------------------------------------------- #
# Triage (triage_plan / triage_apply / mailbox_create)                    #
# ---------------------------------------------------------------------- #


def plans_dir(create: bool = True) -> Path:
    """Root of the triage plan store (frozen plan JSONs). Created 0700 —
    plans carry message metadata.

    Pass create=False to resolve the path without touching the filesystem:
    read-side callers (doctor) must never create, which also stops a
    diagnostic from materialising directories wherever EMAIL_MCP_PLANS_DIR
    happens to point."""
    raw = os.environ.get("EMAIL_MCP_PLANS_DIR", "").strip()
    d = Path(raw).expanduser() if raw else Path.home() / ".email-mcp" / "plans"
    if create:
        _refuse_degenerate(d, bool(raw))
        d.mkdir(parents=True, exist_ok=True)
        _lock_down(d, bool(raw))
    return d


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
    """Batch osascript timeout; 0 (default) = auto from message count."""
    return float(os.environ.get("EMAIL_MCP_TRIAGE_TIMEOUT", "0"))


def triage_verify_polls() -> int:
    return int(os.environ.get("EMAIL_MCP_TRIAGE_VERIFY_POLLS", "3"))


def triage_verify_interval() -> float:
    return float(os.environ.get("EMAIL_MCP_TRIAGE_VERIFY_INTERVAL", "2.0"))


# ---------------------------------------------------------------------- #
# FTS body index (email_mcp.fts)                                          #
# ---------------------------------------------------------------------- #


def fts_dir(create: bool = True) -> Path:
    """Root of the local FTS5 body index. Created 0700 like plans_dir —
    the index stores extracted message bodies.

    Default ~/.email-mcp/fts, override with EMAIL_MCP_FTS_DIR. Pass
    create=False to resolve the path without touching the filesystem —
    read paths (search, --status) must never create the index directory.
    """
    raw = os.environ.get("EMAIL_MCP_FTS_DIR", "").strip()
    d = Path(raw).expanduser() if raw else Path.home() / ".email-mcp" / "fts"
    if create:
        _refuse_degenerate(d, bool(raw))
        d.mkdir(parents=True, exist_ok=True)
        _lock_down(d, bool(raw))
    return d


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


class AuditDirRefused(OSError):
    """The ledger directory resolves through a symlink we must not follow.

    Raised only by ``audit_dir(create=True)``; ``audit.emit`` catches it
    and drops the event with a warning, so a squatting link costs receipts
    — never a mutation (contract §6, emit-failure policy).
    """


def audit_dir(create: bool = True) -> Path:
    """Root of the append-only audit ledger (monthly JSONL event files).
    Created 0700 like plans_dir — events carry recipients and subjects.

    Default ~/.email-mcp/audit, override with EMAIL_MCP_AUDIT_DIR. Pass
    create=False to resolve the path without touching the filesystem —
    read paths (query, --status) must never create the ledger directory.

    Symlink discipline mirrors repairs' ``_tree_links``: mkdir and chmod
    both resolve through a redirection, so a link squatting on the
    DEFAULT path — the one path this package owns — is refused outright
    (``AuditDirRefused``) rather than followed onto an arbitrary victim
    directory. A link the operator named with EMAIL_MCP_AUDIT_DIR is
    theirs to follow, but its target's mode is still not ours to set.
    """
    raw = os.environ.get("EMAIL_MCP_AUDIT_DIR", "").strip()
    d = Path(raw).expanduser() if raw else Path.home() / ".email-mcp" / "audit"
    if create:
        # The fence the first fix missed: audit.emit() calls this on EVERY
        # mutation, so an unfenced audit_dir let EMAIL_MCP_AUDIT_DIR=/Users
        # chmod it 0700 and write the ledger there.
        _refuse_degenerate(d, bool(raw))
        linked = d.is_symlink()
        if linked and not raw:
            raise AuditDirRefused(
                f"{d} is a symlink — refusing to create or chmod the ledger "
                "through it; remove the link or set EMAIL_MCP_AUDIT_DIR"
            )
        d.mkdir(parents=True, exist_ok=True)
        if not raw and not d.parent.is_symlink():
            # Lock down ~/.email-mcp itself. An env-overridden dir's
            # parent is not ours to chmod (and may refuse — e.g. a
            # root-owned temp root), which must never cost an event; a
            # symlinked parent would land the chmod on its target.
            d.parent.chmod(0o700)
        if not linked:
            d.chmod(0o700)
    return d


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

    EMAIL_MCP_IDENTITIES overrides the path; default is
    ~/.email-mcp/identities.toml. Absent file → a single identity is
    synthesized from the send_* getters above (see email_mcp.identities).
    """
    raw = os.environ.get("EMAIL_MCP_IDENTITIES", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".email-mcp" / "identities.toml"
