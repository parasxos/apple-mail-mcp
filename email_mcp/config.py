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
    """The chosen state root is not a directory this tool may manage.

    Raised BEFORE any filesystem effect. What makes a root refusable: it is
    $HOME or an ancestor of it; an override names an existing directory
    that already holds someone else's files and carries no ownership
    marker; a non-directory squats on it; or a retired per-directory
    variable is still set (see RETIRED_STATE_VARS).
    """


STATE_MARKER = ".email-mcp-root"
STATE_ROOT_VERSION = 1
_LEAVES = ("spool", "plans", "graph", "fts", "audit")

# The five per-directory overrides EMAIL_MCP_STATE_DIR replaced in v0.11.
#
# Migration policy: REJECTED, never ignored. Silently ignoring them would
# relocate live state without saying so — a user with
# EMAIL_MCP_SPOOL_DIR=/Volumes/big/spool would find scheduled mail apparently
# vanished (it is in the old directory, undelivered, and the dispatcher is
# now looking somewhere else). Refusing costs one startup error and a one-line
# fix; ignoring costs mail that looks lost. See docs/reference.md.
RETIRED_STATE_VARS = (
    "EMAIL_MCP_SPOOL_DIR", "EMAIL_MCP_PLANS_DIR", "EMAIL_MCP_GRAPH_DIR",
    "EMAIL_MCP_FTS_DIR", "EMAIL_MCP_AUDIT_DIR",
)


def retired_state_vars() -> list[str]:
    """Retired per-directory variables that are still set, sorted."""
    return sorted(v for v in RETIRED_STATE_VARS
                  if os.environ.get(v, "").strip())


def retired_state_var_error() -> str | None:
    """The migration message for any retired variable still set, or None.

    Pure, total, and — unlike :func:`state_root_refusal` — free of any
    filesystem question, which is why READ paths can and must consult it
    too. Rejection used to live only on the write path, so a read tool
    silently ignored the variable: with EMAIL_MCP_SPOOL_DIR set,
    ``list_scheduled`` resolved the DEFAULT root, found nothing, and
    returned ``{"ok": true, "pending": []}`` while the user's queued mail
    sat in the old directory with nothing delivering it. That is precisely
    the "mail that looks lost" outcome the rejection exists to prevent, so
    it has to fail closed on every entry point, not just on writes.
    """
    retired = retired_state_vars()
    if not retired:
        return None
    return (
        f"{', '.join(retired)} {'is' if len(retired) == 1 else 'are'} "
        "retired — v0.11 derives spool, plans, graph, fts and audit from "
        "ONE root. Ignoring the variable would relocate live state without "
        "telling you (scheduled mail would still be in the old directory, "
        "and nothing would be delivering it). Unset "
        f"{'it' if len(retired) == 1 else 'them'} and set "
        "EMAIL_MCP_STATE_DIR to the parent directory instead. Moving "
        "existing contents into it makes that directory non-empty and "
        "unmarked, which is refused in turn — adopt it deliberately by "
        f"writing the {STATE_MARKER} marker, then run `email-mcp doctor "
        "--fix` for the modes. Full procedure: docs/reference.md, "
        "'Migrating from the per-directory variables'."
    )


