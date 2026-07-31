"""Lifecycle commands: ``email-mcp setup`` / ``update`` / ``uninstall``
(v0.11 L3+L4).

Setup — eight steps, run in order, each reported as a :class:`StepResult`:

1. **preflight** — macOS only; detect Mail.app, the macOS version and
   ``~/Library/Mail``; an existing ``meta.json`` flips the run into
   review/repair mode (nothing is ever installed twice blindly).
2. **permissions** — the doctor's own ``check_mail_store`` /
   ``check_automation`` / ``check_accessibility`` probes, with the check's
   own ``fix`` text on red and an Enter-to-retry / s-to-skip loop.
   Full Disk Access honesty: FDA binds when a process STARTS, so a grant
   made mid-wizard cannot help this process — the wizard says so and asks
   for a terminal restart instead of retry-looping forever.
3. **state_dirs** — the ``~/.email-mcp`` tree via the config getters
   (which already mkdir + chmod 0700), then ``meta.json`` (0600,
   tmp + rename). The FTS dir is DERIVED state and is never created here —
   ``email-mcp fts --build`` remains the only builder.
4. **identity** — the identities.toml question tree (per driver:
   ssh_sendmail / smtp / pipe). Secret VALUES are never read and never
   stored: for smtp the wizard prints the exact
   ``security add-generic-password`` command for the user to run
   themselves, or accepts an ``op://`` reference. Identity names must
   match ``^[A-Za-z0-9_-]{1,64}$`` — the name becomes
   ``graph/<name>.token.json``, so the regex is a path-traversal fence.
   Skipped entirely under ``--read-only``.
5. **launchd** — the dispatcher (scheduled send) and FTS (nightly sync)
   agents, asked per agent, installed via the modules' own
   ``install_launchd()``.
6. **fts** — full build / quick start (``--limit``) / defer / skip, with
   an honest time warning before a full first build.
7. **client_config** — the ready-to-paste ``mcpServers`` JSON with the
   ABSOLUTE entry-point path, plus the read-only env variant.
8. **smoke** — ``doctor.run()`` rendered as a table; optional self-send.

One ``lifecycle`` audit event (an additive event type, contract §8) is
emitted per run that touched state:
``detail = {mode, identities: [names], agents, fts_choice}`` — identity
NAMES only, never addresses, never secrets.

``email-mcp update`` (L4) is post-upgrade housekeeping: run the pending
:data:`MIGRATIONS` in ascending target order (each printed + logged),
re-render + re-bootstrap every INSTALLED launchd agent whose plist
differs from a fresh render (a moved venv bakes a dead ``sys.executable``
into ProgramArguments), stamp ``meta.json`` (state_version,
package_version, updated_at), run the doctor and point at the releases
page. Idempotent: a second run is a no-op report. One ``lifecycle`` event
(outcome ``update``) per run.

``email-mcp uninstall`` (L4) removes the launchd agents (both labels plus
the legacy ones) and the Graph token caches; state stays unless
``--purge``, which additionally removes the HARDCODED ``~/.email-mcp``
through the :func:`purge_state` fences (design D7: never an
env-overridden path, never a symlink, never outside ``$HOME``) plus
``~/Library/Logs/email-mcp*.log``. A typed confirmation guards the run
(``--yes`` skips); the ``lifecycle`` event (outcome ``uninstall``) is
emitted BEFORE removal; ``~/Library/Mail`` is never touched; Keychain and
1Password secrets are never deleted — the exact
``security delete-generic-password`` commands are printed for the user.

Printing here is by design: this module is a CLI entry point, not library
code (contract §7 lists the print-by-design entry points; the MCP serve
path never routes through here).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audit, config, doctor, ids
from .identities import KNOWN_FIELDS, NAME_RE
from .log import get_logger
from .transports import DRIVERS, SendError

_log = get_logger()

# The on-disk state schema version written to meta.json. `email-mcp
# update` walks MIGRATIONS from the recorded version up to here: each
# entry is (target_state_version, fn) where fn(meta) -> meta performs
# the on-disk work for that step and returns the updated meta dict.
# Empty at v0.11 BY DESIGN — the hook exists (and is exercised by tests
# with fake migrations) so the first real migration is a one-entry
# addition next to a STATE_VERSION bump.
STATE_VERSION = 1
MIGRATIONS: tuple[tuple[int, Callable[[dict], dict]], ...] = ()

# Identity names become graph/<name>.token.json — this regex is a path
# traversal fence, not cosmetics. Shared with identities.load(), which
# re-applies it to hand-edited files. Table/param keys get the same charset.
_NAME_RE = NAME_RE
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The compose-time header-injection fence (sender._CTL_RE): CR/LF/NUL in
# a header-bound value smuggles extra headers. The wizard refuses them at
# write time so identities.toml can never hold a From: compose will
# reject anyway.
_CTL_RE = re.compile(r"[\r\n\x00]")

# Keys whose VALUE would be a secret. identities.toml stores references
# (a Keychain item name, an op:// path), never the secrets themselves —
# a block carrying one of these is refused outright.
_SECRET_KEYS = frozenset({
    "password", "passwd", "app_password", "secret", "token",
    "access_token", "refresh_token", "api_key", "credential",
    "credentials",
})

# `op` and `keychain` are the only fields allowed to NAME a secret, which
# makes them the likeliest place a real one lands: the wizard's own prompt
# says "op://vault/item/field", and a pasted app password answers it just
# as readily. Both are therefore validated by SHAPE — anything that is not
# a well-formed reference is treated as the credential itself.
# 1Password item names may contain spaces ("op://Personal/gmail app
# password/password"); the field part may be a section/field pair, hence
# 3-4 segments.
_OP_SEGMENT = r"[^/\s][^/\x00-\x1f]*"
_OP_REF_RE = re.compile(rf"^op://{_OP_SEGMENT}(?:/{_OP_SEGMENT}){{2,3}}$")
# A Keychain service name is a label the user chose (the wizard offers
# email-mcp-<identity>). Whitespace and password punctuation are not part
# of that vocabulary, but are part of every pasted credential.
_KEYCHAIN_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")

# [name.graph] carries the public-client app registration only. The device
# -code flow has no client secret by design, so a secret-shaped key here
# is a smuggling attempt, not a config option.
_GRAPH_KEYS = frozenset({"tenant", "client_id"})

# EMAIL_MCP_* variables that redirect state paths; StatePaths.resolve()
# records which of these are in effect.
_PATH_ENV_VARS = (
    "EMAIL_MCP_MAIL_DIR", "EMAIL_MCP_STATE_DIR", "EMAIL_MCP_IDENTITIES",
)

_FDA_RESTART_NOTE = (
    "Full Disk Access binds when a process starts: after granting it, "
    "restart this terminal and re-run `email-mcp setup`."
)

# Module-level seam so tests can script the interactive prompts.
_input = input


class SetupError(Exception):
    """A caller-fixable problem with the setup answers (bad identity
    name, secret value where a reference belongs, unknown driver)."""


# --------------------------------------------------------------------- #
# meta.json + state paths                                                #
# --------------------------------------------------------------------- #


def meta_path() -> Path:
    return Path.home() / ".email-mcp" / "meta.json"


def read_meta() -> dict:
    """Parse meta.json; {} when absent or unreadable (same tolerance as
    cli._state_version — a corrupt meta must never crash the wizard)."""
    try:
        data = json.loads(meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(meta: dict) -> Path:
    """Write meta.json atomically: tmp in the same directory, chmod 0600,
    then rename over the target (the graph token-cache discipline)."""
    path = meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


@dataclass(frozen=True)
class StatePaths:
    """Every path the state tree owns, resolved WITHOUT filesystem side
    effects, plus the EMAIL_MCP_* overrides in effect."""

    root: Path
    spool: Path
    plans: Path
    graph: Path
    audit: Path
    fts: Path
    identities: Path
    meta: Path
    env_overrides: dict[str, str]

    @classmethod
    def resolve(cls) -> "StatePaths":
        """Resolve via the config getters where they are pure
        (audit_dir/fts_dir take create=False; identities_file only
        resolves) and via their documented env-or-default rule where they
        are not (spool_dir/plans_dir/graph_dir mkdir + chmod on every
        call — the purity mirror doctor._graph_token_dir and repairs
        follow)."""
        def env_dir(var: str, default: Path) -> Path:
            raw = os.environ.get(var, "").strip()
            return Path(raw).expanduser() if raw else default

        root = config.state_root(create=False)
        overrides = {
            var: os.environ[var] for var in _PATH_ENV_VARS
            if os.environ.get(var, "").strip()
        }
        return cls(
            root=root,
            spool=root / "spool",
            plans=root / "plans",
            graph=root / "graph",
            audit=config.audit_dir(create=False),
            fts=config.fts_dir(create=False),
            identities=config.identities_file(),
            # meta_path(), NOT root/"meta.json": meta.json is anchored to
            # ~/.email-mcp (the install stamp), so reporting it under a
            # relocated root named a file that does not exist.
            meta=meta_path(),
            env_overrides=overrides,
        )


# --------------------------------------------------------------------- #
# answers + results                                                      #
# --------------------------------------------------------------------- #


@dataclass
class Answers:
    """Scripted answers for a non-interactive run (``--yes`` uses the
    defaults below; ``--answers FILE`` loads a JSON object of them)."""

    read_only: bool = False
    # Scripted "s" to every red permission check; without it a red check
    # blocks a non-interactive run.
    skip_permissions: bool = False
    # With an existing identities.toml: keep it untouched or add/replace
    # the blocks in `identities`. (Interactive mode also offers edit.)
    identity_action: str = "keep"
    identities: list[dict] = field(default_factory=list)
    default_identity: str = ""
    install_dispatcher: bool = False
    install_fts_agent: bool = False
    fts_choice: str = "defer"  # full | quick | defer | skip
    fts_limit: int = 500
    self_send: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Answers":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(
                f"unknown answer key(s) {unknown}; valid: {sorted(known)}")
        answers = cls(**data)
        for name in ("read_only", "skip_permissions", "install_dispatcher",
                     "install_fts_agent", "self_send"):
            value = getattr(answers, name)
            if not isinstance(value, bool):
                # The string "false" is TRUTHY: a JSON answers file that
                # spelled a flag as text would silently arm it — and for
                # skip_permissions that means skipping every red check.
                raise ValueError(
                    f"{name} must be true or false, not {value!r}")
        if answers.identity_action not in {"keep", "add"}:
            raise ValueError(
                f"identity_action must be 'keep' or 'add', "
                f"not {answers.identity_action!r}")
        if answers.fts_choice not in {"full", "quick", "defer", "skip"}:
            raise ValueError(
                "fts_choice must be one of full/quick/defer/skip, "
                f"not {answers.fts_choice!r}")
        if not isinstance(answers.identities, list) or not all(
                isinstance(b, dict) for b in answers.identities):
            raise ValueError("identities must be a list of objects")
        return answers

    @classmethod
    def from_file(cls, path: str | Path) -> "Answers":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("answers file must hold one JSON object")
        return cls.from_dict(data)


@dataclass
class StepResult:
    """One setup step's outcome. ``blocked`` stops the run and makes the
    CLI exit nonzero; ``failed`` is a non-fatal step failure (the run
    continues — e.g. an FTS build that errored); ``skipped`` was a
    deliberate choice."""

    step: str
    status: str  # "ok" | "skipped" | "failed" | "blocked"
    detail: str = ""


# --------------------------------------------------------------------- #
# identities.toml writing (backup-first, tmp+rename, 0600)               #
# --------------------------------------------------------------------- #


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # json string escaping is a valid TOML basic string.
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise SetupError(
        f"cannot serialize a {type(value).__name__} into identities.toml")


def _fence_secret_keys(name: str, table: dict) -> None:
    for key, value in table.items():
        if str(key).lower() in _SECRET_KEYS:
            raise SetupError(
                f"identity [{name}]: key {key!r} refused — identities.toml "
                "never stores secret values. Put the app password in the "
                "macOS Keychain (security add-generic-password -s <item> "
                "-a <account> -w) or 1Password, and reference it with "
                "`keychain` / `op`."
            )
        if not _KEY_RE.fullmatch(str(key)):
            raise SetupError(
                f"identity [{name}]: key {key!r} is not a bare TOML key "
                "([A-Za-z0-9_-]+).")
        if isinstance(value, dict):
            _fence_secret_keys(f"{name}.{key}", value)


def _reference_problem(key: str, value: str) -> str:
    """Why `value` is not a usable secret REFERENCE for `key` — "" when it
    is one.

    The prose never echoes the value: if this fence is doing its job the
    value is a live credential, and the caller prints what it returns.
    """
    if key == "op":
        if not _OP_REF_RE.fullmatch(value):
            return ("`op` must be a 1Password secret reference of the form "
                    "op://<vault>/<item>/<field>")
        return ""
    if value.lower().startswith("op://"):
        return ("`keychain` names a macOS Keychain item, not a 1Password "
                "reference — move that value to `op`")
    if not _KEYCHAIN_ITEM_RE.fullmatch(value):
        return ("`keychain` must be a Keychain item name (letters, digits "
                "and .-_@+, no spaces), not a secret")
    return ""


def _fence_secret_refs(name: str, table: dict) -> None:
    """Refuse a reference field that does not have a reference's shape.

    An empty value is left to the driver-specific "smtp needs a secret
    REFERENCE" fence, which explains the remedy better.
    """
    for key in ("op", "keychain"):
        value = str(table.get(key, "")).strip()
        if not value:
            continue
        problem = _reference_problem(key, value)
        if problem:
            raise SetupError(
                f"identity [{name}]: {problem}. Nothing was written, and "
                "the value is not repeated here in case it IS the "
                "credential. Store the app password once with `security "
                "add-generic-password -s <item> -a <account> -w` (or in "
                "1Password) and name the reference here — identities.toml "
                "never holds the secret itself."
            )


def _driver_keys(driver: str) -> frozenset[str] | None:
    """The driver-parameter keys `driver` actually accepts, read off its
    transport constructor — derived rather than listed so the fence cannot
    drift from the drivers it guards. None when a transport takes
    ``**kwargs`` and therefore constrains nothing.

    `identity` is excluded: get_transport() injects it, and a table key of
    that name would collide with the injected one.
    """
    import importlib
    import inspect

    module_path, class_name = DRIVERS[driver].split(":", 1)
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
        params = inspect.signature(cls.__init__).parameters
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return None
    return frozenset(
        n for n, p in params.items()
        if n not in ("self", "identity") and p.kind is not p.VAR_POSITIONAL
    )


def _fence_unknown_keys(name: str, table: dict, driver: str) -> None:
    """Allowlist the keys of one identity block: identity fields plus the
    driver's own parameters.

    A denylist of secret-looking NAMES cannot win — `smtp_password`,
    `pw`, `apikey` and every other spelling would sail past it and land on
    disk in plaintext. Inverting it means a key this codebase never reads
    is refused by construction, whatever it is called.
    """
    legal = _driver_keys(driver)
    if legal is None:
        return
    unknown = sorted(
        str(k) for k in table if str(k) not in KNOWN_FIELDS and str(k)
        not in legal
    )
    if unknown:
        raise SetupError(
            f"identity [{name}]: key(s) {unknown} are not read by the "
            f"{driver!r} driver — refusing to write settings nothing "
            "consumes (a misspelled secret key would sit there in "
            "plaintext forever). Legal keys: "
            f"{sorted(KNOWN_FIELDS | legal)}."
        )
    graph = table.get("graph")
    if isinstance(graph, dict):
        stray = sorted(str(k) for k in graph if str(k) not in _GRAPH_KEYS)
        if stray:
            raise SetupError(
                f"identity [{name}.graph]: key(s) {stray} are not part of "
                "the Graph app registration. Only "
                f"{sorted(_GRAPH_KEYS)} are read — the device-code flow "
                "is a PUBLIC client and has no client secret."
            )


def _validate_tables(tables: dict[str, dict], default: str) -> None:
    if not tables:
        raise SetupError("no identities to write.")
    for name, table in tables.items():
        if not _NAME_RE.fullmatch(str(name)):
            raise SetupError(
                f"invalid identity name {name!r} — must match "
                "^[A-Za-z0-9_-]{1,64}$. The name becomes "
                "graph/<name>.token.json, so this is a path fence, "
                "not cosmetics."
            )
        if not isinstance(table, dict):
            raise SetupError(f"identity [{name}] must be a table.")
        _fence_secret_keys(name, table)
        if not str(table.get("from_addr", "")).strip():
            raise SetupError(f"identity [{name}] needs `from_addr`.")
        for header_key in ("from_addr", "from_name"):
            value = str(table.get(header_key, ""))
            if _CTL_RE.search(value):
                raise SetupError(
                    f"identity [{name}]: control character (CR/LF/NUL) in "
                    f"`{header_key}` {value!r} — header values are "
                    "single-line (the compose fence would refuse every "
                    "send from this identity).")
        driver = str(table.get("driver", "")).strip()
        if driver not in DRIVERS:
            raise SetupError(
                f"identity [{name}] has missing or unknown driver "
                f"{driver!r}. Available: {sorted(DRIVERS)}")
        _fence_unknown_keys(name, table, driver)
        _fence_secret_refs(name, table)
        if driver == "smtp" and not (
                str(table.get("keychain", "")).strip()
                or str(table.get("op", "")).strip()):
            raise SetupError(
                f"identity [{name}]: the smtp driver needs a secret "
                "REFERENCE — set `keychain` (a Keychain item name; store "
                "the app password once with: security add-generic-password "
                "-s <item> -a <username> -w) or `op` (a 1Password op:// "
                "secret reference). The password itself is never stored "
                "here."
            )
    if default not in tables:
        raise SetupError(
            f"default identity {default!r} has no table. "
            f"Available: {sorted(tables)}")


def _render_identities(tables: dict[str, dict], default: str) -> str:
    lines = [f"default = {json.dumps(default)}", ""]
    for name, table in tables.items():
        lines.append(f"[{name}]")
        subtables: dict[str, dict] = {}
        for key, value in table.items():
            if isinstance(value, dict):
                subtables[key] = value
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        for key, sub in subtables.items():
            lines.append("")
            lines.append(f"[{name}.{key}]")
            for k, v in sub.items():
                lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_identities(
    tables: dict[str, dict],
    default: str,
    path: Path | None = None,
) -> tuple[Path, Path | None]:
    """Validate, serialize and atomically install identities.toml (0600).

    An existing file is backed up FIRST to
    ``identities.toml.bak-<UTCSTAMP>`` (mode preserved, collision-looped,
    never overwritten) — only then is the new content written to a tmp
    file, chmod 0600, and renamed over the target. Validation runs before
    the backup, so a refused write touches nothing at all.

    Returns ``(path, backup_path_or_None)``.
    """
    path = path or config.identities_file()
    _validate_tables(tables, default)
    text = _render_identities(tables, default)
    # Belt: whatever we wrote must parse back — never install a file the
    # loader would reject as malformed TOML.
    tomllib.loads(text)

    backup: Path | None = None
    if path.exists():
        stamp = ids.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
            n += 1
        shutil.copy2(path, backup)  # mode-preserved backup BEFORE clobber

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    _log.info("lifecycle: wrote %s (%d identity(ies), default %r)%s",
              path, len(tables), default,
              f", backup {backup.name}" if backup else "")
    return path, backup


def _identity_names(path: Path) -> list[str]:
    """Table names in an identities file; [] when absent/unparseable.
    Names only — never addresses — these feed the lifecycle event."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return sorted(k for k, v in data.items() if isinstance(v, dict))


