<div align="center">

# ✉️ email-mcp

### Your Apple Mail, fully agent-operable.

Connect your MCP client to Mail.app and your mailbox becomes something you can
**ask, search and delegate to** — find anything in seconds, file hundreds of
messages through a reviewed plan, send polished mail as the right identity,
and let Exchange deliver scheduled messages even while your Mac is asleep.

![ci](https://github.com/parasxos/email-mcp/actions/workflows/ci.yml/badge.svg)
![tools](https://img.shields.io/badge/MCP%20tools-21-brightgreen)
![platform](https://img.shields.io/badge/platform-macOS%20%2B%20Mail.app-orange)
![python](https://img.shields.io/badge/Python-3.11%E2%80%933.14-blue)
![mcp](https://img.shields.io/badge/MCP%20SDK-1.x%20%2B%202.x-purple)
![contract](https://img.shields.io/badge/wire%20contract-frozen%20v1-blue)

</div>

---

## ✨ What you can do

🔍 **Ask your mailbox questions.** *"What did Stefan send me about the memo
last week?"* Search runs at database speed — sender, mailbox, dates, unread,
attachments — and reconstructs whole conversations.

🕳️ **Find what Mail itself can't.** Mail's built-in search only skims the
first line of most messages. email-mcp indexes every message **body** on your
Mac — and for Exchange accounts it even fetches the bodies Mail never
downloaded, straight from your own mailbox on the server. Queries that
returned nothing return twenty.

🎭 **Send as the right you.** Work mail through the work lane, personal
through Gmail — one parameter picks the identity. Every message is composed
from scratch as clean, standards-correct email that renders everywhere,
*including Outlook* (the AppleScript compose path that arrives blank in
Outlook is the reason this project exists).

⏰ **Schedule like "Send Later", but scriptable.** A scheduled message is
frozen in full — attachments, identity, exact text. Exchange can execute it
server-side at the requested time, lid closed; other providers use a local
background sender and deliver on its next pass (or just after the Mac wakes).

🗂️ **Triage at scale, without fear.** *"File these 40 newsletters"* becomes a
reviewable plan: nothing moves until it is approved, every message is
re-checked before it is touched, and the result is verified against Mail's
own records afterward. Delete means Mail's Trash — nothing is ever erased.

📝 **Draft where your drafts live.** Compose into your real Exchange Drafts
folder, ready to open in Outlook or OWA — created, never auto-sent.

## ⚡ Why it's different

Every other Apple-Mail MCP drives AppleScript for both finding and acting.
This one doesn't — and it shows:

| Operation | AppleScript-based MCPs | email-mcp |
|---|---|---|
| 🔍 Search 300k messages | seconds-to-timeout | **milliseconds** |
| 🎯 Address one message in a 72k mailbox | **85.6 s** (measured) | **0.16 s** — 535× faster |
| ✉️ Send mail | body renders **blank in Outlook** | renders everywhere, plain+HTML |
| ⏰ Schedule mail | — | server-side on Exchange; reliable local queue everywhere else |
| 🗂️ Bulk triage | one call per message, fire-and-forget | one reviewed plan, one apply, **verified** |

Every number above was measured on a live 305,000-message mailbox.

## 🛡️ Built to be trusted

- ✅ **Plan → review → apply → verify.** Bulk actions are frozen into a plan
  you can read before anything happens; the outcome is confirmed against
  Mail's own store afterward — never assumed.
- 🗑️ **Nothing is ever erased.** "Delete" files into Mail's Trash, and
  destructive plans have their own separate, capped door.
- 👓 **Read-only mail mode.** Set `EMAIL_MCP_READ_ONLY=1` and only the 11
  non-mutating mail tools exist in the session. Search may still maintain its
  local body index, and attachment retrieval writes the requested file to the
  configured temporary directory.
- 💾 **A crash-safe scheduled queue.** Manifest updates are flushed and
  atomically replaced, so an interrupted rewrite keeps the last valid record.
  If a file is damaged independently, diagnostics name it instead of claiming
  the queue is empty, while healthy scheduled messages keep moving.
- 🧾 **A local, best-effort activity ledger.** Sends, schedules,
  cancellations and triage runs are recorded without making an unwritable log
  block mail. For reconciliation, the message itself, its Message-ID and its
  scheduled record remain authoritative.
- 🔒 **No third-party mail relay.** Mail content stays local except for mail
  you send and optional access to your own provider for Exchange/IMAP body
  backfill, drafts and server-side scheduling. SMTP passwords stay in the
  macOS Keychain or 1Password; Microsoft OAuth tokens live in a private 0600
  cache under `~/.email-mcp/graph/`.
- 📜 **A written contract.** Since v1.0 every tool's shapes, error codes and
  caps evolve additively, held in place by 800+ automated tests.
- 🤝 **Clear to every MCP client.** All 21 tools identify what they do, explain
  every input, and declare whether they read, change or can remove data. Newer
  clients receive structured results; older clients keep the same JSON text.
  Both the maintained MCP 1.x line and current MCP 2.x are tested.
- 🧱 **Built to evolve without breaking your workflow.** Email rules are
  isolated from MCP, Mail.app, Exchange, delivery, and local storage. Provider
  or SDK changes stay at the edge while the 21-tool contract remains stable.
  The dependency rules are enforced in CI and explained in the
  [architecture guide](https://github.com/parasxos/email-mcp/blob/main/docs/architecture.md).
- 📦 **Releases you can verify.** Every tagged release is built and installed
  in a clean environment before publishing. GitHub includes the wheel, source
  archive, SHA-256 checksums and signed build provenance—not just source code.
- 🩺 **Self-diagnosing.** `email-mcp status` gives one readable readiness,
  scheduling and recovery screen. `email-mcp doctor` provides the complete
  diagnostic detail and an exact fix for anything red.

## 🚀 Quick start

```bash
pipx install git+https://github.com/parasxos/email-mcp
email-mcp setup
email-mcp status
```

Before running `setup`, grant your terminal app **Full Disk Access**
(System Settings → Privacy & Security → Full Disk Access) — that is how
reading stays fast and local. There is no pop-up for this one; it is Apple's
one manual toggle, and `setup` walks you to the exact pane if it finds it
missing.

`setup` asks everything in plain words (bare Enter accepts the recommended
answer), offers a sending identity, builds the body-search index, verifies
the nightly refresh actually runs, and ends by printing the one block you
paste into your MCP client:

```json
{
  "mcpServers": {
    "apple-mail": { "command": "email-mcp" }
  }
}
```

Setup ends with a clear **ready** verdict or numbered recovery steps. Grant
**Automation → Mail** when triage first asks for it. Check the installation,
the next scheduled message, and failed scheduled sends anytime with
`email-mcp status`; use `email-mcp doctor` for the full technical detail.

> 💡 *New to the terminal?* Three things that look wrong and aren't:
> `brew install pipx` wants a typed `y` (Enter alone is rejected);
> `pipx ensurepath` may print a ⚠️ — the "pipx is ready to go!" line after it
> is the verdict; and after `ensurepath`, close and reopen the terminal once
> so `email-mcp` is found.

## 🧰 The 21 tools

| Group | Tools |
|---|---|
| 🔍 **Read** (8) | `search_emails` (full-body search) · `get_email` · `get_emails_batch` · `get_thread` · `list_mailboxes` · `list_recent` · `get_attachment` · `refresh_mail` |
| ✉️ **Send** (6) | `send_email` · `reply_email` (threaded, quoted) · `create_draft` · `schedule_email` · `list_scheduled` · `cancel_scheduled` |
| 🗂️ **Triage** (5) | `triage_plan` · `triage_plan_delete` · `triage_apply` · `mailbox_create` · `mailbox_delete` |
| 🩺 **Meta** (2) | `doctor` (full diagnostics with fix-it strings) · `audit` (the local ledger) |

Attachments both ways, size-budgeted. Replies thread correctly in every
client. Scheduling survives sleep — a message due while the lid was closed
goes out on the first tick after wake, or exactly on time via Exchange.

## 🎭 Your addresses, your lanes

The From: address decides how mail travels. `~/.email-mcp/identities.toml`:

```toml
default = "work"

[work]                    # sent through a host you already trust, over SSH
from_addr = "you@cern.ch"
driver    = "ssh_sendmail"
host      = "lxplus.cern.ch"

[gmail]                   # classic SMTP — the app password stays in 1Password
from_addr = "you@gmail.com"
driver    = "smtp"
host      = "smtp.gmail.com"
op        = "op://Personal/gmail app password/password"
```

Exchange identities can add one sign-in to unlock the extras: drafts filed in
your real Drafts folder, and scheduled sends executed by the server itself —
lid closed, Mac asleep. `setup` offers it in one plain question. Reading
needs no sending configuration at all.

---

<div align="center">

**v1.3** · 21 tools · 800+ tests · additive wire contract since v1.0
Live-calibrated end-to-end on a 305k-message store.

Built for one Mac — and for anyone else whose Mac runs Mail.app.

</div>
