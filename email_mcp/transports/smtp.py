"""SMTP submission transport — smtplib + STARTTLS/SSL, secrets external.

The app password never lives in the repo, the env, or the identities file:
it sits in 1Password (an `op://` secret reference, read via the `op` CLI)
or the macOS Keychain (`security` CLI) and is read at delivery time. Port 465 means implicit TLS
(SMTP_SSL); anything else connects plain and upgrades with STARTTLS.
"""
from __future__ import annotations

import email
import smtplib
import socket
import subprocess
import time

from .. import codes
from ..log import get_logger
from . import SendError

_log = get_logger()

_KEYCHAIN_TIMEOUT = 30  # seconds; a hang means a GUI permission prompt


def _read_keychain(item: str, account: str) -> str:
    """Read a secret from the macOS Keychain via the `security` CLI.

    Module-level seam: tests monkeypatch this; nothing else in the driver
    touches the Keychain. `account` is only used to render the fix-it
    command when the item is missing.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", item, "-w"],
            capture_output=True, text=True, timeout=_KEYCHAIN_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise SendError(
            "`security` CLI not found — the smtp driver needs macOS.",
            code=codes.CREDENTIALS_UNAVAILABLE,
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SendError(
            f"Keychain read for {item!r} timed out after "
            f"{_KEYCHAIN_TIMEOUT}s — macOS is probably showing a permission "
            "prompt this process cannot see. Open Keychain Access, find "
            f"{item!r}, and set Access Control to \"Always Allow\" (or run "
            "the read once in a terminal and click Always Allow).",
            code=codes.CREDENTIALS_UNAVAILABLE,
        ) from e
    if proc.returncode != 0:
        raise SendError(
            f"Keychain item {item!r} not readable (security exit "
            f"{proc.returncode}). Store the app password once with: "
            f"security add-generic-password -s {item} -a {account} -w",
            code=codes.CREDENTIALS_UNAVAILABLE,
        )
    return proc.stdout.rstrip("\n")


_OP_TIMEOUT = 45  # generous: the desktop app may pop a Touch ID prompt


def _read_op(ref: str) -> str:
    """Read a secret from 1Password via the `op` CLI secret reference
    (e.g. "op://Personal/email-mcp gmail app password/password").

    Module-level seam like _read_keychain: tests monkeypatch this. Needs
    the 1Password CLI signed in (desktop-app integration or a session);
    a locked app surfaces as a Touch ID prompt — hence the long timeout.
    """
    try:
        proc = subprocess.run(
            ["op", "read", ref],
            capture_output=True, text=True, timeout=_OP_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise SendError(
            "`op` CLI not found — install the 1Password CLI "
            "(brew install 1password-cli) or use a `keychain` param instead.",
            code=codes.CREDENTIALS_UNAVAILABLE,
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SendError(
            f"1Password read for {ref!r} timed out after {_OP_TIMEOUT}s — "
            "the app is probably locked and waiting for Touch ID. Unlock "
            "1Password and retry. (Headless contexts like the launchd "
            "dispatcher cannot answer that prompt; keep 1Password unlocked "
            "or use a `keychain` param for scheduled mail.)",
            code=codes.CREDENTIALS_UNAVAILABLE,
        ) from e
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise SendError(
            f"1Password read for {ref!r} failed (op exit {proc.returncode}): "
            f"{err[:200]} — check the secret reference (op read {ref!r}) and "
            "that the CLI is signed in (Settings → Developer → CLI integration).",
            code=codes.CREDENTIALS_UNAVAILABLE,
        )
    return proc.stdout.rstrip("\n")


class SmtpTransport:
    """Deliver via authenticated SMTP submission (Gmail, iCloud, …)."""

    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        keychain: str = "",
        op: str = "",
        port: int = 587,
        username: str = "",
        identity: str = "default",
        from_addr: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        # Secret source: a 1Password secret reference (`op`) or a macOS
        # Keychain item (`keychain`). `op` wins when both are set.
        self.keychain = keychain
        self.op = op
        if not op and not keychain:
            raise SendError(
                f"[{identity}/{self.name}] needs a secret source: set "
                "`op` (1Password secret reference) or `keychain` (macOS "
                "Keychain item) in identities.toml.",
                code=codes.IDENTITY_MISCONFIGURED,
            )
        # SMTP AUTH login defaults to the identity's own address, which is
        # what Gmail/iCloud app passwords expect.
        self.username = username or from_addr
        self.identity = identity
        self.from_addr = from_addr
        self.last_ensure_error: str | None = None
        self._prefix = f"[{identity}/{self.name}]"

    def _secret(self) -> str:
        # The readers are identity-agnostic seams; the driver owns the
        # lane, so the secret errors gain their [identity/smtp] prefix
        # here (contract §3.4 — the formerly UNPREFIXED prose).
        try:
            if self.op:
                return _read_op(self.op)
            return _read_keychain(self.keychain, self.username)
        except SendError as e:
            raise SendError(f"{self._prefix} {e}", code=e.code) from e

    @property
    def _secret_ref(self) -> str:
        return self.op or self.keychain

    def _connect(self, timeout: float) -> smtplib.SMTP:
        """Port 465 → implicit TLS; anything else → plain + STARTTLS."""
        if self.port == 465:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=timeout)
        server = smtplib.SMTP(self.host, self.port, timeout=timeout)
        server.starttls()
        return server

    # ----------------------------------------------------------------- #
    # MailTransport protocol                                            #
    # ----------------------------------------------------------------- #

    def deliver(self, raw: bytes, mail_from: str, rcpt_to: list[str]) -> None:
        """Submit the message with explicit envelope recipients.

        Bcc recipients travel ONLY in the envelope (`rcpt_to`): the Bcc
        header is stripped before DATA so no recipient's copy reveals it.
        """
        msg = email.message_from_bytes(raw)
        del msg["Bcc"]
        password = self._secret()
        t0 = time.monotonic()
        _log.info(
            "smtp deliver start: %s:%d, %d bytes, %d rcpt",
            self.host, self.port, len(raw), len(rcpt_to),
        )
        server = None
        try:
            server = self._connect(timeout=60)
            server.login(self.username, password)
            server.sendmail(mail_from, rcpt_to, msg.as_bytes())
        except smtplib.SMTPAuthenticationError as e:
            _log.error("smtp auth failed for %s at %s:%d", self.username,
                       self.host, self.port)
            raise SendError(
                f"{self._prefix} SMTP auth failed for {self.username} at "
                f"{self.host}:{self.port} — secret {self._secret_ref!r} "
                f"holds a wrong or expired app password: {e}",
                code=codes.AUTH_FAILED,
            ) from e
        except (smtplib.SMTPException, OSError) as e:
            _log.error("smtp deliver failed (%.1fs): %s",
                       time.monotonic() - t0, e)
            raise SendError(
                f"{self._prefix} SMTP delivery via {self.host}:{self.port} "
                f"failed: {e}",
                code=codes.DELIVERY_FAILED,
            ) from e
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        _log.info("smtp deliver ok (%.1fs)", time.monotonic() - t0)

    def ensure(self) -> bool:
        """Cheap readiness: the secret source reads and the port answers.
        No AUTH — providers throttle repeated logins."""
        self.last_ensure_error = None
        try:
            self._secret()
        except SendError as e:
            self.last_ensure_error = str(e)
            return False
        try:
            with socket.create_connection((self.host, self.port), timeout=10):
                pass
        except OSError as e:
            self.last_ensure_error = f"cannot reach {self.host}:{self.port}: {e}"
            return False
        return True

    def healthcheck(self) -> dict:
        """Full handshake: connect + EHLO + (STARTTLS) + login + quit."""
        info: dict = {
            "driver": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "secret": self._secret_ref,
            "secret_source": "op" if self.op else "keychain",
        }
        server = None
        try:
            password = self._secret()
            server = self._connect(timeout=30)
            server.ehlo()
            server.login(self.username, password)
        except (SendError, smtplib.SMTPException, OSError) as e:
            info["ok"] = False
            info["error"] = str(e)
            return info
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        info["ok"] = True
        return info
