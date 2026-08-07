<div align="center">

# ✉️ email-mcp

### Your Apple Mail, fully agent-operable.

Connect Claude to Mail.app on your Mac and your mailbox stops being a chore
and starts being something you can **ask** and **delegate to** — find anything
in seconds, file hundreds of messages safely, send polished mail as any of
your addresses, and schedule delivery that fires even while the laptop sleeps.

![ci](https://github.com/parasxos/email-mcp/actions/workflows/ci.yml/badge.svg)
![tools](https://img.shields.io/badge/MCP%20tools-21-brightgreen)
![platform](https://img.shields.io/badge/platform-macOS%20%2B%20Mail.app-orange)
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
frozen in full — attachments, identity, exact text — and delivered within
seconds of due time. On Exchange, delivery is handed to the server itself:
calibrated live, a message left on a sleeping Mac went out at the deferred
time **to the second, lid closed**.

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
| ⏰ Schedule mail | — | survives sleep, reboot, even a closed lid |
| 🗂️ Bulk triage | one call per message, fire-and-forget | one reviewed plan, one apply, **verified** |

Every number above was measured on a live 305,000-message mailbox.

## 🛡️ Built to be trusted

- ✅ **Plan → review → apply → verify.** Bulk actions are frozen into a plan
  you can read before anything happens; the outcome is confirmed against
  Mail's own store afterward — never assumed.
- 🗑️ **Nothing is ever erased.** "Delete" files into Mail's Trash, and
  destructive plans have their own separate, capped door.
- 👓 **Read-only mode.** Set `EMAIL_MCP_READ_ONLY=1` and only the 11 tools
  that can move no mail even exist in the session.
- 🧾 **A local audit ledger.** Every send, schedule, cancellation and triage
  run is recorded — your mail history has a paper trail, on your disk only.
- 🔒 **Nothing leaves your Mac** except the mail you send. No cloud service in
  between — the only server ever contacted is your own mail provider — and
  secrets stay in the macOS Keychain or 1Password, never in a config file.
- 📜 **A written contract.** Since v1.0 every tool's shapes, error codes and
  caps are frozen — additive changes only, held in place by 771 automated
  tests.
- 🩺 **Self-diagnosing.** `email-mcp doctor` checks every permission,
  identity and transport lane and tells you the exact fix for anything red.

## 🚀 Quick start

```bash
pipx install git+https://github.com/parasxos/email-mcp
email-mcp setup
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

Grant **Automation → Mail** when triage first asks for it. Check the whole
installation anytime with `email-mcp doctor`.

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

**v1.2** · 21 tools · 771 tests · wire contract frozen since v1.0
Live-calibrated end-to-end on a 305k-message store.

Built for one Mac — and for anyone else whose Mac runs Mail.app.

</div>
