"""Mail.app refresh adapter; the application never imports subprocesses."""
from __future__ import annotations

import subprocess
import time

from .. import applescript, codes
from ..application.models import RefreshOutcome


class AppleMailRefreshGateway:
    @staticmethod
    def _check(timeout_seconds: float) -> dict:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                ["osascript", "-e",
                 'tell application "Mail" to check for new mail'],
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
        before = getattr(source, "freshness_snapshot", lambda: {})()
        result = self._check(timeout_seconds)
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
