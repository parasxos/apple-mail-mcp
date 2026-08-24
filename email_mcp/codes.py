"""Compatibility export for the domain error-code vocabulary.

The canonical definitions live in :mod:`email_mcp.domain.codes`; this module
keeps the public ``email_mcp.codes`` import stable for clients and adapters.
"""
from .domain.codes import *  # noqa: F401,F403
