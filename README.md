# email-mcp

Local MCP server that gives Claude Code read-only access to every email Apple Mail.app already has on this Mac. No network, no creds, no new index — it just reads Mail's own SQLite envelope database plus the `.emlx` files on disk.

## Install

```bash
cd tools/email-mcp
python3 -m pip install -e ".[dev]"          # or: uv pip install -e ".[dev]"
```

## Register with Claude Code

Add to `~/.claude.json` (merge with whatever is already there):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "python3",
      "args": ["-m", "email_mcp.server"]
    }
  }
}
```

Restart Claude Code. Run `/mcp` — `apple-mail` should appear with six tools: `search_emails`, `get_email`, `get_thread`, `list_mailboxes`, `list_recent`, `get_attachment`.

## macOS Full-Disk-Access

`~/Library/Mail` is gated by macOS. Grant **Full Disk Access** to whichever terminal app starts Claude Code (Terminal / iTerm / VS Code / Cursor):

System Settings → Privacy & Security → Full Disk Access → toggle the app on.

You'll likely need to fully quit and relaunch the terminal app after toggling.

Without FDA the server returns a clear error from `list_mailboxes` on first call, so the failure mode is obvious.

## Smoke test

```bash
python3 -m email_mcp.server --selftest
```

Prints a small JSON summary (number of mailboxes, newest subject/from/date). If this works, the MCP server will work.

## Run the test suite

```bash
cd tools/email-mcp
pytest
```

The tests build a fake `~/Library/Mail/V10` tree in `tmp_path` — they don't read your real mail.

## Environment variables (all optional)

| Var | Default | Effect |
|---|---|---|
| `EMAIL_MCP_MAIL_DIR` | newest `~/Library/Mail/V*` | Override Mail.app base directory. |
| `EMAIL_MCP_SOURCE` | `apple` | Source adapter to load (Phase 2: gmail, imap, …). |
| `EMAIL_MCP_MAX_BODY_BYTES` | `2000000` | Cap on body returned per `get_email`. |
| `EMAIL_MCP_ATTACH_DIR` | `$TMPDIR/email-mcp` | Where `get_attachment` writes blobs. |

## Tool reference

All tools are **read-only**. All return JSON-friendly objects.

| Tool | Purpose |
|---|---|
| `search_emails(query, from_addr?, to_addr?, mailbox?, account?, before?, after?, has_attachment?, unread_only?, limit?, offset?)` | Full-text search over subject + sender + snippet, plus AND filters. |
| `get_email(id)` | Full headers, plain-text and HTML body, attachment list for one message. |
| `get_thread(thread_id)` | All messages in the conversation, oldest first. |
| `list_mailboxes()` | Every mailbox across every account, with counts. |
| `list_recent(mailbox?, account?, limit?)` | Newest messages first. |
| `get_attachment(id, attachment_id)` | Materialises the attachment to a tmp file; returns the path. |

## Phase-2 hooks (not implemented yet)

- **More sources**: implement `EmailSource` in `email_mcp/sources/`, register in `email_mcp/sources/__init__.py::_REGISTRY`, select via `EMAIL_MCP_SOURCE`.
- **FTS5 sidecar**: a separate adapter that mirrors `.emlx` bodies into a local FTS5 db.
- **Write tools** (mark-read, move, send): future, gated behind `EMAIL_MCP_WRITES=1`.

## Safety notes

- Opens the Envelope Index with `?mode=ro&immutable=1` — safe to run while Mail.app is active.
- Never writes to `~/Library/Mail`.
- `get_attachment` writes only to the configurable `EMAIL_MCP_ATTACH_DIR`.
- Body and attachment size are capped by `EMAIL_MCP_MAX_BODY_BYTES`.
