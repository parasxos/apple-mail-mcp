"""Attachment normalization, loading, type detection, and size policy."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from . import codes, config
from .transports import SendError


def attachment_paths(attachments: str | list[str] | None) -> list[Path]:
    if not attachments:
        return []
    items = [attachments] if isinstance(attachments, str) else list(attachments)
    return [Path(path).expanduser() for path in items if str(path).strip()]


def load_attachments(
    attachments: str | list[str] | None,
) -> list[tuple[bytes, str, str, str]]:
    paths = attachment_paths(attachments)
    if not paths:
        return []

    loaded: list[tuple[bytes, str, str, str]] = []
    total = 0
    for path in paths:
        if not path.exists():
            raise SendError(
                f"attachment not found: {path}",
                code=codes.ATTACHMENT_NOT_FOUND,
            )
        if path.is_dir():
            raise SendError(
                f"attachment is a directory: {path} — zip it first and "
                "attach the archive.",
                code=codes.ATTACHMENT_UNREADABLE,
            )
        try:
            data = path.read_bytes()
        except OSError as error:
            raise SendError(
                f"cannot read attachment {path}: {error}",
                code=codes.ATTACHMENT_UNREADABLE,
            ) from error
        total += len(data)
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (
            content_type or "application/octet-stream"
        ).partition("/")
        loaded.append((data, maintype, subtype, path.name))

    budget = config.send_max_attach_mb()
    if total > budget * 1024 * 1024:
        raise SendError(
            f"attachments total {total / (1024 * 1024):.1f} MB, over the "
            f"{budget:g} MB budget (base64 adds ~33% on top; servers commonly "
            "reject large mail). Shrink the set, or raise "
            "EMAIL_MCP_MAX_ATTACH_MB if the recipient's server allows it.",
            code=codes.ATTACHMENTS_TOO_LARGE,
        )
    return loaded
