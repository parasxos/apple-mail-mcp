"""Recipient normalization, validation, and identity authorization."""
from __future__ import annotations

import re
from email.utils import formataddr, getaddresses, parseaddr

from . import codes, identities
from .transports import SendError

CONTROL_RE = re.compile(r"[\r\n\x00]")


def reject_header_injection(fields: dict[str, object]) -> None:
    for name, value in fields.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item and CONTROL_RE.search(str(item)):
                raise SendError(
                    f"header_injection: control character (CR/LF/NUL) in "
                    f"`{name}`: {str(item)!r} — headers are single-line; "
                    "put extra recipients in to/cc/bcc, extra text in body.",
                    code=codes.HEADER_INJECTION,
                )


def validate_bare_addresses(field: str, addrs: list[str]) -> None:
    for entry in addrs:
        address = parseaddr(entry)[1].strip()
        local, separator, domain = address.partition("@")
        if (not separator or not local or not domain or "@" in domain
                or any(char.isspace() or ord(char) < 0x20 for char in address)):
            raise SendError(
                f"invalid_recipient: {entry!r} in `{field}` is not a usable "
                "address (want user@domain, optionally as 'Name <user@domain>').",
                code=codes.INVALID_RECIPIENT,
            )


def split_addresses(addrs: str | list[str] | None) -> list[str]:
    if not addrs:
        return []
    if isinstance(addrs, str):
        pairs = getaddresses([addrs])
        return [formataddr(pair) if pair[0] else pair[1]
                for pair in pairs if pair[1]]
    normalized: list[str] = []
    for item in addrs:
        for name, address in getaddresses([item]):
            if address:
                normalized.append(
                    formataddr((name, address)) if name else address
                )
    return normalized


def recipient_lists(
    to: str | list[str] | None,
    cc: str | list[str] | None,
    bcc: str | list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    normalized: list[list[str]] = []
    for name, raw in (("to", to), ("cc", cc), ("bcc", bcc)):
        items = [] if not raw else ([raw] if isinstance(raw, str) else list(raw))
        for item in items:
            if CONTROL_RE.search(str(item)):
                raise SendError(
                    f"invalid_recipient: control character (CR/LF/NUL) in "
                    f"`{name}`: {str(item)!r} — addresses are single-line, "
                    "comma-separated.",
                    code=codes.INVALID_RECIPIENT,
                )
        split = split_addresses(raw)
        validate_bare_addresses(name, split)
        normalized.append(split)
    return normalized[0], normalized[1], normalized[2]


def bare_address(address: str) -> str:
    return parseaddr(address)[1].strip().lower()


def enforce_allowlist(
    recipients: list[str],
    identity: identities.Identity,
) -> None:
    if identity.allow_all:
        return
    allowed = (
        {address.strip().lower() for address in identity.allowlist}
        | {bare_address(identity.from_addr)}
    )
    blocked = sorted({
        bare_address(recipient) for recipient in recipients
        if bare_address(recipient) not in allowed
    })
    if blocked:
        raise SendError(
            f"Refusing to send as identity [{identity.name}]: recipient(s) not "
            f"on its allowlist — {', '.join(blocked)}. Sending is restricted "
            f"to {', '.join(sorted(allowed))} by this identity's declared "
            "guard (allowlist / allow_all = false) — extend the allowlist, "
            "or remove the declaration to lift it.",
            code=codes.RECIPIENT_NOT_ALLOWED,
        )
