"""Shared macOS AppleScript error vocabulary.

Refresh, doctor, and triage intentionally keep separate subprocess seams:
they invoke different scripts, accept different inputs, and have different
recovery policies.  Numeric AppleScript interpretation is common, though,
and must not drift between those callers.
"""
from __future__ import annotations


# Stable osascript/Apple Event error numbers observed across the Mail and
# System Events integrations.
NOT_AUTHORIZED = -1743
NO_APP = -1728
ASSISTIVE_ACCESS_DENIED = frozenset({-1719, -25211})
ACCESSIBILITY_DENIED = ASSISTIVE_ACCESS_DENIED | {NOT_AUTHORIZED}
EVENT_HANDLER_FAILED = -10000


def error_code(stderr: str | None) -> int | None:
    """Extract the trailing ``(-1234)`` code from osascript stderr.

    The parser is deliberately total: malformed, absent, or non-numeric
    suffixes return ``None`` rather than creating a second failure while an
    original AppleScript failure is being reported.
    """
    text = (stderr or "").strip()
    if "(-" not in text or not text.endswith(")"):
        return None
    try:
        return int(text.rsplit("(", 1)[1][:-1])
    except ValueError:
        return None
