"""Runtime configuration via environment variables. All optional."""
from __future__ import annotations

import os
import re
from pathlib import Path


def mail_dir() -> Path:
    """Return the active Mail.app data directory.

    Honors EMAIL_MCP_MAIL_DIR; otherwise picks the newest ~/Library/Mail/V*.
    """
    override = os.environ.get("EMAIL_MCP_MAIL_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    base = Path.home() / "Library" / "Mail"
    if not base.exists():
        raise FileNotFoundError(
            f"{base} does not exist. Apple Mail is not configured on this Mac, "
            f"or grant Full Disk Access to the app running Claude Code."
        )
    versioned = [
        p for p in base.iterdir()
        if p.is_dir() and re.fullmatch(r"V\d+", p.name)
    ]
    if not versioned:
        raise FileNotFoundError(
            f"No V<N> directory found under {base}. "
            f"Grant Full Disk Access to the app running Claude Code."
        )
    # Highest version number wins (V10 > V9 > V8 …).
    return max(versioned, key=lambda p: int(p.name[1:]))


def source_name() -> str:
    return os.environ.get("EMAIL_MCP_SOURCE", "apple").strip() or "apple"


def max_body_bytes() -> int:
    return int(os.environ.get("EMAIL_MCP_MAX_BODY_BYTES", "2000000"))


def attach_dir() -> Path:
    raw = os.environ.get("EMAIL_MCP_ATTACH_DIR", "").strip()
    if raw:
        d = Path(raw).expanduser()
    else:
        tmp = os.environ.get("TMPDIR", "/tmp")
        d = Path(tmp) / "email-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d
