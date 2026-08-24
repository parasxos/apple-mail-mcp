"""Shared AppleScript error vocabulary used by every macOS integration."""
from __future__ import annotations

import pytest

from email_mcp import applescript


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("execution error: Not authorised (-1743)", -1743),
        ("execution error: Can't get application Mail. (-1728)\n", -1728),
        ("execution error: handler failed. (-10000)", -10000),
        ("execution error: malformed (-oops)", None),
        ("execution error: missing code", None),
        ("", None),
    ],
)
def test_error_code_is_one_total_parser(stderr, expected):
    assert applescript.error_code(stderr) == expected


def test_error_code_constants_are_one_shared_vocabulary():
    assert applescript.NOT_AUTHORIZED == -1743
    assert applescript.NO_APP == -1728
    assert applescript.EVENT_HANDLER_FAILED == -10000
    assert applescript.ACCESSIBILITY_DENIED == frozenset({-1719, -25211, -1743})
