"""Pure email domain vocabulary.

This package contains data and events only.  It deliberately imports no MCP,
macOS, network, database, filesystem, or process adapter.
"""

from .events import DomainEvent, EventPublisher, NullEventPublisher
from .errors import InvalidInput, MailUnavailable, NotFound, ToolError
from .mail import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    EmailSource,
    Mailbox,
    SearchQuery,
)
from .models import (
    DraftResult,
    IntegrityIssue,
    Plan,
    PlanAction,
    PlanMessage,
    ScheduledEntry,
    ScheduledScan,
    SendResult,
)

__all__ = [
    "AttachmentBlob",
    "AttachmentRef",
    "DomainEvent",
    "DraftResult",
    "Email",
    "EmailRef",
    "EmailSource",
    "EventPublisher",
    "IntegrityIssue",
    "InvalidInput",
    "MailUnavailable",
    "Mailbox",
    "NullEventPublisher",
    "NotFound",
    "Plan",
    "PlanAction",
    "PlanMessage",
    "ScheduledEntry",
    "ScheduledScan",
    "SearchQuery",
    "SendResult",
    "ToolError",
]