# --------------------------------------------------------------------- #
# interactive helpers                                                    #
# --------------------------------------------------------------------- #


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = _input(f"  {prompt}{suffix}: ").strip()
    return raw or default


def _ask_required(prompt: str) -> str:
    while True:
        value = _ask(prompt)
        if value:
            return value
        print("  (required)")


def _ask_yn(prompt: str, default: bool = False) -> bool:
    raw = _input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip()
    if not raw:
        return default
    return raw.lower() in {"y", "yes"}


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        value = _ask(f"{prompt} ({'/'.join(choices)})", default)
        if value in choices:
            return value
        print(f"  (one of: {', '.join(choices)})")


# --------------------------------------------------------------------- #
# steps 1-3: preflight, permissions, state_dirs                          #
# --------------------------------------------------------------------- #


def _step_preflight(facts: dict) -> StepResult:
    if sys.platform != "darwin":
        return StepResult(
            "preflight", "blocked",
            f"email-mcp is macOS-only (sys.platform {sys.platform!r}) — "
            "it reads the Apple Mail store and scripts Mail.app.")
    mac_version = platform.mac_ver()[0] or "unknown"
    mail_app = next(
        (p for p in ("/System/Applications/Mail.app", "/Applications/Mail.app")
         if Path(p).exists()), None)
    mail_lib = Path.home() / "Library" / "Mail"
    meta = read_meta()
    if meta.get("state_version"):
        facts["mode"] = "review"
        print(f"  existing installation detected (state_version "
              f"{meta['state_version']}) — reviewing/repairing, not "
              "starting over.")
    bits = [
        f"macOS {mac_version}",
        f"Mail.app {'found' if mail_app else 'NOT found'}",
        f"~/Library/Mail {'present' if mail_lib.is_dir() else 'absent'}",
        f"mode {facts['mode']}",
    ]
    return StepResult("preflight", "ok", "; ".join(bits))


