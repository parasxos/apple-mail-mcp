# email-mcp — full operational reference

Local MCP server that gives Claude Code access to Apple Mail.app on this Mac: **read** every message from Mail's own SQLite envelope database + `.emlx` files (no network, no creds, no new index), and **send** mail through the server's own delivery path. Sending is off by default for everyone but you — see [Sending mail](#sending-mail-send_email--reply_email).

## Install

Each MCP server gets its own venv (same pattern as the sibling `*-mcp` repos):

```bash
git clone https://github.com/parasxos/email-mcp && cd email-mcp
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
      "command": "/path/to/email-mcp/.venv/bin/email-mcp"
    }
  }
}
```

Restart Claude Code. Run `/mcp` — `apple-mail` should appear with nineteen tools: `search_emails`, `get_email`, `get_emails_batch`, `get_thread`, `list_mailboxes`, `list_recent`, `get_attachment`, `refresh_mail`, `list_scheduled`, `doctor`, `send_email`, `reply_email`, `schedule_email`, `cancel_scheduled`, `triage_plan`, `triage_plan_delete`, `triage_apply`, `mailbox_create`, `mailbox_delete`. (With `EMAIL_MCP_READ_ONLY=1` only the first ten — the read-side surface — register.)

To disable the self-only send guard (after you've trusted it — see [Sending mail](#sending-mail-send_email--reply_email)), add the flag to that server's `env` block:

```json
    "apple-mail": {
      "type": "stdio",
      "command": "/path/to/email-mcp/.venv/bin/email-mcp",
      "env": { "EMAIL_MCP_SEND_ALLOW_ALL": "1" }
    }
```

`~/.claude.json` is machine-local and untracked (it holds per-server secrets) — this snippet is the reproducible record of that setting.

## macOS Full-Disk-Access

`~/Library/Mail` is gated by macOS. Grant **Full Disk Access** to whichever terminal app starts Claude Code (Terminal / iTerm / VS Code / Cursor):

System Settings → Privacy & Security → Full Disk Access → toggle the app on.

You'll likely need to fully quit and relaunch the terminal app after toggling.

Without FDA the server returns a clear error from `list_mailboxes` on first call, so the failure mode is obvious.

## macOS Automation (needed only for `refresh_mail`)

`refresh_mail` nudges Mail.app to fetch new messages via AppleScript. This requires a second permission, separate from Full Disk Access:

System Settings → Privacy & Security → Automation → *your terminal app* → toggle **Mail** on.

The first call surfaces the macOS prompt; until granted, `refresh_mail` returns `ok: false` with `error_code: -1743` and a clear message pointing at Privacy & Security. The other read tools don't need this permission.

## Sending mail (`send_email` / `reply_email`)

Two write tools compose the outgoing message (plain + minimal HTML) and
deliver it. They deliberately do **not** go through Mail.app: its AppleScript
compose path wraps the body in a collapsed `Apple-Mail-URLShareWrapperClass`
blockquote that renders as an *empty* message in Outlook/Exchange (reproduced
across six compose variants — it's the OS, not the script). Delivery runs over
the sending identity's transport — out of the box, an existing SSH session to
a CERN host (see [Identities & transports](#identities--transports) for SMTP
and pipe lanes). A `Bcc`-to-self is added automatically so there's a
searchable record (delivery leaves no Exchange *Sent* copy).

**Self-only safety guard.** While `EMAIL_MCP_SEND_ALLOW_ALL` is off (the
default), every recipient must be on the allowlist — which defaults to *just
the From: address*. A mistake during the trial can therefore only reach you. A
blocked send returns `{ok: false, error}` naming the address; it never leaves
the machine. Flip `EMAIL_MCP_SEND_ALLOW_ALL=1` (or set an explicit
`EMAIL_MCP_SEND_ALLOWLIST`) once you trust it.

**Attachments.** Pass `attachments` as a list of local file paths. Each file
is attached with a MIME type guessed from its name (fallback
`application/octet-stream`); the plain+HTML body pair is wrapped in
`multipart/mixed`, exactly what normal clients emit. Directories are refused
(zip first). The total is capped at `EMAIL_MCP_MAX_ATTACH_MB` (default 20 MB
of file bytes — base64 adds ~33% on the wire) and an over-budget call fails
before anything is sent.

**SSH prerequisite.** A live SSH `ControlMaster` socket to the send host.
Establish it yourself (any SSH login with
`-o ControlMaster=yes -o ControlPath=~/.ssh/email-mcp-sock -o ControlPersist=4h`)
and the tools reuse it. Optionally point `EMAIL_MCP_SSH_BOOTSTRAP` (or the
identity's `bootstrap` param) at a script that re-establishes a cold socket
headlessly — the repo's `tools/lxplus_mail_master.sh` is a documented,
CERN-flavoured example (sources a secrets file, generates a TOTP). With no
bootstrap configured (the default), a cold socket is reported as a clear
transport error instead of being repaired.

End-to-end send test (real delivery, defaults to the safe self-only guard):

```bash
python3 -m email_mcp.server --send-test you@example.org
```

Prints `{ok, message_id, to, cc, bcc, ...}`; a non-self address prints the
guard refusal instead of sending.

## Identities & transports

Since v0.7.0 outgoing mail is routed by **identity**: the From: address
decides the transport. `~/.email-mcp/identities.toml` (override:
`EMAIL_MCP_IDENTITIES`) maps identity names to a From address, a transport
driver, and that driver's parameters; `send_email` / `reply_email` /
`schedule_email` take an optional `from_identity` to pick one (omit for the
file's `default`). Each identity carries its **own allowlist** — the
self-only guard is per-identity, so each identity's "self" is its own
address — plus its own Bcc-to-self target and Message-ID domain.

**No file needed for one identity:** with `identities.toml` absent, a single
identity named `default` is synthesized from the environment. The minimal
trio for the ssh lane is `EMAIL_MCP_FROM_ADDR`, `EMAIL_MCP_SEND_HOST` and
`EMAIL_MCP_SEND_USER` — set those three and sending works with no TOML file.
Since 0.8.0 the defaults are empty: with neither a file nor
`EMAIL_MCP_FROM_ADDR`, the send tools fail with a clear
`no sending identity configured` error naming both remedies. Reading needs
no sending configuration at all.

Three drivers ship, all stdlib:

| Driver | What it is |
|---|---|
| `ssh_sendmail` | the original production path: `sendmail` over an SSH `ControlMaster` session |
| `smtp` | `smtplib`, STARTTLS (or implicit TLS on port 465); password read from the macOS Keychain |
| `pipe` | pipe the raw message to a local command (`/usr/sbin/sendmail -t -i`, msmtp, …) |

Picking between the first two: corporate submission hosts are often
published only in the intranet's DNS (`smtp.cern.ch` is — it does not
resolve from home, VPN off). If your org's SMTP host is intranet-only and
you work off-site, `ssh_sendmail` through a login host you can always
reach is the sturdier lane; the smtp driver reports an unresolvable host
as a DNS failure, verbatim, so the two cases are easy to tell apart.

A complete `~/.email-mcp/identities.toml` (chmod 600). The known keys
(`from_addr`, `from_name`, `driver`, `allowlist`, `allow_all`, `bcc_self`)
configure the identity; every other key in a block is a parameter for its
driver:

```toml
default = "work"

[work]                                  # ssh_sendmail — sendmail on a remote host
from_addr = "you@example.org"
from_name = "Your Name"
driver    = "ssh_sendmail"
host      = "mailhost.example.org"
user      = "yourlogin"
socket    = "~/.ssh/email-mcp-sock"
# bootstrap = "~/bin/my-ssh-bootstrap.sh"  # optional headless socket re-establisher
# delivery_cmd = "/usr/sbin/sendmail"   # remote delivery command (default)

[gmail]                                 # smtp — app password in the Keychain
from_addr = "you@gmail.com"
from_name = "Your Name"
driver    = "smtp"
host      = "smtp.gmail.com"
port      = 587                         # 465 = implicit TLS, else STARTTLS
op        = "op://Personal/email-mcp gmail app password/password"  # 1Password secret ref
# keychain = "email-mcp-gmail"           # …or a macOS Keychain item; `op` wins if both
# username = "you@gmail.com"            # SMTP AUTH login (default: from_addr)

[local]                                 # pipe — whatever MTA you already run
from_addr = "you@example.com"
driver    = "pipe"
command   = "/usr/sbin/sendmail -t -i"
```

Store an SMTP password (for Gmail: an app password, which requires 2FA on
the Google account) in the Keychain once — `-s` must match the identity's
`keychain` value, `-a` its SMTP username:

```bash
security add-generic-password -s email-mcp-gmail -a you@gmail.com -w 'your-app-password'
```

Check every configured lane in one shot (part of the full diagnostic run):

```bash
python3 -m email_mcp.server --doctor
```

The `transports` check reports `{default, identities: {name: {ok, ...,
from_addr}}}` — an independent healthcheck per identity, so one broken
lane can't hide the others (`ok: false` on the ssh lane usually just means
a cold socket). `--transport-check` survives as a deprecated alias that
prints exactly that one check. To exercise a specific lane end-to-end:

```bash
python3 -m email_mcp.server --send-test you@gmail.com --from-identity gmail
```

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
cd email-mcp
pytest
```

The tests build a fake `~/Library/Mail/V10` tree in `tmp_path` — they don't read your real mail.

## Environment variables (all optional)

| Var | Default | Effect |
|---|---|---|
| `EMAIL_MCP_MAIL_DIR` | newest `~/Library/Mail/V*` | Override Mail.app base directory. |
| `EMAIL_MCP_SOURCE` | `apple` | Source adapter to load (Phase 2: gmail, imap, …). |
| `EMAIL_MCP_READ_ONLY` | `0` | `1` registers only the ten read-side tools — the widest trust envelope for demos, reviews and new users. |
| `EMAIL_MCP_MAX_BODY_BYTES` | `2000000` | Cap on body returned per `get_email`. |
| `EMAIL_MCP_ATTACH_DIR` | `$TMPDIR/email-mcp` | Where `get_attachment` writes blobs. |
| `EMAIL_MCP_FROM_ADDR` | *(empty)* | From: address for outgoing mail. Required for env-only sending (with `identities.toml` absent). |
| `EMAIL_MCP_FROM_NAME` | *(empty)* | From: display name. |
| `EMAIL_MCP_SEND_ALLOW_ALL` | `0` | `1` disables the allowlist (send to anyone). |
| `EMAIL_MCP_SEND_ALLOWLIST` | (From: addr) | Comma-separated addresses sending may reach while the guard is on. |
| `EMAIL_MCP_BCC_SELF` | `1` | Bcc the From: address on every send for a record. |
| `EMAIL_MCP_MAX_ATTACH_MB` | `20` | Total attachment budget per outgoing message (file bytes, pre-base64). |
| `EMAIL_MCP_SPOOL_DIR` | `~/.email-mcp/spool` | Scheduled-send spool root (created 0700). |
| `EMAIL_MCP_SEND_RETRIES` | `5` | Delivery attempts per scheduled message before parking in `failed/`. |
| `EMAIL_MCP_PLANS_DIR` | `~/.email-mcp/plans` | Triage plan store (created 0700). |
| `EMAIL_MCP_TRIAGE_MAX` | `200` | Message cap per triage plan (bigger selections rejected). |
| `EMAIL_MCP_TRIAGE_DELETE_MAX` | `50` | Tighter cap per delete plan (`triage_plan_delete`). |
| `EMAIL_MCP_TRIAGE_TTL` | `600` | Seconds a draft plan stays applicable. |
| `EMAIL_MCP_TRIAGE_TIMEOUT` | `0` (auto) | Batch AppleScript timeout; 0 = 30 + 0.6×N s, clamped 60–300. |
| `EMAIL_MCP_TRIAGE_VERIFY_POLLS` / `_INTERVAL` | `3` / `2.0` | Verification polling against the index. |
| `EMAIL_MCP_SEND_HOST` | *(empty)* | SSH host used for delivery (env-only ssh lane). |
| `EMAIL_MCP_SEND_USER` | *(empty)* | SSH user on that host. |
| `EMAIL_MCP_SSH_SOCKET` | `~/.ssh/email-mcp-sock` | ControlMaster socket path. |
| `EMAIL_MCP_DELIVERY_CMD` | `/usr/sbin/sendmail` | Remote delivery command. |
| `EMAIL_MCP_SSH_BOOTSTRAP` | *(empty)* | Optional command that re-establishes a cold socket headlessly (`tools/lxplus_mail_master.sh` is a documented example). |
| `EMAIL_MCP_IDENTITIES` | `~/.email-mcp/identities.toml` | Identity routing file (see [Identities & transports](#identities--transports)); absent → one identity synthesized from the env vars above (needs `EMAIL_MCP_FROM_ADDR`). |
| `EMAIL_MCP_FTS_DIR` | `~/.email-mcp/fts` | FTS body-index directory (created 0700 by build paths only). |
| `EMAIL_MCP_FTS_ENABLED` | `1` | `0` disables FTS body hits in `search_emails` (snippet-only search). |
| `EMAIL_MCP_FTS_MAX_HITS` | `2000` | Cap on FTS rowid hits folded into one search (newest kept). |
| `EMAIL_MCP_FTS_INLINE_BATCH` | `500` | Max documents the inline (search-time) incremental pass indexes. |
| `EMAIL_MCP_FTS_INLINE_BUDGET` | `2.0` | Wall-clock budget (s) for the inline incremental pass. |
| `EMAIL_MCP_FTS_DOC_CAP` | `524288` | Per-document cap (bytes) on extracted body text handed to the index. |
| `EMAIL_MCP_FTS_RECONCILE_DAYS` | `7` | How often `--sync` folds in a full rowid-set reconciliation. |
| `EMAIL_MCP_LOG_FILE` | `~/Library/Logs/email-mcp.log` | Debug log path; `off` disables file logging. |
| `EMAIL_MCP_LOG_LEVEL` | `INFO` | Log verbosity. |

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
| `send_email(to, subject, body, cc?, bcc?, attachments?, from_identity?)` | Compose and send. Comma-separated address strings. `attachments` = list of local file paths (each entry ONE path), attached with guessed MIME types; total capped at `EMAIL_MCP_MAX_ATTACH_MB` (default 20). `from_identity` picks the sending identity (see [Identities & transports](#identities--transports)). Auto Bcc-to-self. Self-only guard applies per identity. Returns `{ok, message_id, to, cc, bcc, subject, attachments}` or `{ok: false, error}`. |
| `schedule_email(to, subject, body, send_at, cc?, bcc?, attachments?, from_identity?)` | Compose + freeze now, deliver at `send_at` via the launchd dispatcher on the recorded identity's transport. Returns `{ok, id, send_at, message_id, ...}`. |
| `list_scheduled(state?, limit?)` | The "Send Later mailbox": pending / sending / sent / failed / cancelled, with errors and delivery stamps. |
| `cancel_scheduled(id)` | Cancel a pending scheduled message (sent/mid-flight cannot be recalled). |
| `triage_plan(filters..., actions)` | Stage a mailbox operation: same filters as `search_emails` + a list of dispositions. Mutates nothing; returns `{plan_id, count, summary, messages}` for review. |
| `triage_apply(plan_id)` | Execute a staged plan (one batched AppleScript, by-ROWID addressing) + verify against the index. Per-message failures are data. |
| `mailbox_create(account, path)` | Create a (nested) mailbox; idempotent. |
| `mailbox_delete(account, path)` | Delete an EMPTY mailbox; idempotent. Outcome decided by live re-probe (Mail's delete verb lies); escalates to UI scripting when the verb has no effect (needs Accessibility permission). |
| `reply_email(id, body, reply_all?, cc?, bcc?, include_history?, attachments?, from_identity?)` | Reply to message `id`, threading via In-Reply-To / References / `Re:` subject. Quotes the original below the reply (attribution + `>` block, HTML blockquote) like a normal client; `include_history=False` for a bare reply. Defaults to the original sender only; `reply_all=True` also Ccs the original To+Cc minus your own address. `attachments` as in `send_email`. |

## Scheduled send (`schedule_email` / `list_scheduled` / `cancel_scheduled`)

The MCP's "Send Later" — same semantics as Mail.app's native feature (which
has no automation API; verified against Mail.sdef), without touching Mail's
database:

- `schedule_email(..., send_at)` composes and **freezes** the full RFC-822
  now — recipients validated, allowlist enforced, attachments embedded,
  Bcc-to-self added — into `~/.email-mcp/spool/pending/` (mode 0700).
  Naive `send_at` means local time; explicit offsets respected.
- A **launchd agent** (`com.email-mcp.dispatcher`, every 60 s +
  RunAtLoad) delivers what is due over the same SSH path, bootstrapping the
  ControlMaster if cold. Worst-case delivery lag is ~two ticks (~2 min) past
  send_at — measured 81 s in fleet testing. Mac asleep at send time → the message goes out on
  the first pass after wake, like Mail.app's "send when opened".
- Failures retry with backoff (2/5/15/45/120 min, `EMAIL_MCP_SEND_RETRIES`
  attempts, default 5), then park in `failed/` with the error + a macOS
  notification. Overlapping dispatcher runs are double-send-safe (atomic
  manifest rename claims ownership).
- Authorization happens at **schedule time** (inside the MCP server, where
  your config lives); the dispatcher deliberately does not re-check — it
  runs under launchd's bare environment where the self-only default would
  block everything.
- Install once: `python -m email_mcp.dispatcher --install-launchd`
  (also `--uninstall-launchd`, `--status`; log at
  `~/.email-mcp/dispatcher.log`). Install and uninstall also boot out and
  remove any pre-v0.8 agent (`com.paris.email-mcp-dispatcher`), so
  upgrading never leaves two dispatchers ticking over the same spool.

## Triage (`triage_plan` / `triage_apply` / `mailbox_create` / `mailbox_delete`)

Mailbox management as **selection × disposition** (design + measurements:
`docs/triage-design.md`): SELECT messages via the same SQLite filters as
`search_emails` → freeze them + the dispositions into a reviewable **plan**
(nothing mutates) → `triage_apply` runs ONE batched AppleScript addressing
each message by its Envelope Index ROWID (keyed lookup, 0.16 s in a
72k-message mailbox — measured, vs 85.6 s for name-based scans) → the same
index verifies the mutations landed (write-through ≤2 s).

- Dispositions: `move_to` (same-account; target must exist), `mark_read`,
  `mark_unread`, `flag` (color 0-6), `unflag`, `delete` (Mail's own delete
  verb → that account's Trash; nothing is erased permanently). No `archive`
  verb — that's `move_to` with your archive mailbox.
- **Plan/apply is two calls by design** — review the returned plan before
  applying. Plans live in `~/.email-mcp/plans/` (0700), expire after 10 min,
  cap at 200 messages (larger selections are rejected, never truncated).
  Double-apply and concurrent applies are safe (atomic claim).
- Before mutating, each message's RFC Message-ID is re-checked against the
  plan — a message that moved or a recycled database id fails safe.
- Failures are per-message data (`failures[]`), not call errors; `pending[]`
  means the local index hasn't confirmed within the poll window (normal for
  Exchange — it syncs).
- ⚠ `mailbox_create` on **Exchange (EWS)** accounts: AppleScript-created
  folders may not persist server-side (observed live: Exchange silently
  reverted every move into one). Create Exchange folders in Mail.app/OWA;
  `mailbox_create` is reliable for local and plain-IMAP accounts.
- Requires Mail.app Automation permission (same as `refresh_mail`). The
  Envelope Index itself is **never opened writable** — all mutations go
  through Mail.app, which owns server sync.

## Phase-2 hooks (not implemented yet)

- **More sources**: implement `EmailSource` in `email_mcp/sources/`, register in `email_mcp/sources/__init__.py::_REGISTRY`, select via `EMAIL_MCP_SOURCE`.
- ~~**FTS5 sidecar**~~ shipped in v0.8.0: `python -m email_mcp.fts --build` indexes `.emlx` bodies into a local FTS5 db; `search_emails` folds body hits in transparently (see the `EMAIL_MCP_FTS_*` vars above).
- **More write tools** (mark-read, move): future. `send_email` / `reply_email` shipped in v0.2.0; reply history-quoting in v0.3.0; outgoing attachments in v0.4.0; scheduled send in v0.5.0; triage (mailbox management) in v0.6.0; identity transports in v0.7.0.

## Safety notes

- Opens the Envelope Index with `?mode=ro` (WAL-safe) — safe to run while Mail.app is active.
- Never writes to `~/Library/Mail`. Sending never touches the Mail store.
- Triage mutations go through Mail.app's AppleScript interface; the Envelope Index is never opened writable.
- `get_attachment` writes only to the configurable `EMAIL_MCP_ATTACH_DIR`.
- Body and attachment size are capped by `EMAIL_MCP_MAX_BODY_BYTES`.
- Sending is the only outward action: guarded by the self-only allowlist (default), a mandatory non-empty `to`/`subject`/`body`, and Bcc-to-self for an audit trail.
