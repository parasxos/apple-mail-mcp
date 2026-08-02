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

`create_draft(to, subject, body, cc?, in_reply_to?, from_identity?)`
→ `{ok, draft_id, message_id, to, cc, subject, folder, account}` — tool
#21, in the MUTATING set; READ_ONLY stays 11. *(As shipped 2026-08-02:
no `bcc` — a draft a human will edit carries no Bcc-to-self, and a user
bcc belongs to the send they will perform; no `local_*` fields — local
visibility follows Mail's own sync and is found via the returned
`message_id`, a scope cut from the panel's best-effort-poll suggestion.)*
Success is **verified against the authoritative store for the lane**
(Exchange, via the three-legged readback below — the original
Envelope-Index/ROWID wording here predated the panel's finding that
rowids are rewritten by sync). A draft that cannot be read back as ours,
in Drafts, unarmed, was not created.

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

**PROBE VERDICT — Lane A FAILED on stored fidelity (run 2026-08-01,
rowids 1322571/1322585 against the live store).** The scripted-save
draft stores a `multipart/alternative` whose **`text/plain` part is
EMPTY** and whose `text/html` part is wrapped in the founding-bug
signature (`Apple-Mail-URLShareWrapperClass` → `<blockquote type="cite">`
with style-reset attributes). Mail re-composes from its editor model, so
Lane A structurally cannot store our composer's bytes — the send-by-hand
half is moot; the fidelity bar is already missed in storage. Two Lane A
warts confirmed in passing: compose defaults to an arbitrary account
(the run landed under a forgotten mail.com identity), and the local
account spells its drafts mailbox `DRAFTS` (probe matcher now
case-insensitive). **Per this note's own rule, the movement shrinks: the
remaining choice is Lane B (IMAP APPEND — our bytes exactly, at the cost
of a new protocol + credential surface) or a documented "not possible
here". Decision: Paris's.**

## PANEL DECISION — Lane C: Graph MIME-create (2026-08-02)

A three-expert panel (Mail.app platform, protocols/provider APIs, concept
guardian) converged unanimously on a lane this note had not named:

> **Lane C — `POST /me/messages` with our composer's base64 MIME, via the
> Graph machinery `executor = "graph"` already owns.** It is the first
> third of `create_deferred_draft` (graph.py) with the deferred-time PATCH
> and the `/send` amputated: the send path with the transport removed.
> `Mail.ReadWrite` is already consented at CERN (calibration 2026-07-29,
> whose Phase-A verdict literally includes `mime_create: true`).

**The decision rule:** a draft may only be filed into the Drafts folder of
the mailbox that owns its From address, by a lane that can prove it landed
there; if the identity has no such lane, `create_draft` REFUSES — it never
files elsewhere, and it never falls back. (Unlike `schedule_email`'s
graph→launchd fallback, which is safe because both executors deliver the
same message: for a draft the location IS the artifact, and a fallback
that files elsewhere may publish the body to a third party — see
security-posture §2.12.)

**Lane A is closed permanently, twice over:** the fidelity failure above,
and a measured SECURITY finding — unmatched `sender` silently exfiltrates
the draft body to a third-party IMAP server with exit 0 (posture §2.12).
The panel also completed the founding bug's causal story: the
`blockquote type="cite"` wrapper is semantic, so Mail derives an empty
plain part at rest and a `> `-quoted one on send — blank-in-Outlook was
this all along. **Lane B is rejected:** on Exchange it duplicates a kept
promise (Graph OAuth + a second protocol into the same mailbox), its
Drafts-folder resolution is not portable (this store spells it four ways,
including `Πρόχειρα`), and it buys coverage nobody asked for.

**Corrections to this note's own contract:**
- `draft_id` is the **Graph message id**, never an Envelope Index ROWID —
  rowids are rewritten by server sync within minutes (measured). The
  cross-store join key is the composer-minted Message-ID
  (= Exchange `internetMessageId`); local visibility is reported
  best-effort (`local_*` fields, null until Mail syncs), never required.
- The no-transport invariant is pinned by an **AST call-graph
  reachability test** from the tool function, not by grep — graph.py
  legitimately contains `/send`.
- Acceptance readback, all three or failure: `isDraft: true`;
  `internetMessageId` == ours; `parentFolderId` == the well-known
  `drafts` folder.

**Coverage at v1:** CERN Exchange identities only (both current users);
hotmail = probe-then-enable later (`consumers` tenant, zero new code);
Gmail/IMAP/local = documented refusal with a fix string. The capability is
DECLARED per identity — `drafts = "graph"` beside `driver` and `executor`,
an enum so `"gmail"` stays addable — never inferred, never fallen back to.

**v1 excludes:** `send_draft` (the invariant is the feature),
`update_draft`/`list_drafts`/`delete_draft` (the client's UI; reads
already work), attachments (Graph single-request MIME is size-limited —
the parameter is absent, not ignored), any folder parameter (the seam
"file it elsewhere" would re-enter through), quoted reply history
(`in_reply_to` passes through as a header; the quote block is v1.1).

**Rollout:** R0 restore the lost `[cern.graph]` config + re-login + an
identity-binding check (`GET /me` vs `from_addr` — hardens scheduling
too); R1 the tool (#21, 10 mutating / 11 read-only / 21 total, contract +
freeze rows in the same change); R2 docs; R3 Camilla (two config lines +
one device login); R4 nothing until a real ask.

*R0 DONE 2026-08-02 (binding fence dab2b8f; config + live re-login,
bound to paris.moschovakos@cern.ch). R1 DONE 2026-08-02: tool #21 with
the three-legged readback, `drafts` identity capability, wizard
Exchange step (tenant `organizations` — probed live, no GUID needed),
`draft` audit event, call-graph invariant tests. R2 rode along
(reference.md "Drafts", contract §1/§3/§6 rows). Next: R3 = Camilla's
enable; manual acceptance (edit in OWA/phone, hand-send to Outlook,
verify rendering) still owed for the RC record.*

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
