# email-mcp

**Your Apple Mail, fully agent-operable.**
An MCP server that gives Claude direct access to Mail.app on your Mac — read at SQL speed, send Outlook-safe mail as any of your identities, schedule delivery, and triage whole mailboxes — without ever letting AppleScript near a message body.

![version](https://img.shields.io/badge/version-v0.9.0-blue)
![tools](https://img.shields.io/badge/MCP%20tools-19-brightgreen)
![tests](https://img.shields.io/badge/tests-172%20passing-success)
![platform](https://img.shields.io/badge/platform-macOS%20%2B%20Mail.app-orange)

## Install

```bash
pipx install git+https://github.com/parasxos/email-mcp
email-mcp setup
```

`setup` adopts `~/.email-mcp`, offers a sending identity and the launchd
agents, prints the MCP client config to register, and ends with a doctor
smoke test:

```json
{
  "mcpServers": {
    "apple-mail": { "command": "email-mcp" }
  }
}
```

Grant the host app **Full Disk Access** (reading) and **Automation → Mail** (triage). Check anytime with `email-mcp doctor`.

## What it does

Every other Apple-Mail MCP drives AppleScript for both finding and acting. This one never does — and it shows:

| Operation | AppleScript-based MCPs | email-mcp |
|---|---|---|
| Search 300k messages | `whose` scan, seconds-to-timeout | direct SQLite Envelope Index, **milliseconds** |
| Address one message in a 72k mailbox | `whose message id is …` — **85.6 s** (measured) | by database id — **0.16 s** (measured, 535×) |
| Send mail | Mail.app compose — body renders **blank in Outlook** | own RFC-822 composer, plain+HTML, Outlook-safe |
| Schedule mail | — | frozen spool + launchd, Mail.app "Send Later" semantics |
| Bulk triage | one call per message | one reviewable plan, one batched apply, **verified** |
| Mutations | fire-and-forget | re-read Mail's own store, report confirmed vs pending |

### The hook — "file these 40 newsletters"

Mailbox management is **selection × disposition**, not a verb catalog:

```
triage_plan(from_addr="newsletter@", mailbox="Inbox",
            actions=[{"action": "mark_read"},
                     {"action": "move_to", "mailbox": "Archive/News"}])
→ {plan_id, count: 40, summary, messages: […]}     # nothing mutated — review it

triage_apply(plan_id)
→ {acted: 40, verified: 40, failures: [], pending: []}
```

The plan freezes the exact selection; apply runs ONE batched AppleScript addressing each message by its Envelope Index ROWID (Mail's object id *is* the ROWID — the keyed-lookup trick nobody else uses); verification re-reads the index until the mutations are confirmed. `delete` means Mail's own Trash — nothing is ever erased.

## MCP tools (19)

| Group | Tools |
|---|---|
| **Read** (8) | `search_emails` (full-body FTS) · `get_email` (view levels) · `get_emails_batch` · `get_thread` · `list_mailboxes` · `list_recent` · `get_attachment` · `refresh_mail` |
| **Send** (5) | `send_email` · `reply_email` (threaded, quoted) · `schedule_email` · `list_scheduled` · `cancel_scheduled` |
| **Triage** (5) | `triage_plan` · `triage_plan_delete` (segregated, capped) · `triage_apply` · `mailbox_create` · `mailbox_delete` |
| **Meta** (1) | `doctor` — FDA/Automation/Accessibility/identities/transports/dispatcher/spool/FTS diagnostics with fix-it strings |

Attachments both ways, size-budgeted. Reply threading via real `In-Reply-To`/`References`. Scheduling survives sleep — a message due while the lid was closed goes out on the first dispatcher tick after wake. `EMAIL_MCP_READ_ONLY=1` registers only the 10 tools that can move no mail and leave no durable trace.

### Search means the whole mailbox

Since v0.8 a local FTS5 sidecar (`~/.email-mcp/fts/`, derived state, rebuildable) indexes every locally-stored message body — Mail's own index only snippets ~36% of messages. Measured on a 305k-message store: **build 10m45s, 880 MB, ~190k bodies indexed**; searches stay in the same latency class (~0.4 s) and now surface body-only matches (flagged `body_match`) that snippet search structurally misses — one test query went from 0 hits to 20. New mail is absorbed by an inline incremental pass at search time (verified: one search catches a just-arrived message); `python -m email_mcp.fts --build` once, `--sync` for weekly hygiene.

## Identities

The From: address decides the transport. `~/.email-mcp/identities.toml`:

```toml
default = "cern"

[cern]                    # ssh_sendmail — RFC-822 piped to a remote sendmail
from_addr = "you@cern.ch"
driver    = "ssh_sendmail"
host      = "lxplus.cern.ch"

[gmail]                   # smtp — STARTTLS, secret never enters this file
from_addr = "you@gmail.com"
driver    = "smtp"
host      = "smtp.gmail.com"
op        = "op://Personal/gmail app password/password"   # 1Password ref
# keychain = "email-mcp-gmail"                            # …or macOS Keychain
```

Pick per send with `from_identity`; omit for the default. Each identity carries its own allowlist, Bcc-to-self, and Message-ID domain. Exchange identities can add `executor = "graph"`: scheduled mail is handed to Exchange as a deferred draft (`PidTagDeferredSendTime`) and **sends with the lid closed** — calibrated live: delivered at the deferred time to the second while the Mac slept (`docs/graph-calibration-2026-07-29.md`). The launchd spool remains the executor for everything else and the fallback when Graph declines. No file? Set the minimal env trio — `EMAIL_MCP_FROM_ADDR`, `EMAIL_MCP_SEND_HOST`, `EMAIL_MCP_SEND_USER` — and a single ssh-lane identity is synthesized from the environment. Reading needs no sending configuration at all. Diagnose everything (permissions, identities, every transport lane) with `python -m email_mcp.server --doctor`.

## Use cases

**Ask your mailbox questions.** "What did Stefan send me about the memo last week?" — search runs against Mail's own index at SQL speed, full filters (sender, mailbox, dates, unread, attachments), then `get_thread` reconstructs the conversation.

**Send as the right you.** Work mail over the CERN lane, personal over Gmail SMTP — one `from_identity` parameter. The composer builds clean multipart plain+HTML that renders everywhere (the AppleScript compose path that blanks in Outlook is the reason this project exists).

**Schedule like Send Later, but scriptable.** `schedule_email(send_at=…)` freezes the message — attachments embedded, identity recorded — and a launchd dispatcher delivers within a minute of due time (measured: 1–9 s typical), with retry/backoff and crash recovery.

**Triage at scale, safely.** Plan → review → apply → verify. Caps at 200 messages per plan (rejected, never truncated), per-message Message-ID recheck before mutating, per-message failures as data.

## The invariant

**AppleScript never touches message content.** Reads come from Mail's SQLite Envelope Index and `.emlx` files directly; outgoing mail is composed as RFC-822 from scratch; the Envelope Index is never opened writable. Mail.app is used for exactly two things: fetching new mail on request, and acting as the mutation actor for triage — addressed by database id, verified against the store afterward.

Every claim above with a number was measured on a live 305k-message store; the full calibration notes live in [`docs/triage-design.md`](docs/triage-design.md) and [`docs/transport-design.md`](docs/transport-design.md).

## Status

**v0.8.0** — 19 tools, 172 tests passing, live-calibrated end-to-end (full-body search on a 305k store, send, schedule+launchd, triage, both transport lanes, doctor). v0.8 removed the fine print: search covers bodies, destructive verbs have their own doors (`triage_plan_delete`, `EMAIL_MCP_READ_ONLY`), header injection is refused at compose, and the config defaults are person-clean — identities live in `~/.email-mcp/identities.toml`. The tool surface and the seam contracts (`sender._deliver_bytes`, `MailTransport`, `EmailSource`) are stable for the `0.8.x` line. Personal project, built for one Mac — the read layer and triage port anywhere Mail.app runs.

## See also

- [`docs/reference.md`](docs/reference.md) — full operational reference: every env var, safety guards, SSH bootstrap, spool internals
- [`docs/triage-design.md`](docs/triage-design.md) — the selection × disposition design + measured numbers
- [`docs/transport-design.md`](docs/transport-design.md) — the Identity × Driver design + the 2029 rationale
- [`docs/v0.8-concept.md`](docs/v0.8-concept.md) — "removing the asterisks": the v0.8 design narrative
- [`docs/landscape-2026-07.md`](docs/landscape-2026-07.md) — 40-server ecosystem survey