# (name, doctor check attr) — resolved via getattr at call time so tests
# can monkeypatch the doctor module's checks.
_PERMISSION_CHECKS = ("mail_store", "automation", "accessibility")


def _step_permissions(answers: Answers, interactive: bool) -> StepResult:
    skipped: list[str] = []
    for name in _PERMISSION_CHECKS:
        check_fn = getattr(doctor, f"check_{name}")
        while True:
            check = check_fn()
            if check.get("ok"):
                print(f"  ok   {name}: {check.get('detail', '')}")
                break
            print(f"  FAIL {name}: {check.get('detail', '')}")
            if check.get("fix"):
                print(f"       fix: {check['fix']}")
            if name == "mail_store":
                print(f"       note: {_FDA_RESTART_NOTE}")
            if not interactive:
                if answers.skip_permissions:
                    skipped.append(name)
                    break
                return StepResult(
                    "permissions", "blocked",
                    f"{name} check is red and this run is non-interactive "
                    "— grant the permission and re-run, or set "
                    '"skip_permissions": true in the answers file to '
                    "continue anyway.")
            choice = _input(
                "       Enter to retry, or s to skip this check: ").strip()
            if choice.lower() == "s":
                skipped.append(name)
                break
    if skipped:
        return StepResult("permissions", "ok",
                          "skipped red check(s): " + ", ".join(skipped)
                          + " — `email-mcp doctor` will keep reporting them")
    return StepResult("permissions", "ok",
                      "mail_store, automation, accessibility all green")


def _degenerate_overrides(paths: "StatePaths") -> list[str]:
    """EMAIL_MCP_* path overrides pointing at ``$HOME`` itself or any of
    its ancestors. config.spool_dir()/plans_dir()/graph_dir() mkdir the
    target AND ``chmod 0700`` its PARENT, so an override at ``$HOME``
    means chmodding ``/Users`` (and growing five spool subdirs in the
    home directory). Setup refuses instead — no state path may manage a
    directory that contains the user's home.

    Both sides go through ``os.path.realpath`` first. pathlib keeps
    ``..`` segments and a leading ``//`` verbatim, so a value compare on
    the raw paths sees ``$HOME/sub/..`` and ``//$HOME`` as ordinary
    directories while the filesystem sees ``$HOME`` — the fence has to
    compare what mkdir/chmod will actually act on."""
    home = Path(os.path.realpath(Path.home()))
    ancestors = {home, *home.parents}
    bad: list[str] = []
    if "EMAIL_MCP_STATE_DIR" not in paths.env_overrides:
        return bad
    resolved = Path(os.path.realpath(paths.root))
    if resolved in ancestors:
        bad.append(
            f"EMAIL_MCP_STATE_DIR={paths.env_overrides['EMAIL_MCP_STATE_DIR']}"
            f" -> {resolved}")
    return bad


