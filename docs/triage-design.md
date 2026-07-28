# Triage — mailbox management as selection × disposition

*Design note, 2026-07-28. Status: implemented in v0.6.0; live-calibrated — see addendum at the end.*

## The problem, stated independently

Every Apple-Mail MCP in the field (sweetrb, s-morgan-jeffries, attilagyorffy,
marius-cetanas, chrimerss) treats mailbox management as a **verb catalog**:
10–50 imperative tools (move, flag, mark-read, delete, batch-move, …), each a
separate AppleScript round-trip that both *finds* and *acts*. Finding via
AppleScript is why they are slow (`whose` clauses scan; documented 1–5 s per
query, timeouts on large mailboxes) and why the better ones grow IMAP
fallbacks, keychain config, and injection sanitizers.

But the user's actual job is almost never "move message X". It is **triage**:
*a selection of messages gets a disposition* — "file these 40 newsletters",
"flag everything from Ciaffi this month", "archive what I've read from JIRA".
Selection × disposition is the whole domain. The right shape is one primitive,
not a verb catalog:

    triage(selection, disposition)   —   like SQL's UPDATE … WHERE,
                                          like find -exec.

## The architectural unlock nobody else has

This MCP already reads Mail's own SQLite Envelope Index directly. Two
measured facts (2026-07-28, live store, 305k messages, CERN EWS inbox = 71,892
messages):

| Addressing a specific message via AppleScript | Time |
|---|---|
| `first message of inbox whose message id is "<rfc-id>"` (what competitors must do) | **85.6 s** |
| `«class mssg» id <ROWID> of mailbox "Inbox" of account "CERN"` | **0.16 s** |

**535× faster, because Mail's AppleScript object `id` IS the Envelope Index
ROWID** — a keyed lookup instead of a scan — and we get ROWIDs for free from
the read layer. Competitors cannot use this path: they never see the index.

Mutation write-through was also verified: `set flagged status … to true` via
the ROWID specifier took 0.157 s and the flag bit appeared in the Envelope
Index **within 2 s** (flags 8590131329→8590131345, flag_color 0→1; then
reverted cleanly). So the read layer doubles as a **verification oracle**.

## The concept: SELECT → PLAN → ACT → VERIFY

```
SELECT   SQLite Envelope Index      the existing search language, milliseconds
PLAN     frozen list of (ROWID, mailbox, account, disposition) + summary
ACT      AppleScript by-id specifiers, batched in ONE osascript process
VERIFY   re-read the index (~2 s later), report confirmed vs pending
```

- **Plan/apply** (terraform-style): `triage_plan(query…, actions)` returns the
  exact matched messages and a `plan_id`; nothing mutates. `triage_apply
  (plan_id)` executes and verifies. Claude shows the plan, the user (or the
  calling policy) approves. No "oops, that query matched 4,000 messages".
- Plans are files (`~/.email-mcp/plans/`), same pattern as the send spool:
  frozen at plan time, atomic rename claims, expiry (10 min).
- Disposition vocabulary is small and closed:
  `move_to`, `mark_read`, `mark_unread`, `flag(color)`, `unflag`, `archive`,
  `delete` (= move to account's Trash — true deletion is not offered).
- One `osascript` process per apply handles the whole batch (no per-message
  process spawn); per-message failures collected, not fatal.
- `mailbox_create(account, path)` is the only structural verb (AppleScript
  `make new mailbox`, supports nesting). Rename/delete of mailboxes: deferred —
  rare, riskier, and Mail.app's UI is fine for them.

## Why not the alternatives

| Actor | Verdict |
|---|---|
| AppleScript find+act (all competitors) | 85.6 s per lookup at our scale. Dead. |
| Direct IMAP (UID STORE/MOVE via `messages.remote_id`) | Fastest in theory, but the CERN account is **EWS, not IMAP** (`ews://` in the index) — dead for the main account; adds credential duplication for the rest. Kept as a pluggable actor for IMAP accounts later; the plan format already carries everything it needs. |
| EWS/Graph API | CERN tenant app-registration friction; parallel auth stack. No. |
| Writing the Envelope Index directly | Corruption risk on Apple's live WAL database. Never. |
| **SQLite-select + AppleScript by-ROWID act** | 0.16 s/message, no credentials, works for every account type Mail has (EWS included), Mail handles server sync. **Chosen.** |

## What is deliberately NOT in scope

Rules, smart mailboxes, templates (sweetrb's breadth). Philosophy: **the agent
is the rule engine.** A saved rule is a frozen decision; Claude deciding over
a live selection *is* the rule, with judgment. Triage gives it the one
primitive it needs to act on those decisions.

## Risks / open items

- ROWID↔AppleScript-id equivalence is undocumented Apple behavior. Mitigated:
  verify step catches any drift (a wrong id mutates nothing or the wrong
  message — so ACT should re-check `message id` (RFC header) of the resolved
  object against the plan before mutating; belt and braces, still O(1)).
- Envelope Index write-through lag measured at ≤2 s locally; server-side (EWS)
  sync is Mail's business and eventually consistent — VERIFY reports local
  store state.
- Mail.app must be running (same as competitors); `refresh_mail` already
  launches it.
- Batch ceiling per plan (default 200) until osascript batch latency is
  characterized at larger N.

## Tool surface (v0.6.0 target: 3 new tools, 15 total)

    triage_plan(query, from_addr?, mailbox?, account?, before?, after?,
                unread_only?, limit?, actions) -> {plan_id, count, messages[], summary}
    triage_apply(plan_id) -> {applied, verified, failures[]}
    mailbox_create(account, path) -> {ok}

## Live calibration results (2026-07-28, v0.6.0 implementation)

1. **Flag colors collapse on EWS.** `flag index 2` wrote `flag_color 1` —
   Exchange models flags as a binary follow-up flag. Verify relaxed to the
   `flagged` bit (color kept best-effort, reported in `observed`).
2. **Unsynced-mailbox fallback added.** A freshly created mailbox has NO
   Envelope Index row until content syncs; `move_to` targets now fall back
   to an AppleScript existence probe (target rowid None → verify =
   "left the source mailbox", noted in the apply result).
3. **AppleScript-created folders on EWS are phantoms.** The test folder
   looked fine client-side, but Exchange silently bounced every move into
   it (twice), and the folder couldn't even be deleted via AppleScript
   (-10000). Moves into the ESTABLISHED `Archive` folder verified cleanly
   both ways (Inbox→Archive→Inbox). `mailbox_create` now returns a warning
   on EWS accounts; README documents the create-in-UI workflow.
4. Working end-to-end, verified live: mark_read, flag, unflag, move_to
   (established folders, both directions), plan/apply/verify loop,
   Message-ID recheck, honest `pending` reporting. osascript batch cost:
   0.19-8.6 s per single-message plan (EWS moves are the slow end).
