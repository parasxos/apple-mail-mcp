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
    DraftRequest,
    DraftResult,
    IntegrityIssue,
    Plan,
    PlanAction,
    PlanMessage,
    ReplyRequest,
    ScheduleRequest,
    ScheduledEntry,
    ScheduledScan,
    SendRequest,
    SendResult,
)

__all__ = [
    "AttachmentBlob",
    "AttachmentRef",
    "DomainEvent",
    "DraftResult",
    "DraftRequest",
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
    "ReplyRequest",
    "ScheduleRequest",
    "ScheduledEntry",
    "ScheduledScan",
    "SearchQuery",
    "SendRequest",
    "SendResult",
    "ToolError",
]
