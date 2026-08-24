"""Typed application errors independent of any wire protocol."""
from __future__ import annotations

from . import codes


class ToolError(Exception):
    code: str = codes.INTERNAL_ERROR

    def __init__(
        self,
        error: str,
        *,
        code: str | None = None,
        fix: str | None = None,
        operation_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        super().__init__(error)
        if code is not None:
            self.code = code
        self.fix = fix
        self.operation_id = operation_id
        self.data = data


class NotFound(ToolError):
    code = codes.NOT_FOUND


class InvalidInput(ToolError):
    code = codes.INVALID_INPUT


class MailUnavailable(ToolError):
    code = codes.MAIL_UNAVAILABLE
