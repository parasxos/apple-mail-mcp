"""Mail.app refresh adapter; the application never imports subprocesses."""
from __future__ import annotations

import re
import subprocess
import time

from .. import applescript, codes
from ..application.models import RefreshOutcome


class AppleMailRefreshGateway:
    @staticmethod
    def _check(timeout_seconds: float, *, script: str | None = None) -> dict:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                ["osascript", "-e",
                 script or 'tell application "Mail" to check for new mail'],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": "osascript not found — this tool only works on macOS.",
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": f"osascript timed out after {timeout_seconds:g}s.",
            }

        duration_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode == 0:
            return {"ok": True, "duration_ms": duration_ms}
        stderr = (proc.stderr or "").strip()
        code = applescript.error_code(stderr)
        if code == applescript.NOT_AUTHORIZED:
            message = (
                "Mail.app automation is not authorised for this terminal. "
                "Grant it in System Settings → Privacy & Security → "
                "Automation, then retry."
            )
        elif code == applescript.NO_APP:
            message = "Mail.app is not installed or not reachable via AppleScript."
        else:
            message = stderr or f"osascript failed with exit code {proc.returncode}."
        result = {"ok": False, "duration_ms": duration_ms, "error": message}
        if code is not None:
            result["error_code"] = code
        return result

    def refresh(self, source, wait_seconds: float,
                timeout_seconds: float) -> RefreshOutcome:
        return self._refresh(source, wait_seconds, timeout_seconds)

    def _refresh(self, source, wait_seconds: float,
                 timeout_seconds: float, *, script: str | None = None) -> RefreshOutcome:
        before = getattr(source, "freshness_snapshot", lambda: {})()
        result = (self._check(timeout_seconds, script=script) if script is not None
                  else self._check(timeout_seconds))
        if result["ok"] and wait_seconds > 0:
            time.sleep(wait_seconds)
        after = getattr(source, "freshness_snapshot", lambda: {})()
        new_messages = None
        if before and after:
            old_total, new_total = before.get("total"), after.get("total")
            if isinstance(old_total, int) and isinstance(new_total, int):
                new_messages = max(0, new_total - old_total)
        error_code = result.get("error_code")
        return RefreshOutcome(
            ok=result["ok"],
            applescript_duration_ms=result.get("duration_ms"),
            waited_seconds=wait_seconds if result["ok"] else 0.0,
            before=before or None,
            after=after or None,
            new_messages=new_messages,
            error=result.get("error"),
            error_code=error_code,
            code=(codes.OSA_CODE_MAP.get(error_code)
                  if error_code is not None else None),
        )


def synchronize_account(source, account_uuid: str, *, wait_seconds: float = 5.0,
                        timeout_seconds: float = 30.0) -> RefreshOutcome:
    """Ask Mail to synchronize one existing IMAP account with its server.

    This internal refresh path has the same read-side semantics as checking
    for new mail, including in read-only mode. A successful command means
    synchronization was requested; callers must verify the resulting state.
    """
    if not isinstance(account_uuid, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", account_uuid
    ):
        raise ValueError("account_uuid must be a canonical UUID")
    account_uuid = account_uuid.upper()
    script = (
        'tell application "Mail"\n'
        f'    if not (exists account id "{account_uuid}") then\n'
        '        error "Requested Mail account does not exist." number -2700\n'
        '    end if\n'
        f'    set targetAccount to account id "{account_uuid}"\n'
        '    if (account type of targetAccount) is not imap then\n'
        '        error "Requested Mail account is not IMAP." number -2700\n'
        '    end if\n'
        '    synchronize with targetAccount\n'
        'end tell'
    )
    wait_seconds = max(0.0, min(60.0, float(wait_seconds)))
    timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))
    return AppleMailRefreshGateway()._refresh(
        source, wait_seconds, timeout_seconds, script=script)
