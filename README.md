# email-mcp

Local MCP server that gives Claude Code access to Apple Mail.app on this Mac: **read** every message from Mail's own SQLite envelope database + `.emlx` files (no network, no creds, no new index), and **send** clean mail via a sanctioned transport. Sending is off by default for everyone but you — see [Sending mail](#sending-mail-send_email--reply_email).

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

Restart Claude Code. Run `/mcp` — `apple-mail` should appear with nine tools: `search_emails`, `get_email`, `get_thread`, `list_mailboxes`, `list_recent`, `get_attachment`, `refresh_mail`, `send_email`, `reply_email`.

## macOS Full-Disk-Access

`~/Library/Mail` is gated by macOS. Grant **Full Disk Access** to whichever terminal app starts Claude Code (Terminal / iTerm / VS Code / Cursor):

System Settings → Privacy & Security → Full Disk Access → toggle the app on.

You'll likely need to fully quit and relaunch the terminal app after toggling.

Without FDA the server returns a clear error from `list_mailboxes` on first call, so the failure mode is obvious.

## macOS Automation (needed only for `refresh_mail`)

`refresh_mail` nudges Mail.app to fetch new messages via AppleScript. This requires a second permission, separate from Full Disk Access:

System Settings → Privacy & Security → Automation → *your terminal app* → toggle **Mail** on.

The first call surfaces the macOS prompt; until granted, `refresh_mail` returns `ok: false` with `error_code: -1743` and a clear message pointing at Privacy & Security. The other six tools don't need this permission.

## Sending mail (`send_email` / `reply_email`)

Two write tools compose **clean MIME** (plain + minimal HTML) and deliver it.
They deliberately do **not** go through Mail.app: its AppleScript compose path
wraps the body in a collapsed `Apple-Mail-URLShareWrapperClass` blockquote that
renders as an *empty* message in Outlook/Exchange (reproduced across six compose
variants — it's the OS, not the script). Instead the message is piped to
`sendmail -t -i` on an SSH host (lxplus by default), reusing a warm
`ControlMaster` socket. `smtp.cern.ch` is GPN-internal and refuses tunnelled
STARTTLS, so lxplus `sendmail` is the sanctioned path. A `Bcc`-to-self is added
automatically so there's a searchable record (SMTP-via-sendmail leaves no
Exchange *Sent* copy).

**Self-only safety guard.** While `EMAIL_MCP_SEND_ALLOW_ALL` is off (the
default), every recipient must be on the allowlist — which defaults to *just
the From: address*. A mistake during the trial can therefore only reach you. A
blocked send returns `{ok: false, error}` naming the address; it never leaves
the machine. Flip `EMAIL_MCP_SEND_ALLOW_ALL=1` (or set an explicit
`EMAIL_MCP_SEND_ALLOWLIST`) once you trust it.

**Transport prerequisite.** A live SSH `ControlMaster` socket to the send host.
If it's cold, the tools run `tools/lxplus_mail_master.sh`, which sources
`~/.secrets/cern_secrets.sh` for `CERN_PASSWORD` and generates a TOTP via the
cernvironment helper — headless, self-healing. If that isn't available on your
setup, establish the socket yourself (any 2FA SSH to lxplus with
`-o ControlMaster=yes -o ControlPath=~/.ssh/sock-lxplus-mail -o ControlPersist=4h`)
and the tools reuse it.

End-to-end send test (real transport, defaults to the safe self-only guard):

```bash
python3 -m email_mcp.server --send-test paris.moschovakos@cern.ch
```