def _step_state_dirs(facts: dict) -> StepResult:
    from . import spool

    paths = StatePaths.resolve()
    degenerate = _degenerate_overrides(paths)
    if degenerate:
        return StepResult(
            "state_dirs", "blocked",
            "refusing to build state under your home directory (or above "
            "it): " + "; ".join(degenerate) + " — creating the tree there "
            "would scatter spool subdirectories in it. Unset the variable "
            "or point it at a dedicated directory.")
    # Every other reason the root may not be managed (an override at a
    # directory that already holds someone else's files, a non-directory
    # squatting on it) comes from the resolver itself, so setup reports
    # exactly what the write path would refuse instead of crashing on it.
    refusal = config.state_root_refusal()
    if refusal:
        return StepResult("state_dirs", "blocked", refusal)
    # The config getters create the tree: 0700 for what they create, and
    # NOTHING for what already existed — setup does not re-mode a
    # directory the user (or another tool) made. `doctor` reports a wrong
    # mode and `doctor --fix` repairs it, on request. The FTS dir is
    # deliberately absent here: derived state, --build only.
    config.spool_dir()   # root + spool + its five state subdirectories
    config.plans_dir()
    config.graph_dir()
    config.audit_dir()

    meta = read_meta()
    now = ids.iso(ids.utcnow())
    meta.setdefault("created_at", now)
    meta["state_version"] = STATE_VERSION
    meta["updated_at"] = now
    meta["env_overrides"] = sorted(paths.env_overrides)
    try:
        from .cli import _package_version
        meta["package_version"] = _package_version()
    except Exception:  # never let version metadata block the write
        pass
    write_meta(meta)
    facts["state_ready"] = True

    detail = (f"state tree ready under {paths.root}; meta.json "
              f"state_version {STATE_VERSION}")
    if paths.env_overrides:
        detail += (" (env overrides in effect: "
                   + ", ".join(sorted(paths.env_overrides)) + ")")
    return StepResult("state_dirs", "ok", detail)


# --------------------------------------------------------------------- #
# step 4: identity                                                       #
# --------------------------------------------------------------------- #


def _security_hint(name: str, table: dict) -> None:
    """The exact command that stores the smtp app password — run by the
    USER, never by the wizard; the value never touches this process."""
    if str(table.get("driver", "")) != "smtp":
        return
    item = str(table.get("keychain", "")).strip()
    if not item:
        return
    account = str(table.get("username", "")).strip() or table.get("from_addr")
    print(f"  store the {name!r} app password once (the wizard never reads "
          "or stores it):")
    print(f"    security add-generic-password -s {item} -a {account} -w")


def _ask_identity_block(name: str = "") -> tuple[str, dict]:
    """The interactive question tree for one identity."""
    while True:
        name = _ask("identity name (becomes graph/<name>.token.json)",
                    name) or _ask_required(
                        "identity name (becomes graph/<name>.token.json)")
        if _NAME_RE.fullmatch(name):
            break
        print("  (must match ^[A-Za-z0-9_-]{1,64}$ — it is used as a "
              "filename)")
        name = ""
    table: dict = {"from_addr": _ask_required("From: address")}
    from_name = _ask("display name (optional)")
    if from_name:
        table["from_name"] = from_name
    driver = _ask_choice("transport driver", sorted(DRIVERS), "ssh_sendmail")
    table["driver"] = driver
    if driver == "ssh_sendmail":
        table["host"] = _ask_required("SSH host (e.g. lxplus.cern.ch)")
        table["user"] = _ask_required("SSH user")
        table["socket"] = _ask("ControlMaster socket path",
                               "~/.ssh/email-mcp-sock")
        bootstrap = _ask("bootstrap command (optional, re-opens the socket)")
        if bootstrap:
            table["bootstrap"] = bootstrap
        table["delivery_cmd"] = _ask("remote delivery command",
                                     "/usr/sbin/sendmail")
    elif driver == "smtp":
        table["host"] = _ask_required("SMTP host (e.g. smtp.gmail.com)")
        table["port"] = int(_ask("SMTP port", "587"))
        username = _ask("SMTP username", table["from_addr"])
        if username != table["from_addr"]:
            table["username"] = username
        source = _ask_choice("secret source", ["keychain", "op"], "keychain")
        while True:
            if source == "keychain":
                value = _ask("Keychain item name", f"email-mcp-{name}").strip()
            else:
                value = _ask_required(
                    "1Password secret reference (op://vault/item/field)"
                ).strip()
            problem = _reference_problem(source, value)
            if not problem:
                break
            # Re-ask here rather than let write_identities kill the run:
            # this prompt is precisely where a real app password gets
            # pasted, and the answer is never echoed back.
            print(f"  refused — {problem}")
            print("  (the answer is not repeated here in case it is the "
                  "password itself)")
        table[source] = value
    else:  # pipe
        table["command"] = _ask("delivery command",
                                "/usr/sbin/sendmail -t -i")
    return name, table


