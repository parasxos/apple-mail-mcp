"""Pipe transport — hand RFC-822 bytes to a local MTA command over stdin.

For Macs that already run a configured mailer (postfix's sendmail, msmtp,
…). The default `/usr/sbin/sendmail -t -i` is exactly the remote half of
the ssh_sendmail driver, minus the ssh.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import time

from .. import codes
from ..log import get_logger
from . import SendError

_log = get_logger()


class PipeTransport:
    """Deliver by piping the message to a local command's stdin."""

    name = "pipe"

    def __init__(
        self,
        *,
        command: str = "/usr/sbin/sendmail -t -i",
        identity: str = "default",
        from_addr: str = "",
    ) -> None:
        self.command = command
        self.argv = shlex.split(command)
        self.identity = identity
        self.from_addr = from_addr
        self.last_ensure_error: str | None = None
        self._prefix = f"[{identity}/{self.name}]"
        if not self.argv:
            raise SendError(f"{self._prefix} `command` is empty.",
                            code=codes.IDENTITY_MISCONFIGURED)

    # ----------------------------------------------------------------- #
    # MailTransport protocol                                            #
    # ----------------------------------------------------------------- #

    def deliver(self, raw: bytes, mail_from: str, rcpt_to: list[str]) -> None:
        """Pipe the bytes to the command. `mail_from`/`rcpt_to` are
        deliberately unused: `-t`-style commands read the recipients from
        the message's own headers, and the command line stays exactly what
        the user configured."""
        t0 = time.monotonic()
        _log.info("pipe deliver start: %s, %d bytes", self.argv[0], len(raw))
        try:
            proc = subprocess.run(
                self.argv, input=raw, capture_output=True, timeout=60,
            )
        except FileNotFoundError as e:
            raise SendError(
                f"{self._prefix} command not found: {self.argv[0]}",
                code=codes.TRANSPORT_UNAVAILABLE,
            ) from e
        except subprocess.TimeoutExpired as e:
            raise SendError(
                f"{self._prefix} {self.argv[0]} hung for 60s — is the local "
                "MTA configured?",
                code=codes.DELIVERY_FAILED,
            ) from e
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            _log.error(
                "pipe deliver failed (exit %d, %.1fs): %s",
                proc.returncode, time.monotonic() - t0, err[:500] or "no stderr",
            )
            raise SendError(
                f"{self._prefix} delivery failed (exit {proc.returncode}): "
                f"{err or 'no stderr'}",
                code=codes.DELIVERY_FAILED,
            )
        _log.info("pipe deliver ok (%.1fs)", time.monotonic() - t0)

    def ensure(self) -> bool:
        self.last_ensure_error = None
        if shutil.which(self.argv[0]):
            return True
        self.last_ensure_error = (
            f"command not found or not executable: {self.argv[0]}"
        )
        return False

    def healthcheck(self) -> dict:
        found = bool(shutil.which(self.argv[0]))
        return {
            "driver": self.name,
            "command": self.command,
            "executable_found": found,
            "ok": found,
        }