Prints `{ok, message_id, to, cc, bcc, ...}`; a non-self address prints the
guard refusal instead of sending.

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
| `EMAIL_MCP_FROM_ADDR` | `paris.moschovakos@cern.ch` | From: address for outgoing mail. |
| `EMAIL_MCP_FROM_NAME` | `Paris Moschovakos` | From: display name. |
| `EMAIL_MCP_SEND_ALLOW_ALL` | `0` | `1` disables the allowlist (send to anyone). |
| `EMAIL_MCP_SEND_ALLOWLIST` | (From: addr) | Comma-separated addresses sending may reach while the guard is on. |
| `EMAIL_MCP_BCC_SELF` | `1` | Bcc the From: address on every send for a record. |
| `EMAIL_MCP_SEND_HOST` | `lxplus.cern.ch` | SSH host that runs `sendmail`. |
| `EMAIL_MCP_SEND_USER` | `pmoschov` | SSH user on that host. |
| `EMAIL_MCP_SSH_SOCKET` | `~/.ssh/sock-lxplus-mail` | ControlMaster socket path. |
| `EMAIL_MCP_SENDMAIL` | `/usr/sbin/sendmail` | Remote sendmail path. |
| `EMAIL_MCP_SSH_BOOTSTRAP` | bundled `tools/lxplus_mail_master.sh` | Command that re-establishes a cold socket headlessly. |

## Tool reference

The seven read tools are **read-only on disk**. `refresh_mail` nudges Mail.app via AppleScript but writes nothing to `~/Library/Mail` itself. `send_email` / `reply_email` are the only tools that leave the machine, gated by the self-only guard above. All return JSON-friendly objects.

| Tool | Purpose |
|---|---|
| `search_emails(query, from_addr?, to_addr?, mailbox?, account?, before?, after?, has_attachment?, unread_only?, limit?, offset?)` | Full-text search over subject + sender + snippet, plus AND filters. |
| `get_email(id)` | Full headers, plain-text and HTML body, attachment list for one message. |
| `get_thread(thread_id)` | All messages in the conversation, oldest first. |
| `list_mailboxes()` | Every mailbox across every account, with counts. |
| `list_recent(mailbox?, account?, limit?)` | Newest messages first. |
| `get_attachment(id, attachment_id)` | Materialises the attachment to a tmp file; returns the path. |
| `refresh_mail(wait_seconds=5, timeout_seconds=30)` | Asks Mail.app to fetch new mail, waits, returns before/after snapshot + delta count. Launches Mail.app if it isn't running. Needs Automation permission (see above). |
| `send_email(to, subject, body, cc?, bcc?)` | Compose clean MIME and send. Comma-separated address strings. Auto Bcc-to-self. Self-only guard applies. Returns `{ok, message_id, to, cc, bcc, subject}` or `{ok: false, error}`. |
| `reply_email(id, body, reply_all?, cc?, bcc?)` | Reply to message `id`, threading via In-Reply-To / References / `Re:` subject. Defaults to the original sender only; `reply_all=True` also Ccs the original To+Cc minus your own address. |

## Phase-2 hooks (not implemented yet)

- **More sources**: implement `EmailSource` in `email_mcp/sources/`, register in `email_mcp/sources/__init__.py::_REGISTRY`, select via `EMAIL_MCP_SOURCE`.
- **FTS5 sidecar**: a separate adapter that mirrors `.emlx` bodies into a local FTS5 db.
- **More write tools** (mark-read, move): future. `send_email` / `reply_email` shipped in v0.2.0.
- **Mail.app transport**: if a future macOS build fixes the scripted-compose wrapper bug, a `transport=mail-app` option could be added so sends file into Exchange *Sent* natively.

## Safety notes

- Opens the Envelope Index with `?mode=ro` (WAL-safe) — safe to run while Mail.app is active.
- Never writes to `~/Library/Mail`. Sending goes over SSH/sendmail, not through the Mail store.
- `get_attachment` writes only to the configurable `EMAIL_MCP_ATTACH_DIR`.
- Body and attachment size are capped by `EMAIL_MCP_MAX_BODY_BYTES`.
- Sending is the only outward action: guarded by the self-only allowlist (default), a mandatory non-empty `to`/`subject`/`body`, and Bcc-to-self for an audit trail.
