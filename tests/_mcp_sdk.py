"""Small test accessor for MCP SDK 1.x camelCase and 2.x snake_case fields."""
from __future__ import annotations


def sdk_attr(value, *names):
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    raise AttributeError(f"{type(value).__name__} has none of {names!r}")
