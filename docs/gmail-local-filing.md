# Gmail messages filed into local Mail folders

A native Mail rule can create a complete copy in an On My Mac folder while
leaving the original message in Gmail's Inbox. In the observed failure, Mail
removed its local source mapping and had no queued action left, while a fresh
IMAP connection still found the exact Gmail message in Inbox. Running the
rules again produced additional local copies.

An empty Mail queue or the presence of a destination copy does not establish
that the server Inbox changed. Changing Gmail's Auto-Expunge setting did not
fix the controlled reproduction; the original setting was restored.

## Recovery adapter

`email_mcp.gmail_archive` is an internal recovery adapter, not a registered
MCP tool or an automatic rule runner. It uses the existing configured Gmail
IMAP identity. Callers must supply an explicitly reviewed inventory of exact
All Mail UIDs, RFC Message-IDs, and complete local preservation copies.

### Python entrypoint

Use the import API from a reviewed recovery script. There is no CLI command
or MCP endpoint for this adapter. Select an existing identity with an `imap`
configuration from `identities.load()`. The adapter discovers the actual
Gmail `\All` mailbox through IMAP LIST, regardless of that identity's
optional `imap.folder` setting. UIDs must come from this All Mail mailbox.

Each candidate is a dictionary with these fields:

| Field | Value |
| --- | --- |
| `all_mail_uid` | Exact All Mail UID as a positive decimal string |
| `rfc_message_id` | The reviewed RFC Message-ID, with or without angle brackets |
| `local_copy_path` | Absolute path to a complete local `.emlx` preservation copy |
| `apple_global_message_id`, `keeper_rowid` | Optional integer identifiers retained in the private plan |

Create the plan and retain the returned digest for review:

```python
from pathlib import Path
from email_mcp import gmail_archive, identities

configured, _ = identities.load()
identity = configured["gmail"]
candidates = [{
    "all_mail_uid": "12345",
    "rfc_message_id": "<message@example.org>",
    "local_copy_path": "/absolute/path/keeper.emlx",
}]
plan = gmail_archive.plan_archive(identity, candidates)
plan_path = Path("gmail-archive-plan.json").resolve()
reviewed_digest = gmail_archive.save_plan(plan, plan_path)
```

Review the private plan before applying. In the same script session, or after
loading the identity and saved path again, pass the original reviewed digest:

```python
result = gmail_archive.apply_archive(identity, plan_path, reviewed_digest)
```

Do not recompute the digest to accept edits to the saved plan. Plans expire
according to `EMAIL_MCP_TRIAGE_TTL`. `result["complete"]` means every selected
message was verified outside Inbox and still in All Mail. Inspect each entry
in `result["results"]` when it is false. The same unchanged, unexpired plan can
be retried after an uncertain result; already archived messages need no
additional mutation. Importing the module does not connect. `plan_archive` reads provider metadata,
`save_plan` writes the local plan, and `apply_archive` can remove Inbox
membership.

### Guarantees

The adapter:

- Limits plans to 200 messages and honors read-only mode.
- Validates complete local `.emlx` content and rejects missing external MIME
  parts. The local digest protects the reviewed copy; it does not compare
  the full server body with that copy.
- Freezes the account identity, UIDVALIDITY, Gmail message ID, Message-ID,
  size, flags, labels, and local digest in a private, expiring plan.
- Rechecks the plan and source before removing only the `\Inbox` label.
- Opens a fresh connection to verify Inbox absence, All Mail retention,
  and unchanged flags and other labels, including after an uncertain reply.

The adapter never issues a delete or expunge command. It preserves the Gmail
message in All Mail. A failed or partial result requires checking the saved
per-message outcomes before preparing a retry.

`email_mcp.adapters.refresh.synchronize_account` asks native Mail to
synchronize one existing IMAP account. A successful return means the request
was accepted; callers still need to verify the resulting mailbox state.

## Duplicate cleanup

Keep an older, complete copy in the intended category. Move only explicitly
reviewed duplicate IDs into a recovery mailbox through the normal triage
plan/apply path, then verify both the recovered payload and its keeper.
Never repair this condition by editing the live Envelope Index.

The local triage executor groups eligible moves through a native filtered
message specifier. Each chunk requires all Message-ID guards to pass. Remote
moves and compound actions retain their existing execution path. Mail can
partly perform a bulk operation before reporting an error, so verification
decides which messages succeeded; the executor does not retry the group.

This recovery corrects the selected mailbox state. It does not change
Apple's native rule engine or promise that future cross-account rule moves
will remove Gmail Inbox membership.
