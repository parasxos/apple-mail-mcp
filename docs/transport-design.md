# Identity × Driver — portability as a seam, not a feature

*Design note, 2026-07-28. Status: concept, measured and evaluated; not yet
implemented. Companion to `docs/triage-design.md`.*

## The problem, stated independently

The scorecard says "portability: 2/5" and the reflex reading is "add SMTP
support with a config file" — which is how the portable competitors do it
(sweetrb, s-morgan-jeffries: optional SMTP/IMAP + keychain credentials).
That's bolt-on thinking. The independent question is: **what exactly is
non-portable, and why?**

Measured on the codebase (2026-07-28):

- The ENTIRE non-portable surface is **six plumbing functions in
  `sender.py`** (`_ssh_base`, `_socket_alive`, `_kill_master`,
  `_bootstrap_master`, `_deliver`, `_deliver_bytes`) consumed through
  **one seam** — `_deliver_bytes(raw: bytes)` — with **two callers**
  (immediate send, spool dispatcher), plus **eight Paris-flavoured config
  defaults** (from-addr/name, SSH host/user/socket, bootstrap path).
- Everything else is already generic macOS: the read layer, composer,
  spool, dispatcher logic, triage, launchd install.

So portability is not a feature to add. It is a seam to name. And there is
an architectural tell: the READ side got its abstraction on day one
(`EmailSource` is a Protocol with a registry — "Phase 2: gmail, imap" is
written in the source). The WRITE side never got one. **Portability is
finishing the architecture's own sentence.**

## Why this matters beyond aesthetics: the 2029 clock

The current transport is `pmoschov@lxplus.cern.ch` + CERN 2FA. That is a
**wasting asset with a hard expiry**: CERN affiliation ends and lxplus dies
with it (funding window closes 2029-12-31). The day that happens, send,
reply, scheduled send, and the whole spool go dark — while the read layer,
triage, and every other part keep working. Portability here is not
marketplace vanity; it is the continuity plan for the tool's owner.

Second-order wins: sending as other identities TODAY (gmail/hotmail
addresses exist in this very Mail store), and strangers cloning the repo
without cosplaying as Paris.

## The concept: Identity × Driver

Two small ideas, composed:

**1. `MailTransport` protocol** — the write-side mirror of `EmailSource`:

    class MailTransport(Protocol):
        name: str
        def deliver(self, raw: bytes) -> None: ...   # raises SendError
        def healthcheck(self) -> dict: ...           # for --selftest

Drivers, day one (all stdlib, zero new dependencies — both patterns proven
live today):

| Driver | What it is | Proof |
|---|---|---|
| `ssh-sendmail` | today's path, de-Parised: any host/user/socket/bootstrap-cmd | in production since v0.2.0 |
| `smtp` | `smtplib` + STARTTLS/SSL, credentials from the macOS Keychain | STARTTLS+AUTH handshake verified against smtp.gmail.com AND smtp.mail.me.com; `security` CLI round-trip verified |
| `pipe` | pipe to a local command (`/usr/sbin/sendmail -t -i`, msmtp, …) | trivial subprocess; the ssh driver minus ssh |

**2. Identities** — the routing rule: *the From: address decides the
transport.* One file, `~/.email-mcp/identities.toml` (stdlib `tomllib`):

    default = "cern"

    [cern]
    from_addr = "paris.moschovakos@cern.ch"
    from_name = "Paris Moschovakos"
    driver    = "ssh-sendmail"
    host      = "lxplus.cern.ch"
    user      = "pmoschov"
    socket    = "~/.ssh/sock-lxplus-mail"
    bootstrap = "…/lxplus_mail_master.sh"

    [gmail]
    from_addr = "parasxos@gmail.com"
    from_name = "Paris Moschovakos"
    driver    = "smtp"
    host      = "smtp.gmail.com"
    port      = 587
    keychain  = "email-mcp-gmail"     # item name; secret lives in Keychain only

`send_email` / `reply_email` / `schedule_email` gain one optional
parameter: `from_identity` (default = the file's `default`). Everything an
identity implies follows from it: From: header, Message-ID domain,
Bcc-to-self target, **per-identity allowlist** (the self-only guard becomes
per-identity — each identity's "self" is its own from_addr), and the
driver. Tool surface stays at sixteen; one optional parameter is the whole
API change.

**Spool compatibility:** the manifest gains an `identity` field; the frozen
`.eml` is unchanged; the dispatcher resolves identity → driver at fire
time. Manifests without the field mean the legacy default — old spool
entries keep working.

**Back-compatibility (the migration IS the null case):** no
`identities.toml` → synthesize a single identity from today's env/defaults.
Paris's setup keeps working with zero changes; `config.py` loses its
personal defaults to the (gitignored-by-location) identity file, and the
repo becomes clean of any person.

## Alternatives evaluated

| Approach | Verdict |
|---|---|
| Status quo + "it's personal" docs | Fails the 2029 clock; fails the marketplace precedent (speak, session-rename are published) |
| Replace SSH with SMTP-only | Loses the working CERN path (no clean external SMTP submission for CERN; the SSH path exists for a reason) |
| Full provider abstraction (OAuth/XOAUTH2, Gmail API, Graph) | The enterprise trap: token refresh daemons, app registrations, hundreds of lines of auth for zero additional capability — app passwords already work for Gmail/iCloud. OAuth can be a *driver later* if ever needed; it must not be the architecture |
| Local relay daemon (postfix/msmtp config) | Pushes complexity onto the user's system; the `pipe` driver covers whoever already has one |
| **Identity × Driver (chosen)** | ~20-line protocol, 3 stdlib drivers, native Keychain secrets, one optional parameter, read/write symmetry — and each edge independently replaceable |

## Deliberately out of scope (the boundary is part of the design)

- **Non-macOS.** This is an *Apple Mail* MCP — the read layer IS Mail's
  store, triage IS Mail automation. Portability means: any Mac, any user,
  any mail provider, any decade. Not Linux.
- **OAuth flows.** See above; a future driver, never a prerequisite.
- **Localization of the UI-scripting tier** (`mailbox_delete` tier 2
  assumes English menu titles) — documented edge, orthogonal to transport.
- **Multi-source reading** — `EmailSource` already has the registry; not
  this design's job.

## Scalability

N identities × M drivers, all additive; adding a driver is one class + one
registry line, adding an identity is a TOML block. Per-identity allowlists
keep the trial-safety story intact per address. `healthcheck()` per driver
extends `--selftest` into a full transport diagnostic (`socket alive`,
`SMTP auth ok`, …) — the new-Mac onboarding becomes one command.

## Effort estimate

~200 lines: protocol + registry (~30), smtp driver (~60), pipe driver
(~20), ssh driver extraction (move, not write), identity loader +
keychain reader (~60), parameter plumbing (~30). Tests mirror the house
pattern (fake transport, fake keychain via env override). One session.

## Risks

- Keychain prompts: first `security find-generic-password` from a new
  binary may prompt; document `-w` + "Always Allow" once.
- Gmail app passwords require 2FA enabled on the Google account (true for
  this user; documented for strangers).
- Two transports = two failure vocabularies; `SendError` messages must
  carry the identity + driver name so a failed send names its lane.
