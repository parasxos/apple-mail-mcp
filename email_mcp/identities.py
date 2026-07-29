"""Identity routing: the From: address decides the transport.

`~/.email-mcp/identities.toml` (override: EMAIL_MCP_IDENTITIES) maps
identity names to a From address, a transport driver, and that driver's
parameters. Everything an identity implies follows from it — From: header,
Message-ID domain, Bcc-to-self target, per-identity allowlist, driver.

Absent file → a single identity named "default" synthesized from the env
getters in `config.py` (requires EMAIL_MCP_FROM_ADDR), so an env-only
setup keeps working without a file.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from . import config
from .transports import DRIVERS, SendError

# TOML keys that map to Identity fields; everything else in a block is a
# driver parameter and lands in `params`.
_KNOWN_FIELDS = {
    "from_addr", "from_name", "driver", "allowlist", "allow_all", "bcc_self",
}


class IdentityError(SendError):
    """A caller-fixable problem with the identities file (or an unknown
    identity name). Subclasses SendError so every existing
    `except SendError` handler catches it for free."""


@dataclass(frozen=True)
class Identity:
    name: str
    from_addr: str
    from_name: str = ""
    driver: str = "ssh_sendmail"
    params: dict = field(default_factory=dict)
    allowlist: list[str] = field(default_factory=list)
    allow_all: bool = False
    bcc_self: bool = True

    def __post_init__(self) -> None:
        # Each identity's "self" is its own address: an empty allowlist
        # means self-only, never allow-nothing.
        if not self.allowlist:
            object.__setattr__(
                self, "allowlist", [self.from_addr.strip().lower()]
            )


def _synthesized() -> Identity:
    """No identities file → mirror the env-driven config exactly, so an
    env-only setup works with zero files. With neither a file nor
    EMAIL_MCP_FROM_ADDR there is no sending identity at all — fail here
    with the remedy instead of cascading empty-From errors downstream."""
    from_addr = config.send_from_addr()
    if not from_addr:
        raise IdentityError(
            "no sending identity configured — create "
            "~/.email-mcp/identities.toml (see docs/reference.md, "
            "'Identities & transports') or set EMAIL_MCP_FROM_ADDR."
        )
    return Identity(
        name="default",
        from_addr=from_addr,
        from_name=config.send_from_name(),
        driver="ssh_sendmail",
        params={
            "host": config.send_host(),
            "user": config.send_user(),
            "socket": str(config.send_ssh_socket()),
            "bootstrap": config.send_bootstrap_cmd(),
            "delivery_cmd": config.send_delivery_cmd(),
        },
        allowlist=sorted(config.send_allowlist()),
        allow_all=config.send_allow_all(),
        bcc_self=config.send_bcc_self(),
    )


def load() -> tuple[dict[str, Identity], str]:
    """Parse the identities file → ({name: Identity}, default_name).

    Deliberately uncached: the file is tiny and late binding matches the
    env getters — an edit takes effect on the next send, no restart.
    """
    path = config.identities_file()
    if not path.exists():
        ident = _synthesized()
        return {ident.name: ident}, ident.name

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise IdentityError(f"malformed TOML in {path}: {e}") from e
    except OSError as e:
        raise IdentityError(f"cannot read {path}: {e}") from e

    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    stray = [k for k in data if k != "default" and k not in tables]
    if stray:
        raise IdentityError(
            f"{path}: top-level key(s) {stray} are not identity tables — "
            'only `default = "<name>"` and [name] blocks are allowed.'
        )

    default = data.get("default")
    if not isinstance(default, str) or not default:
        raise IdentityError(
            f'{path}: missing `default = "<name>"` — add it, naming one of '
            f"the identity tables ({sorted(tables) or '[none defined]'})."
        )
    if default not in tables:
        raise IdentityError(
            f"{path}: default identity {default!r} has no [{default}] "
            f"table. Available: {sorted(tables)}"
        )

    identities: dict[str, Identity] = {}
    seen_from: dict[str, str] = {}
    for name, t in tables.items():
        from_addr = str(t.get("from_addr", "")).strip()
        if not from_addr:
            raise IdentityError(
                f"{path}: identity [{name}] is missing `from_addr`."
            )
        bare = from_addr.lower()
        if bare in seen_from:
            raise IdentityError(
                f"{path}: identities [{seen_from[bare]}] and [{name}] share "
                f"from_addr {from_addr!r} — the From: address must map to "
                "exactly one identity."
            )
        seen_from[bare] = name

        driver = str(t.get("driver", "")).strip()
        if driver not in DRIVERS:
            raise IdentityError(
                f"{path}: identity [{name}] has missing or unknown driver "
                f"{driver!r}. Available: {sorted(DRIVERS)}"
            )

        raw_allow = t.get("allowlist", [])
        if isinstance(raw_allow, str):
            raw_allow = raw_allow.split(",")
        allowlist = [str(a).strip().lower() for a in raw_allow if str(a).strip()]

        identities[name] = Identity(
            name=name,
            from_addr=from_addr,
            from_name=str(t.get("from_name", "")).strip(),
            driver=driver,
            params={k: v for k, v in t.items() if k not in _KNOWN_FIELDS},
            allowlist=allowlist,
            allow_all=bool(t.get("allow_all", False)),
            bcc_self=bool(t.get("bcc_self", True)),
        )
    return identities, default


def get(name: str | None = None) -> Identity:
    """Resolve an identity by name.

    None and the literal name "default" BOTH alias the file's default —
    old spool manifests carry "default", and they must keep firing through
    whatever the file currently designates.
    """
    identities, default = load()
    if name is None or name == "default":
        return identities[default]
    if name not in identities:
        raise IdentityError(
            f"unknown identity {name!r}. Available: {sorted(identities)} "
            f"(default: {default!r}) — defined in {config.identities_file()}."
        )
    return identities[name]
