# Drafts — intent handed back, never executed

*Concept note, 2026-08-01. Sixth in the series: `triage-design.md`,
`transport-design.md`, `v0.8-concept.md`, `v1-roadmap.md`,
`v0.11-clean-concept.md`. Status: DECISION PENDING — this note frames the
probe that decides the implementation; no code before the probe.*

## The ask

The tool's first user, unprompted, on day one: *"create a draft email …
it is very important you don't send the email, only write and save a
draft."* The session had no way to honor it — the surface is send / reply
/ schedule, all of which execute. The workaround was genuinely bad: a
Gmail draft under the wrong From identity plus a text file to paste.

That ask is a new **category**, not a new verb in an old one. Everything
mutating this tool ships follows *frozen intent → deterministic execution
→ verification*. A draft inverts the middle clause: it is intent
deliberately handed BACK to the human — an artifact in their mail
client's Drafts, theirs to edit, theirs to send. The tool's job ends at
composition.

## The invariant

> **A draft created by this tool cannot leave the machine by this tool's
> hand.** There is no promote-and-send path, no schedule-from-draft, no
> retry. `create_draft` composes and files; sending a draft is the mail
> client's business, under the human's finger.

This is what makes the feature safe to add at all: the allowlist
question, the transport question, and the permission-prompt question do
not arise, because nothing is transmitted. The audit event (`draft`,
additive per §8) records composition, not transmission.

## The surface (one tool, additive)

`create_draft(to, subject, body, cc?, bcc?, from_identity?, in_reply_to?)`
→ `{ok, draft_id, mailbox, account}` — tool #21, in the MUTATING set (it
writes to the mail store's Drafts); READ_ONLY stays 11. Success is
**verified against the store**: after filing, the Drafts mailbox is
re-read through the Envelope Index until the new message appears, and the
returned `draft_id` is its ROWID — the same id `get_email` accepts. A
draft you cannot immediately read back was not created.

Composition uses the tool's own RFC-822 composer (plain+HTML,
Outlook-safe) — never a scripted compose window.

## The decision the probe must make

Two candidate lanes, each with a known unknown:

**Lane A — AppleScript `make new outgoing message … save`.** Store-native:
the draft appears in Mail.app, OWA, and the phone, under the right
account. The unknown is the founding bug of this project: Mail's
*scripted-compose* path wraps bodies in a collapsed blockquote that
renders empty in Outlook — measured on **send**. Whether a *saved draft*'s
content suffers the same corruption (in Mail's own storage, and after a
human presses Send on it later) has never been probed. If drafts survive
round-trip clean, Lane A wins: smallest code, correct account placement.

**Lane B — IMAP APPEND to the account's Drafts folder.** The message is
our composer's bytes, byte-exact — no AppleScript anywhere — and appears
on every client. The unknown is cost: this opens a protocol + credential
surface the tool has never had (IMAP hosts, per-account auth, intranet
DNS questions all over again). Only worth it if Lane A's probe fails.

**Probe (tools/draft_probe.py, Lane A first):** create a draft with a
multi-paragraph plain+HTML body via AppleScript against a real account;
read the stored bytes back from the store; send it by hand from Mail.app
to an Outlook recipient; verdict on both stored fidelity and
post-send rendering. The Graph-executor precedent governs: **if the probe
refuses, the movement shrinks to a documented "not possible here"** and
the honest answer to a draft request stays "compose the text; paste it
yourself" — stated, not silently substituted.

## What is deliberately NOT here

- **No `send_draft`.** The invariant is the feature.
- **No draft editing/listing tools** — Drafts is the mail client's UI;
  `search_emails`/`get_email` already read the mailbox like any other.
- **No spool-state pseudo-draft.** A spool entry without a date is
  invisible to the user's clients — it fails the actual ask ("a draft I
  can open and edit"), and it would put a never-fires state into the
  dispatcher's vocabulary for no gain.

## Order of work (after Paris approves)

1. `tools/draft_probe.py` + a probe verdict recorded in this file.
2. Lane per verdict; contract rows (§1 tool #21, §6 `draft` event) and
   the derived-schema freeze row in the same change.
3. Policy tests: verified-in-store round trip; the invariant (no code
   path from a draft to any transport — provable by grep the way
   READ_ONLY is pinned lexically); identity → account routing.

The one-line version: **send executes intent; a draft returns it.**
