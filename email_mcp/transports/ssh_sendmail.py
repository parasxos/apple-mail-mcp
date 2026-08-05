"""SSH + remote sendmail transport — today's production path, de-Parised.

Bodies extracted from `sender.py` (v0.6.0) with the config getters replaced
by constructor parameters, so any host/user/socket/bootstrap combination
works. Delivery pipes RFC-822 bytes to `<delivery_cmd> -t -i -f <mail_from>`
over an existing SSH ControlMaster session; if the session is cold, a
headless bootstrap script re-establishes it.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .. import codes, config
from ..log import get_logger
from . import SendError

_log = get_logger()


class SshSendmailTransport:
    """Deliver via `ssh <user>@<host> '<delivery_cmd> -t -i -f <from>'`."""

    name = "ssh_sendmail"

    def __init__(
        self,
        *,
        host: str,
        user: str,
        socket: str,
        bootstrap: str = "",
        delivery_cmd: str = "/usr/sbin/sendmail",
        identity: str = "default",
        from_addr: str = "",
    ) -> None:
        self.host = host
        self.user = user
        self.socket = Path(socket).expanduser()
        self.bootstrap = bootstrap
        self.delivery_cmd = delivery_cmd
        self.identity = identity
        self.from_addr = from_addr
        self.last_ensure_error: str | None = None
        self._prefix = f"[{identity}/{self.name}]"

    # ----------------------------------------------------------------- #
    # session plumbing                                                  #
    # ----------------------------------------------------------------- #

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-o", f"ControlPath={self.socket}",
            "-o", "BatchMode=yes",
            f"{self.user}@{self.host}",
        ]

    def socket_alive(self) -> bool:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["ssh", "-O", "check",
                 "-o", f"ControlPath={self.socket}",
                 f"{self.user}@{self.host}"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            _log.warning(
                "socket check errored (%s) after %.1fs",
                type(e).__name__, time.monotonic() - t0,
            )
            return False
        alive = proc.returncode == 0
        _log.info(
            "socket check: %s (%.2fs)",
            "alive" if alive else "dead", time.monotonic() - t0,
        )
        return alive

    def kill_master(self) -> None:
        """Tear down the ControlMaster socket (best-effort). Used after a hung
        delivery pipe: a wedged master passes `-O check` yet stalls every
        channel, so killing it lets the next send bootstrap a fresh session."""
        try:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self.socket}",
                 f"{self.user}@{self.host}"],
                capture_output=True, text=True, timeout=10,
            )
            _log.warning("killed suspect ControlMaster socket after hung delivery")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            _log.warning("could not kill ControlMaster: %s", type(e).__name__)

    def bootstrap_master(self) -> bool:
        """Run the headless bootstrap to (re)establish the master socket."""
        cmd = self.bootstrap
        if not cmd:
            _log.warning("no bootstrap command configured; cannot re-establish SSH")
            return False
        t0 = time.monotonic()
        _log.info("bootstrapping SSH master: %s", cmd)
        try:
            proc = subprocess.run(
                ["bash", cmd] if cmd.endswith(".sh") else ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            _log.error(
                "bootstrap errored (%s) after %.1fs",
                type(e).__name__, time.monotonic() - t0,
            )
            return False
        _log.info(
            "bootstrap exit %d (%.1fs)%s",
            proc.returncode, time.monotonic() - t0,
            "" if proc.returncode == 0
            else f" stderr: {(proc.stderr or '').strip()[:500]}",
        )
        return self.socket_alive()

    # ----------------------------------------------------------------- #
    # MailTransport protocol                                            #
    # ----------------------------------------------------------------- #

    def deliver(self, raw: bytes, mail_from: str, rcpt_to: list[str]) -> None:
        """Pipe pre-serialised RFC-822 bytes to the remote delivery command.

        `rcpt_to` is deliberately unused: `sendmail -t` reads the recipients
        from the message's own To/Cc/Bcc headers.
        """
        from email.parser import BytesHeaderParser

        hdr = BytesHeaderParser().parsebytes(raw, headersonly=True)
        msgid, to = hdr.get("Message-ID", "?"), hdr.get("To", "?")
        remote = f"{self.delivery_cmd} -t -i -f {mail_from}"
        t0 = time.monotonic()
        _log.info("deliver start: %s, %d bytes, to=%s", msgid, len(raw), to)
        try:
            proc = subprocess.run(
                self._ssh_base() + [remote],
                input=raw, capture_output=True, timeout=60,
            )
        except FileNotFoundError as e:
            _log.error("deliver failed: ssh not found on PATH")
            raise SendError(f"{self._prefix} ssh not found on PATH.",
                            code=codes.TRANSPORT_UNAVAILABLE) from e
        except subprocess.TimeoutExpired as e:
            _log.error(
                "deliver timed out after %.0fs: %s — master passed check but the "
                "pipe hung; killing the socket so the next send re-bootstraps",
                time.monotonic() - t0, msgid,
            )
            self.kill_master()
            raise SendError(
                f"{self._prefix} delivery pipe timed out after 60s — the SSH "
                "master looked alive but the session hung (stale "
                "ControlMaster). The socket has been reset; retry once and "
                "the send will re-bootstrap. Log: "
                f"{config.log_file() or 'disabled'}.",
                code=codes.DELIVERY_FAILED,
            ) from e
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            _log.error(
                "deliver failed (exit %d, %.1fs): %s",
                proc.returncode, time.monotonic() - t0, err[:500] or "no stderr",
            )
            raise SendError(
                f"{self._prefix} delivery failed (exit {proc.returncode}): "
                f"{err or 'no stderr'}",
                code=codes.DELIVERY_FAILED,
            )
        _log.info("deliver ok: %s (%.1fs)", msgid, time.monotonic() - t0)

    def ensure(self) -> bool:
        """Socket alive → done; otherwise bootstrap and re-check."""
        self.last_ensure_error = None
        if self.socket_alive():
            return True
        if self.bootstrap_master():
            return True
        self.last_ensure_error = (
            f"no live SSH ControlMaster to {self.user}@{self.host} and "
            "bootstrap failed. Establish the socket (2FA) then retry"
            + (f" — e.g. run {self.bootstrap}." if self.bootstrap
               else " (no bootstrap command configured).")
        )
        return False

    def healthcheck(self) -> dict:
        """Report the socket state without bootstrapping or killing anything
        (`ssh -O check` is read-only).

        An unhealthy report carries its own `fix`: the driver knows the
        remedy for its own lane, so doctor never has to guess one (RC
        P03 found a red transports check with no fix at all)."""
        alive = self.socket_alive()
        out = {
            "driver": self.name,
            "host": self.host,
            "user": self.user,
            "socket": str(self.socket),
            "bootstrap": self.bootstrap,
            "socket_alive": alive,
            "ok": alive,
        }
        if not alive:
            out["fix"] = (
                f"run {self.bootstrap} to re-establish the session"
                if self.bootstrap else
                f"establish the ControlMaster (2FA): ssh -fN -o "
                f"ControlMaster=yes -o ControlPath={self.socket} "
                f"{self.user}@{self.host}"
            ) + " — a cold socket is a state, not damage: the next send "
            if self.bootstrap:
                out["fix"] += "bootstraps it headlessly."
                # Self-healing by the driver's own claim: degradation the
                # next send repairs must warn, never redden the doctor —
                # a first user on holiday read "NOT ready" off a lane
                # that needed no hand at all (2026-08-05).
                out["advisory"] = True
            else:
                out["fix"] += "will report it until the session exists."
        return out