def _is_same_dir(a: Path, b: Path) -> bool:
    """Directory identity by inode, not by spelling.

    macOS ships case-insensitive APFS and firmlinks `/Users` into
    `/System/Volumes/Data/Users`, so `$HOME`, `/users/paris`,
    `/USERS/PARIS` and the firmlink are one directory with four spellings —
    and `Path.resolve()` returns whichever it was handed. Comparing text
    let every variant but the exact one walk through the fence.
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _is_home_or_above(d: Path) -> bool:
    home = Path.home()
    if _is_same_dir(d, home):
        return True
    return any(_is_same_dir(d, a) for a in home.resolve().parents)


def is_dir_safe(p: Path) -> bool:
    """`p.is_dir()` that cannot raise.

    A refused or unreadable state root (mode 000) makes even a stat throw,
    and read paths must ANSWER rather than raise — "not a directory I can
    see" is the honest result. Used by every read-side probe that stats a
    path derived from the root.
    """
    try:
        return p.is_dir()
    except OSError:
        return False


def _marked(root: Path) -> bool:
    """True when the ownership marker is present. TOTAL: a root we cannot
    stat into (mode 000) is simply "not marked" — this is consulted from
    state_root_refusal(), which is documented pure and total, and an
    unreadable root must produce a REFUSAL there, not a PermissionError."""
    try:
        return (root / STATE_MARKER).is_file()
    except OSError:
        return False


def _mark(root: Path) -> None:
    """Stamp a root as ours. Best effort — a read-only volume must not
    break sending, and the marker is a safety hint, not a lock."""
    import json

    marker = root / STATE_MARKER
    try:
        marker.write_text(json.dumps(
            {"tool": "email-mcp", "root_version": STATE_ROOT_VERSION}) + "\n")
        marker.chmod(0o600)
    except OSError:
        pass


def _make_ours(d: Path) -> bool:
    """Create `d` if absent. True when WE created it.

    The whole permission story rests here: a directory this tool creates is
    ours to mode 0700, and a directory that already existed is never
    chmodded at all. That makes "email-mcp changed the mode of a directory
    I did not name" unrepresentable rather than fenced — three release
    gates in a row found a new way past the fences.
    """
    try:
        # parents=False deliberately. `parents=True` creates every missing
        # intermediate at the process umask, so EMAIL_MCP_STATE_DIR=
        # /a/b/c/root produced a/, b/ and c/ world-readable — directories
        # the user never named, made by a mail tool, at 0755. We do not
        # chmod them (they are not ours), which left the only options
        # "create them loose" or "do not create them at all". A missing
        # parent is a configuration mistake with a one-line fix, so it is
        # refused in state_root_refusal rather than papered over here.
        d.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        return False
    # umask can only clear bits, so mkdir(0o700) may land tighter but never
    # looser; chmod pins it exactly.
    d.chmod(0o700)
    return True


def _configured_root() -> tuple[Path, bool, str | None]:
    """(root, explicit, unresolvable) — the configured state root, whether
    the user named it, and why it could not be resolved (None = fine).

    TOTAL: this never raises. Two ways resolution itself fails, both found
    at the v0.11 gate, both of which used to escape as a bare RuntimeError
    or FileNotFoundError from a function documented as total:

      * ``~nosuchuser/foo`` — ``expanduser()`` raises RuntimeError when the
        named user has no home directory;
      * a relative value while the process's cwd has been deleted —
        ``absolute()`` raises FileNotFoundError.

    Both are configuration faults, so they become a REASON (reported by
    doctor, raised as StateDirRefused on the write path) rather than a
    traceback out of uninstall planning or a read tool.
    """
    raw = os.environ.get("EMAIL_MCP_STATE_DIR", "").strip()
    try:
        root = Path(raw).expanduser() if raw else Path.home() / ".email-mcp"
    except (RuntimeError, OSError) as e:
        return Path(raw or "~/.email-mcp"), bool(raw), str(e)
    # Collapse `..` TEXTUALLY before validating, so the path we check is the
    # path we then use. `$HOME/sub/..` names $HOME but cannot be stat'd while
    # `sub` is absent, so an identity check saw nothing to compare and the
    # mkdir afterwards created `sub` and marked $HOME. Textual collapse is
    # the conservative direction here: it can only make us refuse more.
    try:
        return Path(os.path.normpath(root.absolute())), bool(raw), None
    except OSError as e:
        return root, bool(raw), str(e)


def state_root_refusal() -> str | None:
    """Why the configured state root may not be managed, or None.

    PURE: it stats, it never creates. This is the single definition of
    "refusable", so a caller can ask WITHOUT triggering the write that
    would raise — which is what read-side and repair-side code needs.

    Resolving a refusable root used to be possible through
    ``state_root(create=False)``: the create flag short-circuited before
    the checks, handed back ``$HOME``, and ``doctor --fix`` then created
    ``$HOME/spool`` from it. Validation belongs to resolution; only the
    EFFECT is conditional on `create`.
    """
    retired = retired_state_var_error()
    if retired:
        return retired
    root, explicit, unresolvable = _configured_root()
    if unresolvable:
        return (f"EMAIL_MCP_STATE_DIR={root} cannot be resolved to a path "
                f"({unresolvable}). Point it at an absolute path in a "
                "directory that exists.")
    if _is_home_or_above(root):
        return (f"{root} is your home directory or above it — refusing to "
                "manage state there. Point EMAIL_MCP_STATE_DIR at a "
                "directory of its own.")
    # A non-directory squatting on the root is refused whether or not the
    # user named it: the default ~/.email-mcp can be a stray file too, and
    # that used to escape as a raw NotADirectoryError from the first getter
    # rather than as a refusal.
    if root.exists() and not root.is_dir() and not root.is_symlink():
        return (f"{root} exists and is not a directory — refusing to manage "
                "it. Move it aside, or point EMAIL_MCP_STATE_DIR at a "
                "directory.")
    if not explicit:
        # The DEFAULT root is never refused for its CONTENTS: ~/.email-mcp
        # predates the marker, and every v0.10 install has one full of our
        # own files. Adopting it is the upgrade path (see docs/reference).
        return None
    if not root.exists() and not root.parent.is_dir():
        # We create the root, never a chain of directories above it: those
        # would land at the process umask (0755) and belong to nobody.
        return (f"{root.parent} does not exist, so {root} cannot be created "
                "without also creating directories above it. Create the "
                "parent yourself, or point EMAIL_MCP_STATE_DIR inside an "
                "existing directory.")
    if root.exists() and not _marked(root):
        try:
            # The marker is OURS, so it is not "someone else's files" —
            # counting it made a freshly adopted root look foreign.
            others = any(p.name != STATE_MARKER for p in root.iterdir())
        except OSError as e:
            # We cannot tell whether this directory is someone else's, so
            # we must not adopt it. Treating an unlistable root as EMPTY
            # was a fail-open: `chmod 0300` on a directory full of another
            # tool's files made it silently adoptable — marker written,
            # spool and ledger created inside — which is exactly what this
            # check exists to prevent. Every other branch here errs toward
            # refusing; this one has to as well.
            return (f"{root} cannot be read ({e.strerror}), so whether it "
                    "already holds someone else's files cannot be "
                    "determined. Refusing to manage it — fix its "
                    "permissions, or point EMAIL_MCP_STATE_DIR elsewhere.")
        # Re-read the marker before refusing. Two writers exist (server and
        # the launchd dispatcher), and they race on the first mutation
        # after a root is configured: one can pass the _marked() check
        # above, have the other adopt the root underneath it, and then see
        # that adoption as foreign content. The refusal has to be based on
        # the marker as it stands AFTER the scan, not before it.
        if others and not _marked(root):
            return (f"{root} already contains files and is not an email-mcp "
                    f"state directory (no {STATE_MARKER}). Refusing to manage "
                    "it — point EMAIL_MCP_STATE_DIR at a new or empty "
                    "directory.")
    return None


def state_root(create: bool = True) -> Path:
    """The one directory this tool manages: ~/.email-mcp, or EMAIL_MCP_STATE_DIR.

    ONE override replaces the five per-directory ones (spool/plans/graph/
    fts/audit). Every leaf is derived from this root, so there is a single
    place to validate and a single thing to relocate — the configuration
    complexity was itself the defect.

    Relocation onto another volume is supported, including via a symlinked
    root: the link is followed and the target keeps its own mode. What is
    refused is an override pointing at a directory that already holds
    someone else's files without our marker — which is what makes $HOME,
    /Users and every case-variant of them refusable without comparing a
    single path string.

    ``create=False`` resolves the path and touches NOTHING. It is total by
    design: uninstall planning, ``doctor`` and ``audit.query`` must be able
    to name the configured root even when it is refusable. Ask
    :func:`state_root_refusal` for the verdict; anything that intends to
    WRITE must go through ``create=True``, which raises.
    """
    root, _, _unresolvable = _configured_root()
    if not create:
        return root

    # --- validate BEFORE any filesystem effect -------------------------
    reason = state_root_refusal()
    if reason:
        raise StateDirRefused(reason)

    # --- effects -------------------------------------------------------
    _make_ours(root)          # pre-existing root keeps its own mode
    if not _marked(root):
        _mark(root)
    return root


def _leaf(name: str, create: bool = True) -> Path:
    """One managed subdirectory of the state root."""
    if not create:
        return state_root(create=False) / name
    d = state_root(create=True) / name
    if d.is_symlink():
        # A link on a leaf WE own is a squat: mkdir and chmod both resolve
        # through it, and the ledger or the spool would be written into the
        # target. (A symlinked ROOT is the user's own relocation and is fine.)
        raise StateDirRefused(
            f"{d} is a symlink — refusing to create state or set modes "
            "through it. Remove the link, or relocate the whole root with "
            "EMAIL_MCP_STATE_DIR."
        )
    _make_ours(d)
    return d


def spool_dir(create: bool = True) -> Path:
    """Scheduled-mail spool (frozen .eml + .json manifests), <root>/spool.

    Holds fully-composed outgoing mail, so anything this tool creates here
    is 0700. create=False resolves the path without touching the disk —
    read-side callers (doctor) must never create.
    """
    d = _leaf("spool", create=create)
    if create:
        for sub in ("pending", "sending", "sent", "failed", "cancelled"):
            s = d / sub
            if not s.is_symlink():
                _make_ours(s)
    return d


def send_max_retries() -> int:
    """Delivery attempts per scheduled message before it parks in failed/."""
    return int(os.environ.get("EMAIL_MCP_SEND_RETRIES", "5"))


def attach_dir() -> Path:
    """Materialised attachment scratch, $TMPDIR/email-mcp (override with
    EMAIL_MCP_ATTACH_DIR).

    Deliberately NOT under the state root: these are transient extracts of
    message content that the OS is free to reap, and folding them into the
    root would make `uninstall --purge` delete them and keep them across
    reboots. Same permission rule as everything else — 0700 when we create
    it, untouched when it already exists.
    """
    raw = os.environ.get("EMAIL_MCP_ATTACH_DIR", "").strip()
    if raw:
        d = Path(raw).expanduser()
    else:
        tmp = os.environ.get("TMPDIR", "/tmp")
        d = Path(tmp) / "email-mcp"
    if _is_home_or_above(d):
        raise StateDirRefused(
            f"{d} is your home directory or above it — refusing to write "
            "attachments there."
        )
    _make_ours(d)
    return d


def graph_dir(create: bool = True) -> Path:
    """Graph executor state (per-identity OAuth token caches), <root>/graph.
    Token files grant delegated mailbox access. create=False resolves
    without touching the disk, like every other managed getter — it was
    the one leaf missing the parameter, so read-side callers had to
    rebuild the path by hand instead of asking for it."""
    return _leaf("graph", create=create)


def plans_dir(create: bool = True) -> Path:
    """Triage plan store (frozen plan JSONs), <root>/plans — plans carry
    message metadata. create=False resolves without touching the disk."""
    return _leaf("plans", create=create)


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
    """Local FTS5 body index, <root>/fts — it stores extracted message
    bodies. create=False resolves without touching the disk: read paths
    (search, --status) must never create the index directory."""
    return _leaf("fts", create=create)


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


class AuditDirRefused(StateDirRefused):
    """The ledger directory resolves through a symlink we must not follow.

    Raised only by ``audit_dir(create=True)``; ``audit.emit`` catches it
    and drops the event with a warning, so a squatting link costs receipts
    — never a mutation (contract §6, emit-failure policy).
    """


def audit_dir(create: bool = True) -> Path:
    """Append-only audit ledger (monthly JSONL), <root>/audit — events
    carry recipients and subjects. create=False resolves without touching
    the disk; a symlink squatting on the leaf is refused
    (AuditDirRefused) rather than followed onto a victim directory."""
    if not create:
        return _leaf("audit", create=False)
    try:
        return _leaf("audit", create=True)
    except StateDirRefused as e:
        # Narrower type so audit.emit's existing catch keeps working; it is
        # a StateDirRefused subclass, so newer catches see it too.
        raise AuditDirRefused(str(e)) from e


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