def _load_existing_tables(path: Path) -> tuple[dict[str, dict], str]:
    """Existing identities.toml → (tables, default). Malformed TOML is a
    SetupError: setup refuses to clobber a file it cannot merge."""
    if not path.is_file():
        return {}, ""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SetupError(
            f"cannot merge into {path}: {e} — fix or move the file aside, "
            "then re-run setup.") from e
    tables = {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
    default = data.get("default")
    return tables, default if isinstance(default, str) else ""


def _merge_answer_blocks(
    path: Path, blocks: list[dict], default_answer: str,
) -> tuple[dict[str, dict], str]:
    tables, existing_default = _load_existing_tables(path)
    first_new = ""
    for block in blocks:
        block = dict(block)
        name = str(block.pop("name", "")).strip()
        if not name:
            raise SetupError("every identity block needs a `name`.")
        first_new = first_new or name
        tables[name] = block
    default = default_answer or existing_default or first_new
    return tables, default


def _step_identity(
    answers: Answers, interactive: bool, facts: dict,
) -> StepResult:
    if answers.read_only or config.read_only():
        return StepResult(
            "identity", "skipped",
            "read-only mode — no sending identity needed (reads require "
            "no configuration at all)")
    path = config.identities_file()
    exists = path.is_file()
    facts["identities"] = _identity_names(path)

    if interactive:
        return _step_identity_interactive(path, exists, facts)

    action = (answers.identity_action if exists
              else ("add" if answers.identities else "keep"))
    if action == "keep":
        if exists:
            return StepResult(
                "identity", "ok",
                f"kept existing {path} "
                f"({', '.join(facts['identities']) or 'unparseable'})")
        return StepResult(
            "identity", "skipped",
            "no identities.toml and no identity answers — sending stays "
            "env-driven (EMAIL_MCP_FROM_ADDR) or disabled; reads work "
            "regardless")
    try:
        tables, default = _merge_answer_blocks(
            path, answers.identities, answers.default_identity)
        written, backup = write_identities(tables, default, path)
    except SetupError as e:
        return StepResult("identity", "blocked", str(e))
    for name in sorted(tables):
        _security_hint(name, tables[name])
    facts["identities"] = sorted(tables)
    detail = (f"wrote {written} ({', '.join(sorted(tables))}; "
              f"default {default!r})")
    if backup:
        detail += f"; previous file backed up to {backup.name}"
    return StepResult("identity", "ok", detail)


def _step_identity_interactive(
    path: Path, exists: bool, facts: dict,
) -> StepResult:
    if exists:
        names = facts["identities"]
        print(f"  identities.toml exists with: "
              f"{', '.join(names) or '(unparseable)'}")
        action = _ask_choice("keep, add or edit", ["keep", "add", "edit"],
                             "keep")
        if action == "keep":
            return StepResult("identity", "ok", f"kept existing {path}")
    else:
        if not _ask_yn("configure a sending identity now?", True):
            return StepResult(
                "identity", "skipped",
                "no identity configured — sending disabled until "
                "identities.toml exists (re-run setup any time)")
        action = "add"

    try:
        tables, default = _load_existing_tables(path)
        edit_name = ""
        if action == "edit":
            edit_name = _ask_choice(
                "which identity", sorted(tables) or [""],
                sorted(tables)[0] if tables else "")
        while True:
            name, table = _ask_identity_block(edit_name)
            tables[name] = table
            _security_hint(name, table)
            edit_name = ""
            if not _ask_yn("add another identity?", False):
                break
        if len(tables) > 1:
            default = _ask_choice("default identity", sorted(tables),
                                  default if default in tables
                                  else sorted(tables)[0])
        else:
            default = next(iter(tables))
        written, backup = write_identities(tables, default, path)
    except SetupError as e:
        return StepResult("identity", "blocked", str(e))
    facts["identities"] = sorted(tables)
    detail = (f"wrote {written} ({', '.join(sorted(tables))}; "
              f"default {default!r})")
    if backup:
        detail += f"; previous file backed up to {backup.name}"
    return StepResult("identity", "ok", detail)


# --------------------------------------------------------------------- #
# steps 5-6: launchd agents, fts index                                   #
# --------------------------------------------------------------------- #


def _step_launchd(
    answers: Answers, interactive: bool, facts: dict,
) -> StepResult:
    from . import dispatcher, fts

    agents = (
        ("dispatcher", dispatcher.LAUNCHD_LABEL,
         "scheduled-send dispatcher (runs every 60s)",
         dispatcher.install_launchd, answers.install_dispatcher),
        ("fts", fts.LAUNCHD_LABEL,
         "nightly FTS body-index sync (03:30)",
         fts.install_launchd, answers.install_fts_agent),
    )
    installed: list[str] = []
    failures: list[str] = []
    notes: list[str] = []
    for name, label, blurb, install, wanted in agents:
        if name == "dispatcher" and (answers.read_only or config.read_only()):
            notes.append(f"{label} skipped (read-only mode never sends)")
            continue
        if interactive:
            wanted = _ask_yn(f"install the {blurb} launchd agent?",
                             default=False)
        if not wanted:
            notes.append(f"{label} not installed")
            continue
        try:
            message = install()
        except OSError as e:
            # launchctl (or the plist dir) unavailable: a degraded step,
            # never a crashed wizard — the run continues past launchd.
            _log.warning("setup: %s install failed: %s", label, e)
            failures.append(f"{label}: install failed: {e}")
            print(f"  {label}: install failed: {e}")
            continue
        print(f"  {message}")
        if "bootstrap failed" in message:
            failures.append(f"{label}: {message}")
        else:
            installed.append(label)
    facts["agents"] = installed
    detail = "installed: " + (", ".join(installed) or "none")
    if notes:
        detail += "; " + "; ".join(notes)
    if failures:
        return StepResult("launchd", "failed",
                          detail + "; " + "; ".join(failures))
    return StepResult("launchd", "ok", detail)


_FTS_WARNING = (
    "a full first build reads every message body — on a large mailbox "
    "this can run from many minutes to hours (resumable; Ctrl-C is safe)"
)


def _step_fts(answers: Answers, interactive: bool, facts: dict) -> StepResult:
    from . import fts

    if interactive:
        print(f"  full-text body index — note: {_FTS_WARNING}")
        print("    full:  build everything now")
        print(f"    quick: index the newest {answers.fts_limit} now, "
              "the rest later")
        print("    defer: build later with `email-mcp fts --build`")
        print("    skip:  no body index (EMAIL_MCP_FTS_ENABLED=0 to "
              "silence search's fallback note)")
        choice = _ask_choice("index choice",
                             ["full", "quick", "defer", "skip"], "defer")
    else:
        choice = answers.fts_choice
    facts["fts_choice"] = choice

    if choice == "full":
        print(f"  building ({_FTS_WARNING})…")
        rc = fts.main(["--build"])
        if rc != 0:
            return StepResult(
                "fts", "failed",
                f"full build failed (exit {rc}) — run `email-mcp fts "
                "--build` once the doctor is green")
        return StepResult("fts", "ok", "full build completed")
    if choice == "quick":
        rc = fts.main(["--build", "--limit", str(answers.fts_limit)])
        if rc != 0:
            return StepResult(
                "fts", "failed",
                f"quick build failed (exit {rc}) — run `email-mcp fts "
                "--build` once the doctor is green")
        return StepResult(
            "fts", "ok",
            f"quick start: newest {answers.fts_limit} indexed; finish "
            "later with `email-mcp fts --build` (or the nightly agent)")
    if choice == "skip":
        return StepResult(
            "fts", "skipped",
            "no body index — search falls back to subjects/snippets; "
            "set EMAIL_MCP_FTS_ENABLED=0 to make that explicit")
    return StepResult(
        "fts", "skipped",
        "deferred — build any time with `email-mcp fts --build` "
        f"({_FTS_WARNING})")


# --------------------------------------------------------------------- #
# steps 7-8: client config, smoke                                        #
# --------------------------------------------------------------------- #


def _entry_point() -> tuple[str, list[str]]:
    """The ABSOLUTE path MCP clients should launch. The console script
    next to the running interpreter wins (the venv that owns this
    install); PATH lookup is the fallback; `python -m email_mcp.cli`
    covers a scriptless checkout."""
    # NOT .resolve() first: sys.executable in a venv is a symlink to the
    # base interpreter, and resolving would leave the venv's bin/ where
    # the console script actually lives.
    script = Path(sys.executable).parent / "email-mcp"
    if script.is_file() and os.access(script, os.X_OK):
        return str(script), []
    found = shutil.which("email-mcp")
    if found:
        return str(Path(found).resolve()), []
    return sys.executable, ["-m", "email_mcp.cli"]


def _step_client_config() -> StepResult:
    command, args = _entry_point()
    spec: dict = {"command": command}
    if args:
        spec["args"] = args
    block = {"mcpServers": {"apple-mail": dict(spec)}}
    read_only = {"mcpServers": {"apple-mail": {
        **spec, "env": {"EMAIL_MCP_READ_ONLY": "1"}}}}
    print("  paste into your MCP client's config (e.g. ~/.claude.json):")
    print(json.dumps(block, indent=2))
    print("  read-only variant (only the 11 read-side tools register):")
    print(json.dumps(read_only, indent=2))
    return StepResult("client_config", "ok", f"entry point: {command}")


def _step_smoke(answers: Answers, interactive: bool) -> StepResult:
    from .cli import _render_doctor

    report = doctor.run()
    _render_doctor(report)
    verdict = "doctor green" if report.get("ok") else (
        "doctor RED — see the FAIL lines above; `email-mcp doctor --fix` "
        "handles the safe ones")

    if answers.read_only or config.read_only():
        return StepResult("smoke", "ok", verdict + "; self-send n/a "
                                                   "(read-only)")
    want_send = (answers.self_send if not interactive
                 else _ask_yn("send a self-test email to your own address?",
                              False))
    if not want_send:
        return StepResult("smoke", "ok", verdict)
    try:
        from . import identities, sender
        ident = identities.get(None)
        result = sender.send_email(
            to=ident.from_addr,
            subject="email-mcp setup self-test",
            body="This is the email-mcp setup smoke test. If you can read "
                 "this in your inbox, sending works end to end.",
        )
        print(f"  self-send ok: message_id {result.message_id}")
        return StepResult("smoke", "ok",
                          verdict + f"; self-send ok ({result.message_id})")
    except SendError as e:
        print(f"  self-send failed: {e}")
        return StepResult("smoke", "failed", verdict + f"; self-send "
                                                       f"failed: {e}")


# --------------------------------------------------------------------- #
# the run                                                                #
# --------------------------------------------------------------------- #

_STATUS_TAGS = {"ok": "ok", "skipped": "skip", "failed": "FAIL",
                "blocked": "BLOCKED"}


def run_setup(answers: Answers, interactive: bool) -> list[StepResult]:
    """Run the eight setup steps in order; stop at the first ``blocked``
    result. Emits ONE ``lifecycle`` audit event when the run touched
    state (identity NAMES, agent labels and the fts choice only — no
    addresses, no secrets)."""
    audit.set_process("cli")
    facts: dict = {"mode": "setup", "identities": [], "agents": [],
                   "fts_choice": None}
    steps: tuple[tuple[str, Callable[[], StepResult]], ...] = (
        ("preflight", lambda: _step_preflight(facts)),
        ("permissions", lambda: _step_permissions(answers, interactive)),
        ("state_dirs", lambda: _step_state_dirs(facts)),
        ("identity", lambda: _step_identity(answers, interactive, facts)),
        ("launchd", lambda: _step_launchd(answers, interactive, facts)),
        ("fts", lambda: _step_fts(answers, interactive, facts)),
        ("client_config", lambda: _step_client_config()),
        ("smoke", lambda: _step_smoke(answers, interactive)),
    )
    results: list[StepResult] = []
    for i, (name, fn) in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {name}")
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 — the wizard must never
            # dump a traceback as its UX; the log gets the full story
            _log.exception("setup step %s crashed", name)
            result = StepResult(name, "blocked",
                                f"step crashed: {e!r} (details in the log)")
        results.append(result)
        print(f"  {_STATUS_TAGS[result.status]:7s} {result.detail}")
        if result.status == "blocked":
            break

    blocked = results[-1].status == "blocked" if results else False
    if facts.get("state_ready"):
        detail = {"mode": facts["mode"], "identities": facts["identities"],
                  "agents": facts["agents"],
                  "fts_choice": facts["fts_choice"]}
        if blocked:
            detail["blocked"] = results[-1].step
        audit.emit("lifecycle", outcome="setup", detail=detail)
    print(f"setup {'blocked at ' + results[-1].step if blocked else 'complete'}"
          f" — {sum(1 for r in results if r.status == 'ok')} ok, "
          f"{sum(1 for r in results if r.status == 'skipped')} skipped, "
          f"{sum(1 for r in results if r.status == 'failed')} failed"
          + (", 1 blocked" if blocked else ""))
    return results


def run_setup_cli(argv: list[str] | None = None) -> int:
    """``email-mcp setup [--yes] [--answers FILE] [--read-only]`` →
    exit 0 on a completed run, 1 when a step blocked, 2 on usage errors
    (including a non-interactive invocation without --yes/--answers)."""
    parser = argparse.ArgumentParser(
        prog="email-mcp setup",
        description="Guided install: permissions, state tree, identities, "
                    "launchd agents, FTS index, client config, smoke test.",
    )
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive; accept the safe defaults "
                             "(keep identities, install nothing, defer FTS)")
    parser.add_argument("--answers", metavar="FILE",
                        help="non-interactive; scripted answers from a "
                             "JSON file")
    parser.add_argument("--read-only", action="store_true",
                        help="set up the read-only surface — the identity "
                             "step is skipped entirely")
    args = parser.parse_args(argv)

    if args.answers:
        try:
            answers = Answers.from_file(args.answers)
        except (OSError, ValueError) as e:
            print(f"email-mcp setup: cannot load answers file: {e}",
                  file=sys.stderr)
            return 2
    else:
        answers = Answers()
    if args.read_only:
        answers.read_only = True

    interactive = not (args.yes or args.answers)
    if interactive:
        try:
            has_tty = sys.stdin is not None and sys.stdin.isatty()
        except (AttributeError, ValueError):
            has_tty = False
        if not has_tty:
            # This refusal keeps the stub's frozen contract
            # (tests/test_cli.py::test_lifecycle_stubs_exit_2_with_pointer):
            # exit 2, silent stdout, "setup" + the "L3" stage pointer on
            # stderr — the wizard landed in L3, the pointer stays honest.
            print("email-mcp setup: the interactive wizard (L3 of the "
                  "v0.11 lifecycle work) needs a terminal — rerun in "
                  "one, or script it with --yes or --answers FILE.",
                  file=sys.stderr)
            return 2

    results = run_setup(answers, interactive=interactive)
    return 1 if any(r.status == "blocked" for r in results) else 0


