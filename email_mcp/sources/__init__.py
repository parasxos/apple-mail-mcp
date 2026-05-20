"""Source adapters. Phase 1: Apple Mail. Phase 2: Gmail, IMAP, mbox."""
from __future__ import annotations

from .base import EmailSource

# Registry — populated lazily so we don't import Apple-specific code on
# non-darwin systems. Add Phase-2 sources by extending this dict.
_REGISTRY: dict[str, str] = {
    "apple": "email_mcp.sources.apple_mail:AppleMailSource",
}


def get_source(name: str) -> EmailSource:
    """Resolve a source name (e.g. 'apple') to an initialised EmailSource."""
    import importlib

    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown email source {name!r}. Available: {sorted(_REGISTRY)}"
        )
    module_path, class_name = _REGISTRY[name].split(":", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
