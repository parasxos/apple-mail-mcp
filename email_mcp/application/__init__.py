"""Application use cases and outbound ports.

Nothing in this package imports MCP, macOS APIs, subprocesses, concrete
storage, or network clients.  Those dependencies are supplied by adapters at
the composition root.
"""

from .service import EmailApplication

__all__ = ["EmailApplication"]