# --------------------------------------------------------------------- #
# update (L4): migrations → plist drift → meta stamp → doctor            #
# --------------------------------------------------------------------- #

_RELEASES_URL = "https://github.com/parasxos/email-mcp/releases"


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def meta_state_version(meta: dict) -> int:
    """The recorded state schema version; 0 when absent or corrupt (the
    cli._state_version tolerance)."""
    value = meta.get("state_version", 0)
    return value if isinstance(value, int) else 0


def pending_migrations(
    from_version: int,
) -> tuple[tuple[int, Callable[[dict], dict]], ...]:
    """The (target, fn) steps update must run, ascending target order:
    everything past the recorded version, up to and including
    STATE_VERSION (a target beyond it belongs to a newer package)."""
    return tuple(sorted(
        ((t, fn) for t, fn in MIGRATIONS
         if from_version < t <= STATE_VERSION),
        key=lambda pair: pair[0]))


def plist_current(render: str, path: Path) -> bool:
    """True when the installed plist at ``path`` matches the fresh
    ``render``; False on any read trouble — an unreadable plist needs
    the re-render just as much as a stale one."""
    try:
        return path.read_text() == render
    except OSError:
        return False


def _refresh_agents() -> tuple[list[str], list[str], list[str]]:
    """Re-render + re-bootstrap every INSTALLED agent whose plist differs
    from a fresh render — a moved venv bakes a dead ``sys.executable``
    into ProgramArguments and only a re-render heals it. Returns
    ``(refreshed, current, failed)`` label lists. An absent plist means
    the agent was never installed: update installs NOTHING (that is
    setup's decision — the repairs registry's rule). render() is only
    called past the exists() check, so an uninstalled FTS agent never
    costs the fts dir its never-created-here guarantee."""
    from . import dispatcher, fts

    refreshed: list[str] = []
    current: list[str] = []
    failed: list[str] = []
    agents = (
        (dispatcher.LAUNCHD_LABEL, dispatcher._plist_path,
         dispatcher._plist_content, dispatcher.install_launchd),
        (fts.LAUNCHD_LABEL, fts._plist_path,
         fts._plist_content, fts.install_launchd),
    )
    for label, plist_path, render, install in agents:
        path = plist_path()
        if not path.exists():
            print(f"  agent {label}: not installed — left that way")
            continue
        if plist_current(render(), path):
            print(f"  agent {label}: plist current")
            current.append(label)
            continue
        try:
            message = install()  # the module's own re-render+bootout+bootstrap
        except OSError as e:
            # launchctl unavailable: report the agent as failed, never
            # dump a traceback out of `email-mcp update`.
            _log.warning("update: %s re-install failed: %s", label, e)
            print(f"  agent {label}: stale plist — re-install failed: {e}")
            failed.append(label)
            continue
        print(f"  agent {label}: stale plist — {message}")
        _log.info("update: %s stale plist — %s", label, message)
        if "bootstrap failed" in message:
            failed.append(label)
        else:
            refreshed.append(label)
    return refreshed, current, failed


def _stamp_meta(meta: dict) -> dict:
    """The update stamp: created_at kept (or adopted), updated_at now,
    env overrides recorded, package_version refreshed."""
    now = ids.iso(ids.utcnow())
    meta.setdefault("created_at", now)
    meta["updated_at"] = now
    meta["env_overrides"] = sorted(StatePaths.resolve().env_overrides)
    try:
        from .cli import _package_version
        meta["package_version"] = _package_version()
    except Exception:  # never let version metadata block the write
        pass
    return meta


