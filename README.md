# email-mcp

Local MCP server that gives Claude Code read-only access to every email Apple Mail.app already has on this Mac. No network, no creds, no new index — it just reads Mail's own SQLite envelope database plus the `.emlx` files on disk.

## Install

Each MCP server gets its own venv (same pattern as the sibling `*-mcp` repos):

```bash
cd ~/code/parasxos/email-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Register with Claude Code

Add to `~/.claude.json` (merge with whatever is already there) — point at the venv's installed entry point:

```json
{
  "mcpServers": {
    "apple-mail": {
      "type": "stdio",
      "command": "/Users/<you>/code/parasxos/email-mcp/.venv/bin/email-mcp"
    }
  }
}
```

Restart Claude Code. Run `/mcp` — `apple-mail` should appear with seven tools: `search_emails`, `get_email`, `get_thread`, `list_mailboxes`, `list_recent`, `get_attachment`, `refresh_mail`.

## macOS Full-Disk-Access

`~/Library/Mail` is gated by macOS. Grant **Full Disk Access** to whichever terminal app starts Claude Code (Terminal / iTerm / VS Code / Cursor):

System Settings → Privacy & Security → Full Disk Access → toggle the app on.

You'll likely need to fully quit and relaunch the terminal app after toggling.

Without FDA the server returns a clear error from `list_mailboxes` on first call, so the failure mode is obvious.

## macOS Automation (needed only for `refresh_mail`)

`refresh_mail` nudges Mail.app to fetch new messages via AppleScript. This requires a second permission, separate from Full Disk Access:

System Settings → Privacy & Security → Automation → *your terminal app* → toggle **Mail** on.

The first call surfaces the macOS prompt; until granted, `refresh_mail` returns `ok: false` with `error_code: -1743` and a clear message pointing at Privacy & Security. The other six tools don't need this permission.

## Smoke test

```bash
python3 -m email_mcp.server --selftest
```

Prints a small JSON summary (number of mailboxes, newest subject/from/date). If this works, the MCP server will work.

To exercise `refresh_mail` end-to-end against the live Mail.app:

```bash
python3 -m email_mcp.server --refresh-test --refresh-wait 5
```

Prints `{ok, applescript_duration_ms, before, after, new_messages, ...}`. Exit code is non-zero on failure (permission denied, Mail.app missing, timeout).

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

All read tools are **read-only on disk**. `refresh_mail` nudges Mail.app via AppleScript but writes nothing to `~/Library/Mail` itself. All return JSON-friendly objects.

| Tool | Purpose |
|---|---|
| `search_emails(query, from_addr?, to_addr?, mailbox?, account?, before?, after?, has_attachment?, unread_only?, limit?, offset?)` | Full-text search over subject + sender + snippet, plus AND filters. |
| `get_email(id)` | Full headers, plain-text and HTML body, attachment list for one message. |
| `get_thread(thread_id)` | All messages in the conversation, oldest first. |
| `list_mailboxes()` | Every mailbox across every account, with counts. |
| `list_recent(mailbox?, account?, limit?)` | Newest messages first. |
| `get_attachment(id, attachment_id)` | Materialises the attachment to a tmp file; returns the path. |
| `refresh_mail(wait_seconds=5, timeout_seconds=30)` | Asks Mail.app to fetch new mail, waits, returns before/after snapshot + delta count. Launches Mail.app if it isn't running. Needs Automation permission (see above). |

## Phase-2 hooks (not implemented yet)

- **More sources**: implement `EmailSource` in `email_mcp/sources/`, register in `email_mcp/sources/__init__.py::_REGISTRY`, select via `EMAIL_MCP_SOURCE`.
- **FTS5 sidecar**: a separate adapter that mirrors `.emlx` bodies into a local FTS5 db.
- **Write tools** (mark-read, move, send): future, gated behind `EMAIL_MCP_WRITES=1`.

## Safety notes

- Opens the Envelope Index with `?mode=ro&immutable=1` — safe to run while Mail.app is active.
- Never writes to `~/Library/Mail`.
- `get_attachment` writes only to the configurable `EMAIL_MCP_ATTACH_DIR`.
- Body and attachment size are capped by `EMAIL_MCP_MAX_BODY_BYTES`.
