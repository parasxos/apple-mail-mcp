"""The one error-code namespace — data only, imports nothing.

Machine-readable mirror of docs/v1-contract.md §3, which defines what each
code means, which tools raise it, and the versioning rules (codes are never
renamed or removed after v1.0; adding one is additive). This module is
deliberately dumb — flat string constants, frozensets, one dict — so any
layer (server, dispatcher, audit CLI, tests) can import it without dragging
in anything else.

Groups mirror the contract:
  TRIAGE_CODES     tool-level codes on the five triage/mailbox tools
                   (22 raised via TriageError + 2 literal-only)
  ITEM_CODES       per-message result codes inside triage_apply
                   ("ok" marks success internally; failures[].code is
                   always ITEM_CODES minus "ok")
  BELT_CODES       v0.10 wire-safety belts on previously-crashing paths
  SEND_CODES_V011  target codes frozen on paper for today's prose-only
                   SendError failures; wired to the wire at v0.11
  OSA_CODE_MAP     osascript numeric error -> namespace code
"""

# --------------------------------------------------------------------- #
# Triage & mailbox codes (contract §3.1)                                #
# 22 raised via TriageError in email_mcp/triage.py, verbatim.           #
# --------------------------------------------------------------------- #

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

# The 2 literal-only codes: appear directly in mailbox_delete's failure
# dicts (no TriageError behind them).
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

# --------------------------------------------------------------------- #
# Item-level result codes (contract §3.2)                               #
# Per-message vocabulary inside triage_apply; OK marks success in the   #
# result map and never appears in failures[].                           #
# --------------------------------------------------------------------- #

OK = "ok"
MID_MISMATCH = "mid_mismatch"
APPLESCRIPT = "applescript"
NO_RESULT = "no_result"
BATCH_TIMEOUT = "batch_timeout"

ITEM_CODES = frozenset({OK, MID_MISMATCH, APPLESCRIPT, NO_RESULT,
                        BATCH_TIMEOUT})

# --------------------------------------------------------------------- #
# Belt codes (contract §3.5) — new at v0.10                             #
# Coded envelopes on previously-crashing paths; belts log the full      #
# traceback and return fix: "run doctor".                               #
# --------------------------------------------------------------------- #

INTERNAL_ERROR = "internal_error"
NOT_FOUND = "not_found"
INVALID_INPUT = "invalid_input"
MAIL_UNAVAILABLE = "mail_unavailable"

BELT_CODES = frozenset({INTERNAL_ERROR, NOT_FOUND, INVALID_INPUT,
                        MAIL_UNAVAILABLE})

# Dispatcher/ledger-level code, not on any tool's wire: a claimed spool
# manifest whose frozen .eml is gone — the deliver event's failure code.
SPOOL_EML_MISSING = "spool_eml_missing"

# --------------------------------------------------------------------- #
# Send codes (contract §3.4) — v0.11 targets, frozen on paper at v0.10  #
# Assigned to today's prose-only SendError sites; not yet on the wire.  #
# --------------------------------------------------------------------- #

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
# smtp's unprefixed secret-source errors (op/security CLI missing, locked
# 1Password, unreadable Keychain item) — the known-gap assignment.
CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
IDENTITY_MISCONFIGURED = "identity_misconfigured"
UNKNOWN_IDENTITY = "unknown_identity"

SEND_CODES_V011 = frozenset({
    HEADER_INJECTION, INVALID_RECIPIENT, RECIPIENT_NOT_ALLOWED,
    ATTACHMENT_NOT_FOUND, ATTACHMENT_UNREADABLE, ATTACHMENTS_TOO_LARGE,
    INVALID_HEADER, INVALID_INPUT, INVALID_SEND_AT, SEND_AT_IN_PAST,
    TRANSPORT_UNAVAILABLE, DELIVERY_FAILED, AUTH_FAILED,
    CREDENTIALS_UNAVAILABLE, IDENTITY_MISCONFIGURED, UNKNOWN_IDENTITY,
})

# --------------------------------------------------------------------- #
# osascript numeric map (contract §3.3)                                 #
# Context notes: -1743 reads as ACCESSIBILITY_DENIED inside the         #
# mailbox_delete UI fallback (System Events context); -10000 is         #
# advisory on mailbox delete — the outcome there is decided by a live   #
# re-probe, never by the code.                                          #
# --------------------------------------------------------------------- #

OSA_CODE_MAP: dict[int, str] = {
    -1743: AUTOMATION_DENIED,
    -1728: NO_APP,
    -1719: ACCESSIBILITY_DENIED,
    -25211: ACCESSIBILITY_DENIED,
    -10000: SCRIPT_ERROR,
}