def run_update() -> int:
    """Run pending migrations in order, heal plist drift on installed
    agents, stamp meta.json, run the doctor, point at the releases page.
    Idempotent — a second run reports nothing pending and changes only
    ``updated_at``. ONE ``lifecycle`` audit event (outcome ``update``)
    per run; exit 1 when a migration or an agent re-bootstrap failed."""
    audit.set_process("cli")
    print("email-mcp update")
    meta = read_meta()
    before = meta_state_version(meta)
    ran: list[str] = []
    failed_migration: dict | None = None
    for target, fn in pending_migrations(before):
        name = getattr(fn, "__name__", repr(fn))
        print(f"  migration -> state_version {target}: {name}")
        _log.info("update: migration -> state_version %d (%s)",
                  target, name)
        try:
            migrated = fn(meta)
            if not isinstance(migrated, dict):
                raise TypeError(
                    f"migration returned {type(migrated).__name__}, "
                    "not the meta dict")
        except Exception as e:  # noqa: BLE001 — a broken migration must
            # report and stop cleanly, never dump a traceback as UX
            _log.exception("update: migration -> %d failed", target)
            print(f"  migration -> state_version {target} FAILED: {e!r} "
                  "(details in the log) — stopping here; state_version "
                  f"stays {meta_state_version(meta)}")
            failed_migration = {"target": target, "migration": name}
            break
        meta = migrated
        meta["state_version"] = target
        ran.append(name)

    if failed_migration is not None:
        # Progress up to the failure is durable: the completed steps'
        # work is on disk and their state_version is recorded, so the
        # re-run resumes at the failed step, never repeats one.
        write_meta(_stamp_meta(meta))
        audit.emit("lifecycle", outcome="update", detail={
            "from_state_version": before,
            "state_version": meta_state_version(meta),
            "migrations": ran,
            "failed_migration": failed_migration,
        })
        return 1

    if not ran:
        if before == STATE_VERSION:
            print(f"  state_version {before}: current — no migrations "
                  "pending")
        elif before < STATE_VERSION:
            # The v0.9/v0.10 adoption path: real state, no meta.json yet.
            print(f"  state_version {before} -> {STATE_VERSION} (no "
                  "migration steps registered for this range)")
    if before > STATE_VERSION:
        print(f"  state_version {before} is newer than this package's "
              f"{STATE_VERSION} — left untouched; upgrade the package")
    else:
        meta["state_version"] = STATE_VERSION

    refreshed, current, failed_agents = _refresh_agents()

    write_meta(_stamp_meta(meta))
    print(f"  meta.json: state_version {meta['state_version']}, "
          f"package_version {meta.get('package_version', 'unknown')}")

    from .cli import _render_doctor
    report = doctor.run()
    _render_doctor(report)
    print(f"release notes: {_RELEASES_URL}")

    audit.emit("lifecycle", outcome="update", detail={
        "from_state_version": before,
        "state_version": meta_state_version(meta),
        "migrations": ran,
        "agents_refreshed": refreshed,
        "agents_current": current,
        "agents_failed": failed_agents,
        "package_version": meta.get("package_version"),
    })
    return 1 if failed_agents else 0


def run_update_cli(argv: list[str] | None = None) -> int:
    """``email-mcp update [--yes]`` → exit 0 on a clean run, 1 when a
    migration or agent re-bootstrap failed, 2 on usage errors (including
    a non-terminal invocation without --yes)."""
    parser = argparse.ArgumentParser(
        prog="email-mcp update",
        description="Post-upgrade housekeeping: run pending state "
                    "migrations, re-render stale launchd plists, stamp "
                    "meta.json, run the doctor.",
    )
    parser.add_argument("--yes", action="store_true",
                        help="run without a terminal (update rewrites "
                             "meta.json and re-bootstraps launchd "
                             "agents, so scripts must opt in)")
    args = parser.parse_args(argv)
    if not args.yes and not _stdin_is_tty():
        # This refusal keeps the stub's frozen contract
        # (tests/test_cli.py::test_lifecycle_stubs_exit_2_with_pointer):
        # exit 2, silent stdout, "update" + the "L4" stage pointer on
        # stderr — update mutates launchd + meta, so a non-terminal run
        # must opt in explicitly.
        print("email-mcp update: refusing to run outside a terminal "
              "without --yes (L4 of the v0.11 lifecycle work) — rerun "
              "in one, or pass --yes.", file=sys.stderr)
        return 2
    return run_update()


# --------------------------------------------------------------------- #
# uninstall (L4): agents + token caches out; state only with --purge     #
# --------------------------------------------------------------------- #


class PurgeRefused(Exception):
    """A purge fence tripped — NOTHING was removed. The fences are
    deliberate paranoia (design D7): purge only ever removes the
    HARDCODED ``~/.email-mcp`` and refuses anything that smells like a
    redirection (symlinked root, degenerate ``$HOME``, a squatting
    file)."""


def purge_state() -> Path | None:
    """Remove the state tree — the HARDCODED ``Path.home()/.email-mcp``
    and nothing else, ever (design D7). Env-overridden dirs are NEVER
    removed here; callers print them instead.

    Fences, each raising :class:`PurgeRefused` with nothing removed:
    ``Path.home()`` must be absolute and must not realpath to ``/``, and
    the root's direct parent; the root must not be a symlink (``os.lstat``
    — the link is never followed) and must be a real directory. Removal
    is ``shutil.rmtree``, whose macOS implementation is fd-based and
    does not follow directory symlinks inside the tree. Returns the
    removed root, or None when it was already absent (idempotent).

    Unlike a tripped fence, an undeletable entry INSIDE the tree raises
    OSError with the tree partly removed; callers report that as an
    incomplete uninstall, never as a traceback."""
    raw_home = Path.home()
    # Absoluteness is judged on the RAW value: realpath would silently
    # anchor a relative $HOME to the cwd and hand it back looking fine.
    if not raw_home.is_absolute():
        raise PurgeRefused(
            f"$HOME resolves to {str(raw_home)!r} — refusing to purge")
    # Only then realpath, because the "/" fence below is a value compare
    # and pathlib preserves ".." and a leading "//": HOME=/.. reads as a
    # directory under root, and HOME=$X/sub/.. would aim the rmtree one
    # level above the string it was derived from.
    home = Path(os.path.realpath(raw_home))
    root = home / ".email-mcp"  # HARDCODED: env overrides never move this
    if home == Path("/"):
        raise PurgeRefused(
            f"$HOME resolves to {str(home)!r} — refusing to purge")
    if root.parent != home:  # unreachable by construction; belt anyway
        raise PurgeRefused(
            f"{root} is not directly under {home} — refusing to purge")
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return None  # already gone — purge is idempotent
    if stat.S_ISLNK(st.st_mode):
        raise PurgeRefused(
            f"{root} is a symlink (os.lstat) — refusing to follow it; "
            "remove the link and its target yourself if that is intended")
    if not stat.S_ISDIR(st.st_mode):
        raise PurgeRefused(
            f"{root} is not a directory — refusing; move the file aside "
            "yourself")
    shutil.rmtree(root)
    _log.info("uninstall: purged %s", root)
    return root


def _logs_dir() -> Path:
    return Path.home() / "Library" / "Logs"


def _purge_logs() -> list[Path]:
    """Unlink ``~/Library/Logs/email-mcp*.log`` (config.log_file's
    default home). unlink never recurses, so a symlinked log file costs
    only the link itself — but a symlinked Logs DIRECTORY is a
    redirection, and the D7 rule the graph token sweep applies holds
    here too: refuse, never glob through it."""
    removed: list[Path] = []
    logs = _logs_dir()
    if logs.is_symlink():
        return removed
    for path in sorted(logs.glob("email-mcp*.log")):
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def uninstall_plan(purge: bool) -> dict:
    """Side-effect-free preview of ``email-mcp uninstall``:
    ``{remove, keep, print_only, env_overrides}`` — remove/keep/
    print_only are one-line strings, env_overrides the live EMAIL_MCP_*
    path map. Everything slated for removal lives under the HARDCODED
    home paths; anything env-overridden lands in print_only — uninstall
    reports it and leaves it alone (design D7)."""
    from . import dispatcher, fts

    home = Path.home()
    root = home / ".email-mcp"
    agents_dir = home / "Library" / "LaunchAgents"
    paths = StatePaths.resolve()
    remove: list[str] = []
    keep: list[str] = []
    print_only: list[str] = []

    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                  *dispatcher.LEGACY_LABELS):
        plist = agents_dir / f"{label}.plist"
        if plist.exists():
            remove.append(f"launchd agent {label} ({plist})")
    graph_default = root / "graph"
    if graph_default.is_symlink():
        print_only.append(
            f"{graph_default} (symlink — token caches behind it are "
            "never followed; delete them yourself)")
    elif config.is_dir_safe(graph_default):
        for token in sorted(graph_default.glob("*.token.json")):
            remove.append(str(token))
    if paths.graph != graph_default and config.is_dir_safe(paths.graph):
        for token in sorted(paths.graph.glob("*.token.json")):
            print_only.append(
                f"{token} (EMAIL_MCP_STATE_DIR override — never removed; "
                "delete it yourself)")

    if purge:
        remove.append(f"{root} (state tree: spool, plans, audit ledger, "
                      "identities.toml, meta.json)")
        logs_dir = _logs_dir()
        if logs_dir.is_symlink():
            print_only.append(
                f"{logs_dir} (symlink — logs behind it are never "
                "followed; delete them yourself)")
        else:
            for log in sorted(logs_dir.glob("email-mcp*.log")):
                remove.append(str(log))
        for var, raw in sorted(paths.env_overrides.items()):
            print_only.append(
                f"{var}={raw} (env-overridden path — never removed; "
                "delete it yourself)")
        keep.append(f"{home / 'Library' / 'Mail'} — never touched")
        keep.append("macOS Keychain items — never touched (delete "
                    "commands are printed at removal time)")
        keep.append("1Password entries — never touched")
    else:
        keep.append(f"{root} (spool, plans, audit ledger, "
                    "identities.toml, meta.json — remove with "
                    "`email-mcp uninstall --purge`)")
        keep.append(f"{home / 'Library' / 'Logs'}/email-mcp*.log "
                    "(--purge removes them)")
        keep.append(f"{home / 'Library' / 'Mail'} — never touched")
    return {"remove": remove, "keep": keep, "print_only": print_only,
            "env_overrides": dict(paths.env_overrides)}


