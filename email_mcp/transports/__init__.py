"""Transport adapters — the write-side mirror of `sources`.

An identity (see `email_mcp.identities`) names a From: address and a driver;
the driver is a `MailTransport` that moves finished RFC-822 bytes to a mail
system. Day-one drivers, all stdlib: `ssh_sendmail` (today's production
path, de-Parised), `smtp` (smtplib + Keychain credentials), `pipe` (local
MTA command). Adding a driver is one class + one registry line.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .. import codes, config
from ..domain.errors import ToolError

if TYPE_CHECKING:
    from ..identities import Identity


class SendError(ToolError):
    """Raised for caller-fixable send failures (blocked recipient, dead
    transport, empty fields). The message is safe to surface verbatim.

    Moved here from `sender.py` in v0.7.0 so drivers can raise it without
    importing the composer; `sender` re-exports it, so `sender.SendError`
    stays the same class for every existing importer and `except` clause.

    Every raise site names its §3.4 code (docs/v1-contract.md — frozen on
    paper at v0.10, on the wire since v0.11); the class default covers a
    bare `SendError("…")` from a replaced delivery seam.
    """

    code = codes.DELIVERY_FAILED


class MailTransport(Protocol):
    """The single write-side extension point. One file per driver.

    Errors must name their lane — messages are prefixed
    `[<identity>/<driver>]` so a failed send says which route broke.
    """

    name: str                       # driver name, e.g. "smtp"
    last_ensure_error: str | None   # set by ensure() on failure

    def deliver(self, raw: bytes, mail_from: str, rcpt_to: list[str]) -> None:
        """Deliver pre-serialised RFC-822 bytes. `mail_from` is the envelope
        sender; drivers whose command reads recipients from the headers
        (`sendmail -t`) may ignore `rcpt_to`. Raises SendError."""
        ...

    def ensure(self) -> bool:
        """Make the transport ready to deliver (bootstrap sessions, check
        credentials). Never raises — on failure returns False and records
        the reason in `last_ensure_error`."""
        ...

    def healthcheck(self) -> dict:
        """Non-mutating diagnostic snapshot for --selftest/--transport-check:
        never bootstraps, never tears anything down."""
        ...


# Registry — populated lazily so importing the package costs nothing.
# Add drivers by extending this dict (same shape as sources._REGISTRY).
DRIVERS: dict[str, str] = {
    "ssh_sendmail": "email_mcp.transports.ssh_sendmail:SshSendmailTransport",
    "smtp": "email_mcp.transports.smtp:SmtpTransport",
    "pipe": "email_mcp.transports.pipe:PipeTransport",
}


def get_transport(identity: "Identity") -> MailTransport:
    """Resolve an Identity to an initialised MailTransport for its driver."""
    import importlib

    driver = identity.driver
    if driver not in DRIVERS:
        raise SendError(
            f"[{identity.name}/{driver}] unknown transport driver "
            f"{driver!r}. Available: {sorted(DRIVERS)}",
            code=codes.IDENTITY_MISCONFIGURED,
        )
    module_path, class_name = DRIVERS[driver].split(":", 1)
    cls = getattr(importlib.import_module(module_path), class_name)
    try:
        return cls(
            identity=identity.name,
            from_addr=identity.from_addr,
            **identity.params,
        )
    except TypeError as e:
        raise SendError(
            f"[{identity.name}/{driver}] bad transport params: {e}. "
            f"Fix the [{identity.name}] block in {config.identities_file()}.",
            code=codes.IDENTITY_MISCONFIGURED,
        ) from e
