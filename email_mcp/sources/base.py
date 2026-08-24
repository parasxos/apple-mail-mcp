"""Compatibility exports for the provider-neutral mailbox contract.

New code imports these records from :mod:`email_mcp.domain.mail`. Existing
source adapters keep this stable path so third-party implementations do not
break during the architectural migration.
"""
from __future__ import annotations

from ..domain.mail import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    EmailSource,
    Mailbox,
    SearchQuery,
)

__all__ = [
    "AttachmentBlob",
    "AttachmentRef",
    "Email",
    "EmailRef",
    "EmailSource",
    "Mailbox",
    "SearchQuery",
]
