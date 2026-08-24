"""Stable machine-readable error vocabulary shared across all boundaries."""

INVALID_ACTION = "invalid_action"
DESTRUCTIVE_ACTION = "destructive_action"
CONFLICTING_ACTIONS = "conflicting_actions"
UNSUPPORTED_SOURCE = "unsupported_source"
EMPTY_SELECTION = "empty_selection"
SELECTION_TOO_LARGE = "selection_too_large"
CROSS_ACCOUNT = "cross_account"
NOOP_MOVE = "noop_move"
UNKNOWN_MAILBOX = "unknown_mailbox"
INVALID_NAME = "invalid_name"
PLAN_NOT_FOUND = "plan_not_found"
PLAN_ALREADY_APPLIED = "plan_already_applied"
PLAN_EXPIRED = "plan_expired"
PLAN_CLAIMED = "plan_claimed"
OSASCRIPT_UNAVAILABLE = "osascript_unavailable"
MAIL_UNRESPONSIVE = "mail_unresponsive"
AUTOMATION_DENIED = "automation_denied"
NO_APP = "no_app"
SCRIPT_ERROR = "script_error"
ACCOUNT_UNRESOLVABLE = "account_unresolvable"
UNKNOWN_ACCOUNT = "unknown_account"
NOT_EMPTY = "not_empty"
ACCESSIBILITY_DENIED = "accessibility_denied"
DELETE_FAILED = "delete_failed"

TRIAGE_CODES = frozenset({
    INVALID_ACTION, DESTRUCTIVE_ACTION, CONFLICTING_ACTIONS,
    UNSUPPORTED_SOURCE, EMPTY_SELECTION, SELECTION_TOO_LARGE, CROSS_ACCOUNT,
    NOOP_MOVE, UNKNOWN_MAILBOX, INVALID_NAME, PLAN_NOT_FOUND,
    PLAN_ALREADY_APPLIED, PLAN_EXPIRED, PLAN_CLAIMED, OSASCRIPT_UNAVAILABLE,
    MAIL_UNRESPONSIVE, AUTOMATION_DENIED, NO_APP, SCRIPT_ERROR,
    ACCOUNT_UNRESOLVABLE, UNKNOWN_ACCOUNT, NOT_EMPTY,
    ACCESSIBILITY_DENIED, DELETE_FAILED,
})

OK = "ok"
MID_MISMATCH = "mid_mismatch"
APPLESCRIPT = "applescript"
NO_RESULT = "no_result"
BATCH_TIMEOUT = "batch_timeout"
NOT_ATTEMPTED = "not_attempted"

ITEM_CODES = frozenset({
    OK, MID_MISMATCH, APPLESCRIPT, NO_RESULT, BATCH_TIMEOUT, NOT_ATTEMPTED,
})

INTERNAL_ERROR = "internal_error"
NOT_FOUND = "not_found"
INVALID_INPUT = "invalid_input"
MAIL_UNAVAILABLE = "mail_unavailable"

BELT_CODES = frozenset({
    INTERNAL_ERROR, NOT_FOUND, INVALID_INPUT, MAIL_UNAVAILABLE,
})

SPOOL_EML_MISSING = "spool_eml_missing"
SPOOL_INTEGRITY = "spool_integrity"
SPOOL_CODES = frozenset({SPOOL_EML_MISSING, SPOOL_INTEGRITY})

HEADER_INJECTION = "header_injection"
INVALID_RECIPIENT = "invalid_recipient"
RECIPIENT_NOT_ALLOWED = "recipient_not_allowed"
ATTACHMENT_NOT_FOUND = "attachment_not_found"
ATTACHMENT_UNREADABLE = "attachment_unreadable"
ATTACHMENTS_TOO_LARGE = "attachments_too_large"
INVALID_HEADER = "invalid_header"
INVALID_SEND_AT = "invalid_send_at"
SEND_AT_IN_PAST = "send_at_in_past"
TRANSPORT_UNAVAILABLE = "transport_unavailable"
DELIVERY_FAILED = "delivery_failed"
AUTH_FAILED = "auth_failed"
CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
IDENTITY_MISCONFIGURED = "identity_misconfigured"
UNKNOWN_IDENTITY = "unknown_identity"
DRAFT_UNSUPPORTED = "draft_unsupported"

SEND_CODES_V011 = frozenset({
    HEADER_INJECTION, INVALID_RECIPIENT, RECIPIENT_NOT_ALLOWED,
    ATTACHMENT_NOT_FOUND, ATTACHMENT_UNREADABLE, ATTACHMENTS_TOO_LARGE,
    INVALID_HEADER, INVALID_INPUT, INVALID_SEND_AT, SEND_AT_IN_PAST,
    TRANSPORT_UNAVAILABLE, DELIVERY_FAILED, AUTH_FAILED,
    CREDENTIALS_UNAVAILABLE, IDENTITY_MISCONFIGURED, UNKNOWN_IDENTITY,
})

OSA_CODE_MAP: dict[int, str] = {
    -1743: AUTOMATION_DENIED,
    -1728: NO_APP,
    -1719: ACCESSIBILITY_DENIED,
    -25211: ACCESSIBILITY_DENIED,
    -10000: SCRIPT_ERROR,
}
