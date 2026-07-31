# email-mcp

**Your Apple Mail, fully agent-operable.**
An MCP server that gives Claude direct access to Mail.app on your Mac — read at SQL speed, send Outlook-safe mail as any of your identities, schedule delivery, and triage whole mailboxes — without ever letting AppleScript near a message body.

![version](https://img.shields.io/badge/version-v0.11.0-blue)
![tools](https://img.shields.io/badge/MCP%20tools-20-brightgreen)
![tests](https://img.shields.io/badge/tests-617%20passing-success)
![platform](https://img.shields.io/badge/platform-macOS%20%2B%20Mail.app-orange)

## Install

```bash
pipx install git+https://github.com/parasxos/email-mcp
email-mcp setup
```

The setup wizard checks permissions (and tells you exactly which Settings pane to open), lays down `~/.email-mcp` with tight modes, walks you through identities (secret values go to the macOS Keychain or 1Password — never to disk), offers the launchd agents and the FTS index, and prints the exact registration JSON for your MCP client, absolute path included:

```json
{
  "mcpServers": {
    "apple-mail": { "command": "/absolute/path/to/email-mcp" }
  }
}
```

Grant the host app **Full Disk Access** (reading) and **Automation → Mail** (triage) when the wizard asks; `email-mcp doctor` re-verifies everything later.

**The name.** The repo and the Python distribution are `email-mcp`; the MCP server registers as `apple-mail` (existing registrations keep working — bare `email-mcp` still serves MCP on stdio). Both PyPI and npm host *unrelated* projects called `email-mcp` by other authors (a multi-account IMAP/SMTP MCP and a Node email tool respectively), so a plain `pipx install email-mcp` off PyPI would fetch someone else's package — install from the git URL above.

### Development install

```bash
git clone https://github.com/parasxos/email-mcp && cd email-mcp
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Lifecycle

One CLI covers the whole life of the install:

| Command | Does |
|---|---|
| `email-mcp setup` | the interactive wizard above; scriptable with `--yes`, `--answers FILE`, `--read-only` |
| `email-mcp doctor [--fix] [--json]` | full diagnostics with fix-it strings; `--fix` runs a frozen whitelist of safe repairs (recreate/re-tighten state dirs, rename a corrupt ledger aside, re-bootstrap launchd agents, recover stranded sends, finalise stale plan claims — never credentials, never index builds, never deletion), each repair leaving a `doctor_fix` audit event; the exit code reflects the after-fix re-check |
| `email-mcp update` | after upgrading the package: runs pending state migrations, re-renders launchd plists that drifted (a moved venv), stamps `meta.json`, re-runs doctor |
| `email-mcp uninstall [--purge]` | removes the launchd agents and Graph token caches, keeps your data; `--purge` (typed confirmation) also deletes `~/.email-mcp` — hardcoded path, symlinks refused, env-overridden dirs listed but never followed — and prints the Keychain cleanup commands. `~/Library/Mail` is never touched, ever |

`email-mcp version` reports package + state version; `email-mcp audit|fts|graph|dispatcher` expose the maintenance CLIs; legacy `--doctor`/`--selftest` flags still forward.

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

## MCP tools (20)

| Group | Tools |
|---|---|
| **Read** (8) | `search_emails` (full-body FTS) · `get_email` (view levels) · `get_emails_batch` · `get_thread` · `list_mailboxes` · `list_recent` · `get_attachment` · `refresh_mail` |
| **Send** (5) | `send_email` · `reply_email` (threaded, quoted) · `schedule_email` · `list_scheduled` · `cancel_scheduled` |
| **Triage** (5) | `triage_plan` · `triage_plan_delete` (segregated, capped) · `triage_apply` · `mailbox_create` · `mailbox_delete` |
| **Meta** (2) | `doctor` — FDA/Automation/Accessibility/identities/transports/dispatcher/spool/FTS/ledger diagnostics with fix-it strings · `audit` — query the audit ledger |

Attachments both ways, size-budgeted. Reply threading via real `In-Reply-To`/`References`. Scheduling survives sleep — a message due while the lid was closed goes out on the first dispatcher tick after wake. `EMAIL_MCP_READ_ONLY=1` registers only the 11 tools that can move no mail and leave no durable trace. Every tool speaks one envelope — `{ok: true, …}` or `{ok: false, code, error, fix?}` with a machine-dispatchable code from a single namespace; no exception ever reaches the wire. The whole surface is written down and frozen by snapshot: [`docs/v1-contract.md`](docs/v1-contract.md).

### Search means the whole mailbox

Since v0.8 a local FTS5 sidecar (`~/.email-mcp/fts/`, derived state, rebuildable) indexes every locally-stored message body — Mail's own index only snippets ~36% of messages. Measured on a 305k-message store: **build 10m45s, 880 MB, ~190k bodies indexed**; searches stay in the same latency class (~0.4 s) and now surface body-only matches (flagged `body_match`) that snippet search structurally misses — one test query went from 0 hits to 20. New mail is absorbed by an inline incremental pass at search time (verified: one search catches a just-arrived message); `email-mcp fts --build` once, `--sync` for weekly hygiene.

### Every mutation leaves a receipt

Since v0.10 an append-only audit ledger (`~/.email-mcp/audit/YYYY-MM.jsonl`, 0600, **never** message bodies) records ONE event per mutation — send, reply, schedule, deliver, cancel, triage apply, mailbox create/delete — with an operation id that threads server and dispatcher events for the same message. "What changed yesterday?" is one query: the `audit` tool from your client, or `email-mcp audit --since 2026-07-29` (`--tail`, `--event`, `--op`, …) from the shell.

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

Pick per send with `from_identity`; omit for the default. Each identity carries its own allowlist, Bcc-to-self, and Message-ID domain. Exchange identities can add `executor = "graph"`: scheduled mail is handed to Exchange as a deferred draft (`PidTagDeferredSendTime`) and **sends with the lid closed** — calibrated live: delivered at the deferred time to the second while the Mac slept (`docs/graph-calibration-2026-07-29.md`). The launchd spool remains the executor for everything else and the fallback when Graph declines. No file? Set the minimal env trio — `EMAIL_MCP_FROM_ADDR`, `EMAIL_MCP_SEND_HOST`, `EMAIL_MCP_SEND_USER` — and a single ssh-lane identity is synthesized from the environment. Reading needs no sending configuration at all. Diagnose everything (permissions, identities, every transport lane) with `email-mcp doctor`.

## Use cases

**Ask your mailbox questions.** "What did Stefan send me about the memo last week?" — search runs against Mail's own index at SQL speed, full filters (sender, mailbox, dates, unread, attachments), then `get_thread` reconstructs the conversation.

**Send as the right you.** Work mail over the CERN lane, personal over Gmail SMTP — one `from_identity` parameter. The composer builds clean multipart plain+HTML that renders everywhere (the AppleScript compose path that blanks in Outlook is the reason this project exists).

**Schedule like Send Later, but scriptable.** `schedule_email(send_at=…)` freezes the message — attachments embedded, identity recorded — and a launchd dispatcher delivers within a minute of due time (measured: 1–9 s typical), with retry/backoff and crash recovery.

**Triage at scale, safely.** Plan → review → apply → verify. Caps at 200 messages per plan (rejected, never truncated), per-message Message-ID recheck before mutating, per-message failures as data.

## The invariant

**AppleScript never touches message content.** Reads come from Mail's SQLite Envelope Index and `.emlx` files directly; outgoing mail is composed as RFC-822 from scratch; the Envelope Index is never opened writable. Mail.app is used for exactly two things: fetching new mail on request, and acting as the mutation actor for triage — addressed by database id, verified against the store afterward.

Every claim above with a number was measured on a live 305k-message store; the full calibration notes live in [`docs/triage-design.md`](docs/triage-design.md) and [`docs/transport-design.md`](docs/transport-design.md).

## Status

**v0.11.0** — 20 tools, 617 tests passing, live-calibrated end-to-end (full-body search on a 305k store, send, schedule via launchd and Exchange Graph, triage, doctor). v0.10 put the promises in writing — [`docs/v1-contract.md`](docs/v1-contract.md) — and added the audit ledger; v0.11 finishes wire conformance (every tool speaks the coded envelope, output surface frozen by snapshot) and adds the lifecycle CLI (`setup` / `doctor --fix` / `update` / `uninstall`). The contract is additive-only from v1.0: codes and event types only grow, breaking means v2. Personal project, built for one Mac — the read layer and triage port anywhere Mail.app runs.

## See also

- [`docs/v1-contract.md`](docs/v1-contract.md) — the binding surface: envelopes, the error-code namespace, idempotency, caps, audit event schema
- [`docs/reference.md`](docs/reference.md) — full operational reference: every env var, safety guards, SSH bootstrap, spool internals
- [`docs/triage-design.md`](docs/triage-design.md) — the selection × disposition design + measured numbers
- [`docs/transport-design.md`](docs/transport-design.md) — the Identity × Driver design + the 2029 rationale
- [`docs/v0.8-concept.md`](docs/v0.8-concept.md) — "removing the asterisks": the v0.8 design narrative
- [`docs/landscape-2026-07.md`](docs/landscape-2026-07.md) — 40-server ecosystem survey