def keychain_instructions(identities_doc: dict) -> list[str]:
    """Print (and return) the exact ``security delete-generic-password``
    commands for every Keychain item the identities document references
    — run by the USER; uninstall itself never touches the Keychain, and
    1Password entries are never touched either. The caller parses
    identities.toml BEFORE any deletion — with --purge the file is gone
    moments later."""
    commands: list[str] = []
    has_op = False
    for name in sorted(identities_doc):
        table = identities_doc[name]
        if not isinstance(table, dict):
            continue
        if str(table.get("op", "")).strip():
            has_op = True
        item = str(table.get("keychain", "")).strip()
        if not item:
            continue
        account = (str(table.get("username", "")).strip()
                   or str(table.get("from_addr", "")).strip())
        cmd = f"security delete-generic-password -s {item}"
        if account:
            cmd += f" -a {account}"
        commands.append(cmd)
    lines: list[str] = []
    if commands:
        lines.append("  Keychain items referenced by identities.toml are "
                     "never deleted by uninstall — remove each yourself "
                     "if wanted:")
        lines += [f"    {c}" for c in commands]
    if has_op:
        lines.append("  1Password entries (op:// references) are never "
                     "touched — nothing to clean up there.")
    for line in lines:
        print(line)
    return lines


def run_uninstall(purge: bool, assume_yes: bool) -> int:
    """Plan → typed confirmation → keychain instructions (identities
    parsed FIRST) → ONE ``lifecycle`` audit event, emitted BEFORE any
    removal (with --purge those are the ledger's last words — hence the
    receipts-export hint in the plan) → both agents out (legacy labels
    included) → token caches deleted → with --purge the fenced
    :func:`purge_state` plus the log files.

    Exit 1 when anything the plan listed under ``remove:`` survived the
    run: a left-behind agent keeps RUNNING, so "complete" would be a
    lie. The failures are named on stderr."""
    audit.set_process("cli")
    from . import dispatcher, fts

    plan = uninstall_plan(purge)
    print("email-mcp uninstall" + (" --purge" if purge else ""))
    for line in plan["remove"]:
        print(f"  remove: {line}")
    if not plan["remove"]:
        print("  remove: nothing found (no agents, no token caches"
              + (", no state tree" if purge else "") + ")")
    for line in plan["print_only"]:
        print(f"  left alone: {line}")
    for line in plan["keep"]:
        print(f"  kept: {line}")
    if purge:
        print("  NOTE: --purge deletes the audit ledger with the rest "
              "of the state tree. Export your receipts first if you "
              "want them:")
        print("    email-mcp audit --since 1970-01-01 > receipts.jsonl")

    if not assume_yes:
        word = "purge" if purge else "uninstall"
        typed = _input(f'  type "{word}" to confirm: ').strip()
        if typed != word:
            print("aborted — nothing removed.")
            return 1

    # Parse identities BEFORE anything is deleted: the delete commands
    # are derived from a file --purge is about to remove.
    identities_doc: dict = {}
    try:
        with config.identities_file().open("rb") as f:
            identities_doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        identities_doc = {}
    keychain_instructions(identities_doc)

    # The ledger's record of this run, BEFORE removal — with --purge the
    # event itself is destroyed moments later; that is the documented
    # deal, and why the receipts hint above prints first.
    audit.emit("lifecycle", outcome="uninstall", detail={
        "purge": purge,
        "remove": plan["remove"],
        "print_only": plan["print_only"],
    })

    # What we PROMISED to remove but could not: the run is not a success
    # just because it survived — an agent left behind keeps running.
    left_behind: list[str] = []

    for label, agent_uninstall in (
            (dispatcher.LAUNCHD_LABEL, dispatcher.uninstall_launchd),
            (fts.LAUNCHD_LABEL, fts.uninstall_launchd)):
        try:
            print(f"  {agent_uninstall()}")
        except OSError as e:
            # launchctl unavailable must not abort the rest of the
            # uninstall (tokens, purge) with a traceback.
            _log.warning("uninstall: launchd removal failed: %s", e)
            print(f"  launchd agent removal failed ({e}) — remove the "
                  "plists under ~/Library/LaunchAgents yourself",
                  file=sys.stderr)
            left_behind.append(f"launchd agent {label} ({e})")

    graph_default = Path.home() / ".email-mcp" / "graph"
    if graph_default.is_symlink():
        # Deleting *.token.json THROUGH a link would reach whatever
        # directory the link points at — the D7 rule (never follow a
        # redirection) applies to the token sweep too.
        print(f"  left alone: {graph_default} is a symlink — token "
              "caches behind it are never followed; delete them yourself")
    elif graph_default.is_dir():
        for token in sorted(graph_default.glob("*.token.json")):
            try:
                token.unlink()
            except OSError as e:
                print(f"  could not remove {token}: {e}", file=sys.stderr)
                left_behind.append(f"{token} ({e})")
                continue
            print(f"  removed {token}")

    if purge:
        root_path = Path.home() / ".email-mcp"
        try:
            root = purge_state()
        except PurgeRefused as e:
            print(f"email-mcp uninstall: purge refused: {e}",
                  file=sys.stderr)
            return 1
        except OSError as e:
            # A fenced rmtree that runs into an undeletable subtree
            # leaves a HALF-removed state tree — the one outcome the
            # user must be told about, and never as a traceback.
            _log.warning("uninstall: purge failed partway: %s", e)
            print(f"email-mcp uninstall: purge incomplete ({e}) — "
                  f"{root_path} is partly removed; delete the rest "
                  "yourself", file=sys.stderr)
            left_behind.append(f"{root_path} (partly removed: {e})")
        else:
            if root is None:
                print(f"  {root_path} already absent")
            else:
                print(f"  removed {root}")
        for log_path in _purge_logs():
            print(f"  removed {log_path}")
    if left_behind:
        print("uninstall INCOMPLETE — still present: "
              + "; ".join(left_behind), file=sys.stderr)
        return 1
    print("uninstall complete." + ("" if purge else
          " State kept — `email-mcp uninstall --purge` removes it."))
    return 0


def run_uninstall_cli(argv: list[str] | None = None) -> int:
    """``email-mcp uninstall [--purge] [--yes]`` → exit 0 on completion,
    1 when aborted, refused, or left incomplete, 2 on usage errors
    (including no terminal for the typed confirmation without --yes)."""
    parser = argparse.ArgumentParser(
        prog="email-mcp uninstall",
        description="Remove the launchd agents and Graph token caches; "
                    "state stays unless --purge. ~/Library/Mail is "
                    "never touched.",
    )
    parser.add_argument("--purge", action="store_true",
                        help="also remove ~/.email-mcp (spool, plans, "
                             "audit ledger, identities.toml, meta.json) "
                             "and ~/Library/Logs/email-mcp*.log")
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation")
    args = parser.parse_args(argv)
    if not args.yes and not _stdin_is_tty():
        # This refusal keeps the stub's frozen contract
        # (tests/test_cli.py::test_lifecycle_stubs_exit_2_with_pointer):
        # exit 2, silent stdout, "uninstall" + the "L4" stage pointer on
        # stderr.
        print("email-mcp uninstall: the typed confirmation (L4 of the "
              "v0.11 lifecycle work) needs a terminal — rerun in one, "
              "or pass --yes.", file=sys.stderr)
        return 2
    return run_uninstall(purge=args.purge, assume_yes=args.yes)
