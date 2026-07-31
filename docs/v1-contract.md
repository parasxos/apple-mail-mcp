# email-mcp v1 contract — promises in writing

*Frozen 2026-07-30 on branch `v0.10`, per `docs/v1-roadmap.md` §1 ("the
contract — design FIRST"). This document states what every tool promises on
the wire: shapes, codes, idempotency, caps, and the audit record of it all.
It is written BEFORE the ledger so the permanent record is built against a
designed surface, not today's mixed outputs. Compatibility policy: additive
only after v1.0 — breaking = v2 (§8).*

Sections: 1 scope + conformance table · 2 envelope semantics · 3 the one
error-code namespace · 4 idempotency + retry · 5 caps + TTLs · 6 audit event
schema v1 · 7 wire safety · 8 versioning.

---

## 1. Scope and per-tool conformance

The contract covers all 20 MCP tools of v0.10: the 19 shipping at v0.9 plus
`audit` (new in v0.10, contract-compliant from birth). With
`EMAIL_MCP_READ_ONLY=1` exactly the 11 read-side tools register (10 at v0.9
+ `audit`); the 9 mutating tools never exist in that session.

Conformance is staged deliberately:

- **v0.10 (this release, 2026-07/08)** eliminates every wire leak — no
  exception ever escapes to the MCP transport again — by adding coded
  failure envelopes to previously-**crashing** paths ("belts", §3.5).
  A crash is not a contract, so belting it breaks nothing. v0.10 does NOT
  touch any existing success shape, and does not add codes to existing
  prose-only `{ok: false, error}` failures.
- **v0.11 (surface)** finishes the job: bare success shapes gain envelopes,
  every failure carries a `code` from §3 (the SendError prose → code
  assignments of §3.4, made on paper here, get wired), and outputSchema is
  frozen by snapshot. inputSchema is already frozen at v0.10 (§8).

| # | Tool | Side | Success shape (v0.11) | Failure shape (v0.11) | v0.10 change | Fully conformant at |
|---|------|------|--------------------|--------------------|--------------|---------------------|
| 1 | `search_emails` | R | `{ok: true, fts, results}` | crashed on bad `before`/`after` (ValueError leak) | belt: coded failures (`invalid_input`, `mail_unavailable`, `internal_error`) | **v0.10** |
| 2 | `get_email` | R | `{ok: true, email}` — envelope since v0.11 (was bare dict) | `{ok: false, code, error}` — `not_found`, `mail_unavailable`, `invalid_input` (bad view), … | belt: coded failures on the crash paths (`not_found`, `mail_unavailable`, …) | **v0.11** — shipped |
| 3 | `get_emails_batch` | R | `{ok: true, view, emails, errors}` — per-id failures are data in `errors[]`, each `{id, code, error}` | `{ok: false, code, error}` on bad view / >50 ids | belt on crash paths | **v0.11** — shipped |
| 4 | `get_thread` | R | `{ok: true, thread: [...]}` — envelope since v0.11 (was bare array) | belted: `{ok: false, code, error}` | none — deferred: the declared array outputSchema blocked a coded dict | **v0.11** — shipped |
| 5 | `list_mailboxes` | R | `{ok: true, mailboxes: [...]}` — envelope since v0.11 | belted: `{ok: false, code, error}` | none — deferred (same reason as get_thread) | **v0.11** — shipped |
| 6 | `list_recent` | R | `{ok: true, messages: [...]}` — envelope since v0.11 | belted: `{ok: false, code, error}` | none — deferred (same reason as get_thread) | **v0.11** — shipped |
| 7 | `get_attachment` | R | `{ok: true, attachment}` — envelope since v0.11 (was bare dict) | `{ok: false, code, error}` | belt: coded failures | **v0.11** — shipped |
| 8 | `refresh_mail` | R | `{ok, before, after, new_messages, …}` (hardened since v0.4) | `{ok: false, error, error_code?, code?}` — the raw osascript number now carries its mapped string `code` (§3.3) | none needed (already total) | **v0.11** — shipped |
| 9 | `list_scheduled` | R | `{ok: true, dispatcher_installed, …states}` | `{ok: false, code: invalid_input, error}` on unknown state | belt on crash paths | **v0.11** — shipped |
| 10 | `doctor` | R | `{ok, read_only, checks, audit}` — **`ok` reports environment health**, not tool failure (documented exception to §2); `audit` is the ledger check as a top-level sibling of `checks` | already total | gained the `audit` check | **v0.10** |
| 11 | `audit` | R | `{ok: true, events, files_scanned, skipped_lines}` | `{ok: false, code, error, fix?}` | **new tool** — born conformant | **v0.10** |
| 12 | `send_email` | W | `{ok: true, message_id, to, cc, bcc, subject, attachments, bootstrapped}` | `{ok: false, code, error}` — every SendError site coded per §3.4 | audit `send` event on every terminal outcome | **v0.11** — shipped |
| 13 | `reply_email` | W | same as send_email | same | audit `reply` event | **v0.11** — shipped |
| 14 | `schedule_email` | W | `{ok: true, id, send_at, message_id, executor, …, warning?}` | `{ok: false, code, error}` — §3.4 codes | audit `schedule` event | **v0.11** — shipped |
| 15 | `cancel_scheduled` | W | `{ok: true, id, status, subject, was_due}` | `{ok: false, code, error, operation_id?}` — all 7 sites coded: unknown id → `not_found`, state conflicts → `invalid_input`, identity/Exchange trouble via §3.4 | audit `cancel` event | **v0.11** — shipped |
| 16 | `triage_plan` | W | `{ok: true, plan_id, count, expires_at, summary, actions, messages}` | `{ok: false, code, error}` — TriageError codes | belt closes the `before`/`after` ValueError leak | **v0.10** |
| 17 | `triage_plan_delete` | W | same as triage_plan | same | same belt | **v0.10** |
| 18 | `triage_apply` | W | `{ok: true, status, planned, acted, failures[], verified, pending[], …}` — per-message failures are data | `{ok: false, code, error}` | belt (carries `operation_id: plan_id`); audit `plan_finish` via plans.finish | **v0.10** |
| 19 | `mailbox_create` | W | `{ok: true, existed, applescript, index_verified, mail_verified, warning}` | `{ok: false, code, error}` | audit `mailbox_create` event (created only) | **v0.10** |
| 20 | `mailbox_delete` | W | `{ok: true, existed, deleted, mail_verified, method?, warning}` | `{ok: false, code, error}` (incl. the literal-only codes, §3.1) | audit `mailbox_delete` event (issued only) | **v0.10** |

Shipped status (2026-07-30, branch v0.11): every "v0.11 — shipped" row
above is live. The three bare-**list** tools (4–6) — whose v0.10 deferral
existed because their registered outputSchema declared an array, so even a
failure envelope would have violated the schema the client already held —
and the two bare-dict tools (2, 7) took their one allowed break into
envelopes; batch `errors[]` entries carry `code`; every send/cancel failure
site carries its §3 code. The output surface is frozen by snapshot from
this point (§8).

## 2. Envelope semantics

Every tool that returns an object speaks one envelope:

**Success** — `{ok: true, …tool-specific data}`.

**Failure** — `{ok: false, code, error, fix?, operation_id?}` where

- `code` — machine-readable snake_case string from the single namespace in
  §3. Stable: dispatch on it.
- `error` — one-line human/agent-readable prose, safe to surface verbatim.
  Never contains secrets, tracebacks, or message bodies.
- `fix` — optional concrete remedy: a command to run or a Settings pane to
  open (belt failures always say `fix: "run doctor"`).
- `operation_id` — **present on failure iff a durable artifact id had
  already been minted for the operation before it failed** (a plan id, a
  spool id). It is the same id the audit ledger's `op` field carries (§6),
  so a failed operation can be threaded to its ledger events in one lookup.
  It is never minted *for* a failure: a validation reject that touched
  nothing carries no `operation_id`.

Rules:

- **No exception escapes to the wire.** A traceback on the MCP transport is
  a contract violation, full stop (see §7).
- **Partial failure is data, not failure.** `failures[]` (triage_apply),
  `errors[]` (get_emails_batch) and `pending[]` (triage_apply) ride inside
  `ok: true` envelopes. `ok: false` means the *operation* did not happen.
- **Documented exceptions**: `doctor`'s top-level `ok` reports environment
  health (a failed check, not a failed tool call); `refresh_mail`'s `ok`
  reports the nudge outcome. Both are total functions and never crash.
- **Consumers must tolerate unknown keys** — the envelope grows additively
  (§8).

## 3. The error-code namespace

One flat namespace. Before v0.10 five separate systems existed: (1) the
TriageError codes, (2) two literal-only dict codes on mailbox_delete,
(3) the item-level result codes inside triage_apply, (4) the numeric
osascript error map, and (5) SendError prose prefixes (with smtp's secret
errors carrying no prefix at all). This section consolidates all five; the
machine-readable mirror is `email_mcp/codes.py` (data-only module — flat
constants, frozensets, `OSA_CODE_MAP`). A code means the same thing on
every tool that uses it. Codes are never renamed or removed after v1.0;
adding one is additive (§8).

### 3.1 Triage & mailbox codes (tool-level, live on the wire since v0.6–v0.8)

The 22 codes raised via `TriageError` in `email_mcp/triage.py`, verbatim,
plus the 2 literal-only codes that appear directly in mailbox_delete's
failure dicts (no exception class behind them):

| Code | Raised by | Meaning |
|------|-----------|---------|
| `invalid_action` | triage_plan | missing/malformed/unknown action entry, bad flag color, move_to without mailbox |
| `destructive_action` | triage_plan | `delete` attempted through triage_plan — it has its own tool |
| `conflicting_actions` | triage_plan | duplicate or mutually exclusive actions in one plan |
| `unsupported_source` | triage_plan, mailbox_create, mailbox_delete | the email source lacks triage capabilities |
| `empty_selection` | triage_plan | no messages match, or all vanished before planning |
| `selection_too_large` | triage_plan, triage_plan_delete | selection over the cap (200 / 50, §5) — rejected, never truncated |
| `cross_account` | triage_plan (move_to), triage_plan_delete | selection spans accounts; add `account=` |
| `noop_move` | triage_plan | every selected message is already in the target mailbox |
| `unknown_mailbox` | triage_plan | move target not in the index and not in Mail.app |
| `invalid_name` | triage_plan, mailbox_create/delete | control character in a name — cannot be scripted |
| `plan_not_found` | triage_apply | no plan with that id |
| `plan_already_applied` | triage_apply | plan status is applied/failed — single-shot |
| `plan_expired` | triage_apply | TTL lapsed (600 s, §5) — re-run triage_plan |
| `plan_claimed` | triage_apply | another process holds the apply claim |
| `osascript_unavailable` | triage_apply | osascript not found — macOS-only tool |
| `mail_unresponsive` | triage_apply, mailbox_create, mailbox_delete | Mail.app did not answer within the timeout |
| `automation_denied` | triage_apply, mailbox_create, mailbox_delete | Apple Events not authorised (osa −1743, §3.3) |
| `no_app` | triage_apply | Mail.app not reachable (osa −1728) |
| `script_error` | triage_apply, mailbox_create | batch/pre-flight script failed wholesale |
| `account_unresolvable` | triage_apply | plan's account id(s) not present in Mail.app |
| `unknown_account` | mailbox_create, mailbox_delete | account UUID not found |
| `not_empty` | mailbox_delete | mailbox holds messages — only empty ones are deletable |
| `accessibility_denied` | mailbox_delete (**literal-only**) | UI fallback blocked — Accessibility permission missing (osa −1719/−25211/−1743 in the System Events context) |
| `delete_failed` | mailbox_delete (**literal-only**) | mailbox survived both the delete verb and the UI path (phantom Exchange folder) |

### 3.2 Item-level codes (`failures[].code` inside triage_apply)

Per-message result vocabulary — data inside an `ok: true` envelope, never a
tool-level code:

| Code | Meaning |
|------|---------|
| `ok` | the batched script confirmed the action for this message (internal success marker; **never appears in `failures[]`**) |
| `mid_mismatch` | the Message-ID guard tripped — the ROWID points at a different message than planned; nothing was done to it |
| `applescript` | Mail returned an AppleScript error for this one message (detail carries the number + text) |
| `no_result` | the script produced no line for this id |
| `batch_timeout` | osascript was killed at the deadline; verification may still confirm the message independently |

`get_emails_batch`'s `errors[]` entries are `{id, code, error}` as of v0.11
(`code` from `not_found`/`invalid_input`).

### 3.3 osascript numeric map

Numeric codes parsed from osascript stderr, and the namespace code each one
maps to (`OSA_CODE_MAP` in codes.py):

| osa code | Maps to | Context |
|----------|---------|---------|
| `-1743` | `automation_denied` | Apple Events not authorised. **Context-sensitive**: inside the mailbox_delete UI fallback (System Events), −1743 reads as `accessibility_denied` instead |
| `-1728` | `no_app` | Mail.app not installed / not reachable |
| `-1719` | `accessibility_denied` | System Events cannot reach Mail's UI |
| `-25211` | `accessibility_denied` | Accessibility explicitly disabled for the host process |
| `-10000` | `script_error` | "AppleEvent handler failed" — generic. **Advisory on mailbox delete**: Mail's delete verb often returns −10000 even when deletion succeeded, so there the outcome is decided by a live re-probe, never by this code |

`refresh_mail` currently surfaces the raw number as `error_code`; at v0.11
it additionally carries the mapped string `code`.

### 3.4 Send codes — v0.11 target assignments (frozen on paper now)

Today every send-path failure is `{ok: false, error}` with prose conventions:
a `header_injection:` / `invalid_recipient:` prefix from the composer, a
`[<identity>/<driver>]` lane prefix from the transports — and **no prefix at
all** on the smtp driver's secret-source errors (the known gap). v0.11 maps
every SendError raise site to a code from this table; the assignment is
frozen HERE so v0.11 implements rather than designs. The set is
`SEND_CODES_V011` in codes.py.

| Prose today (raise site) | v0.11 `code` |
|--------------------------|--------------|
| `header_injection: …` (compose-time CR/LF/NUL fence) | `header_injection` |
| `invalid_recipient: …` (control chars or unusable bare address in to/cc/bcc) | `invalid_recipient` |
| "Refusing to send as identity […]: recipient(s) not on its allowlist …" | `recipient_not_allowed` |
| "attachment not found: …" | `attachment_not_found` |
| "attachment is a directory: …" / "cannot read attachment …" | `attachment_unreadable` |
| "attachments total X MB, over the Y MB budget …" | `attachments_too_large` |
| "invalid header content: …" (stdlib refused a header value) | `invalid_header` |
| "\`to\` is required …" / "\`subject\` is required." / "\`body\` is empty." | `invalid_input` (shared belt code, §3.5) |
| "invalid send_at (want ISO-8601): …" | `invalid_send_at` |
| "send_at is in the past …" | `send_at_in_past` |
| "[i/d] transport unavailable: …" (preflight failed) | `transport_unavailable` |
| ssh_sendmail "ssh not found on PATH." / pipe "command not found: …" | `transport_unavailable` |
| ssh_sendmail "delivery pipe timed out …" / "delivery failed (exit N) …"; pipe "hung for 60s" / "delivery failed (exit N)"; smtp "SMTP delivery via host:port failed: …" | `delivery_failed` |
| smtp "SMTP auth failed for …" (wrong/expired app password) | `auth_failed` |
| **smtp's UNPREFIXED secret errors** — `security`/`op` CLI not found, Keychain/1Password read timed out (locked app, unanswerable prompt), item/reference not readable | `credentials_unavailable` (v0.11 also adds the missing `[identity/smtp]` lane prefix to the prose) |
| transports "unknown transport driver …" / "bad transport params …"; smtp "needs a secret source"; pipe "\`command\` is empty."; every `IdentityError` about the identities file (malformed TOML, missing default, missing from_addr, duplicate from_addr, unknown driver/executor, graph table problems, no identity configured at all) | `identity_misconfigured` |
| `IdentityError` "unknown identity 'X'. Available: …" (bad `from_identity` argument) | `unknown_identity` |

These codes surface on `send_email`, `reply_email`, `schedule_email`, and —
because `IdentityError`/`GraphError` subclass `SendError` — inside
`cancel_scheduled`'s failure prose too.

### 3.5 Belt codes (new at v0.10) and the dispatcher code

The v0.10 wire-safety belts convert previously-crashing paths into coded
envelopes (`BELT_CODES` in codes.py). Every belt logs the full traceback to
the log file and returns `fix: "run doctor"`:

| Code | Meaning |
|------|---------|
| `internal_error` | unexpected exception — the belt of last resort; the traceback is in the log |
| `not_found` | a referenced object does not exist (unknown message id, vanished attachment, unknown scheduled id) |
| `invalid_input` | a caller-supplied value could not be parsed/used (bad ISO datetime in `before`/`after`, malformed argument) |
| `mail_unavailable` | the Mail store is not readable — Mail not configured, or Full Disk Access missing |

One additional code lives at the dispatcher/ledger level, not on any tool's
wire: **`spool_eml_missing`** — a claimed manifest whose frozen `.eml` is
gone (the entry parks in `failed/`); it appears as the `deliver` event's
failure code in the audit ledger (§6).

## 4. Idempotency and retry — every mutating tool

The send dedupe key is the **Message-ID**: it is minted once at
compose/schedule time, frozen into the spool manifest and the Bcc-to-self
copy, and used by the Graph reconcile pass (`internetMessageId` lookups in
Drafts and Sent Items). After ANY ambiguous outcome, search for the
message_id before retrying — never blind-resend.

| Tool | Idempotent? | Retry rule |
|------|-------------|------------|
| `send_email` | **No** — every call composes a fresh Message-ID | `{ok: false}` before transport handoff (validation, allowlist, attachments, preflight) touched nothing: fix and retry freely. After an ambiguous transport outcome (timeout, crash): check for the returned/logged `message_id` (Bcc-to-self copy, audit `send` event) before resending |
| `reply_email` | **No** — same as send_email | same as send_email |
| `schedule_email` | **No per call** (fresh spool id + Message-ID each time), but the scheduled delivery itself is exactly-once: the dispatcher's atomic manifest rename means one claim wins, and a graph entry is never locally delivered while Exchange may still hold or have sent the draft | `{ok: false}` scheduled nothing — retry freely. `{ok: true}` means the frozen .eml is durably spooled; do NOT re-schedule, use `list_scheduled`/`cancel_scheduled`. Dedupe key: the manifest's frozen `message_id` |
| `cancel_scheduled` | **Effectively** — the pending→cancelled transition happens at most once (atomic rename fence); repeats and races return `{ok: false}` explaining the current state, mutating nothing | safe to retry until a terminal answer; graph revoke failures leave the entry pending with instructions (Exchange keeps the job until the revoke is CONFIRMED) |
| `triage_plan` | Each call freezes a NEW plan file (durable artifact, zero mail mutation) | retry freely; superseded plans expire on their own (TTL 600 s) and are GC'd (7 d) |
| `triage_plan_delete` | same as triage_plan | same |
| `triage_apply` | **Exactly-once per plan** — the atomic claim rename means one apply owns the plan; a re-invocation returns `plan_claimed` (mid-flight) or `plan_already_applied` (done), never re-mutates | do not re-invoke mid-flight (large plans run minutes); after `batch_timeout` the verify pass has already reconciled what landed — read `failures[]`/`pending[]`, don't re-apply |
| `mailbox_create` | **Yes** — already-exists short-circuits to `{ok: true, existed: true}` without touching Mail | retry freely |
| `mailbox_delete` | **Yes** — already-absent returns `{ok: true, existed: false}`; outcome decided by live re-probe, not by AppleScript's (often false) error | retry freely |

Scheduled-delivery retry (dispatcher-side, not caller-visible): 5 attempts
per message with 2/5/15/45/120-minute backoff, then park in `failed/` with
`last_error` + a macOS notification. A dispatcher that dies mid-delivery is
recovered after 10 minutes (attempt consumed — the outcome was unknown).

## 5. Caps and TTLs (live config defaults)

Caps REJECT, never truncate. Values below are the defaults of
`email_mcp/config.py` at v0.10; env overrides in parentheses.

| Limit | Default | Knob |
|-------|---------|------|
| triage plan size | 200 messages | `EMAIL_MCP_TRIAGE_MAX` |
| delete plan size | 50 messages | `EMAIL_MCP_TRIAGE_DELETE_MAX` |
| plan TTL (draft → applicable) | 600 s | `EMAIL_MCP_TRIAGE_TTL` |
| plan GC horizon | 7 days | fixed |
| stale apply-claim finalised as failed | 2 × TTL | fixed |
| apply script timeout | auto: max(60, min(300, 30 + 0.6·n)) s | `EMAIL_MCP_TRIAGE_TIMEOUT` (>0 overrides) |
| apply/mailbox verify | 3 polls × 2.0 s | `EMAIL_MCP_TRIAGE_VERIFY_POLLS` / `…_VERIFY_INTERVAL` |
| batch read | 50 ids | fixed (`_BATCH_MAX_IDS`) |
| search / list default page | 50 | `limit` parameter |
| attachment budget per message | 20 MB pre-base64 | `EMAIL_MCP_MAX_ATTACH_MB` |
| body bytes served per message | 2 000 000 | `EMAIL_MCP_MAX_BODY_BYTES` |
| scheduled-send attempts | 5, backoff 2/5/15/45/120 min | `EMAIL_MCP_SEND_RETRIES` (backoff fixed) |
| dispatcher cadence | 60 s (launchd StartInterval) | fixed |
| stranded `sending/` recovery | 10 min | fixed |
| Graph reconcile grace after send_at | 10 min | fixed |
| schedule-in-the-past tolerance | 120 s | fixed |
| refresh_mail wait / timeout clamps | 0–60 s / 1–120 s | parameters, clamped |
| FTS (informational — derived state) | 2000 hits/search, 500 docs & 2.0 s inline pass, 512 KiB/doc | `EMAIL_MCP_FTS_*` |
| audit event size | 16 384 bytes | fixed (schema v1, §6) |
| audit subject field | 200 chars | fixed (schema v1) |

## 6. Audit event schema v1

The ledger indexes the truths already frozen elsewhere (plan files, spool
manifests, Message-IDs) — it does not create truth. Storage: append-only
monthly JSONL, `~/.email-mcp/audit/YYYY-MM.jsonl`, dir 0700, files 0600.
ONE event per mutation. Two writer processes exist (server + launchd
dispatcher); each emit is a single `os.write` on an
`O_RDWR|O_CREAT|O_APPEND` fd resolved per event (RDWR so the tail probe can `pread`; `O_APPEND` carries the atomicity), so lines never interleave
and month rollover has no race.

**Envelope (always present):**

| Field | Meaning |
|-------|---------|
| `v` | schema version — `1` |
| `ts` | UTC ISO-8601 timestamp of the emit |
| `op` | operation id. **The durable artifact's own id where one exists** (spool id for send-later, plan id for triage — which threads dispatcher and server events for free), else a fresh mint. Same value a failure envelope exposes as `operation_id` (§2) |
| `src` | emitting process: `server`, `dispatcher`, or `cli` |
| `event` | event name (table below) |
| `outcome` | terminal result of the mutation (e.g. `sent`, `retry`, `failed`, `applied`, `cancelled`, `created`) |

**Optional fields** (only non-null keys are serialized): `identity`,
`account`, `mailbox`, `message_id`, `spool_id`, `plan_id`, `draft_id`,
`to`, `cc`, `bcc`, `subject`, `summary`, `detail`, `tool` (the MCP tool
name; present on every server-layer event — audit finding F9).

**Events** (placement rule: emit in the process that decides the outcome,
at the point it becomes durable):

| Event | Emitted by | Notes |
|-------|-----------|-------|
| `send` | server tool layer | every terminal outcome of send_email |
| `reply` | server tool layer | detail carries `orig_id` + `reply_all` |
| `schedule` | server tool layer | detail: executor, send_at, draft_id, graph_fallback |
| `deliver` | dispatcher | sent / retry / failed — incl. the eml-missing code `spool_eml_missing` |
| `recover` | dispatcher | stranded `sending/` entry returned to pending |
| `graph_adopt` | dispatcher | orphan Exchange draft adopted (crash-window recovery) |
| `graph_flip` | dispatcher | entry flipped graph → launchd (detail: reason) |
| `graph_sent` | dispatcher | Exchange confirmed the deferred send |
| `graph_cancelled_external` | dispatcher | draft discarded outside the spool (OWA/Outlook) |
| `cancel` | server tool layer | cancelled / too_late_sent / failed … |
| `plan_create` | library (build_plan) | carries the plan summary |
| `plan_finish` | library (`plans.finish` — the one seam covering apply success, all failure sites, expiry AND gc's stale-claim finalisation) | applied / failed / expired; carries `Plan.summary` + compact per-message outcomes (`{id, code}` + pending ids) — this **outlives plan GC** |
| `mailbox_create` | server tool layer | emitted only when actually created (idempotent no-op emits nothing) |
| `mailbox_delete` | server tool layer | emitted only when a deletion was actually issued |
| `doctor_fix` | cli (`doctor --fix`) | *added v0.11* — one event per applied/failed repair, outcome `fixed` / `failed`, detail `{repair, finding, action}`; every event of one `--fix` run shares one freshly-minted `op` |
| `lifecycle` | cli | *added v0.11* — one event per lifecycle run, outcome `setup` / `update` / `uninstall`; detail carries names and counts only (identity *names*, agents, migrations, purge plan) — never addresses or secrets. The `uninstall` event is emitted BEFORE removal; with `--purge` it is destroyed with the ledger moments later (the documented deal — the receipts hint prints first) |

The two `cli`-sourced rows are v0.11 additions under §8's allowance for new
audit event types (additive — no `v` bump); with them the lifecycle CLI
becomes a third writer process beside server and dispatcher, using the same
single-`os.write` O_APPEND emit.

Not ledger-worthy by design: FTS activity (derived state, rebuildable) and
`_graph_leave` no-evidence passes (would spam one event per tick while
Exchange is unreachable).

**No-bodies guarantee.** Events NEVER contain message bodies — not in
`detail`, not anywhere. `subject` is truncated to 200 chars. Recipient
addresses and summaries are allowed; body text is not, ever.

**16 KB truncation order.** An event is capped at 16 384 bytes
(`MAX_EVENT_BYTES`). When over, fields are shed in this order:
per-message lists first (`failures`/`pending` collapse to counts), then
`detail` wholesale. The envelope fields and `summary` ALWAYS survive — a
truncated event still says what happened, to how many, with which outcome.

**Emit-failure policy: log-and-continue, absolute.** An unwritable ledger
(missing dir, permissions, full disk) NEVER blocks mail: `emit()` never
raises; it logs the problem and returns None. The mutation stands.

**The documented event-loss window.** The ledger records mutations after
they become durable. Two loss modes exist and are accepted, documented
behavior — the ledger is an index of truth, not a second source of it:

1. A process killed between the durable mutation and its emit loses that
   one event. The mutation itself is intact and remains discoverable in the
   primary artifacts (spool manifest, plan file, Sent copy).
2. While the audit dir is unwritable, events are dropped (per the policy
   above) and the drop is logged.

Consequently events are **at-most-once per mutation** (and exactly-once in
normal operation); consumers must treat absence of an event as "not
recorded", never as proof the mutation did not happen.

**Reading the ledger:** the `audit` tool
(filters: `since`/`until`/`tool`/`event`/`plan_id`/`operation_id`/`limit`)
returns `{ok: true, events (newest-first), files_scanned, skipped_lines}` —
torn or corrupt lines are skipped and counted, never fatal. CLI:
`python -m email_mcp.audit --tail/--since/…` (JSONL on stdout). `doctor`
gains an `audit` check (dir exists, perms, writability).

## 7. Wire safety

- **stdout of the server process is the MCP transport.** Library and server
  code never print; logging goes through `log.get_logger()` to the log
  file. (The audit CLI and dispatcher `main()` print by design — they are
  separate entry points whose stdout is not the MCP wire.)
- **No exception crosses the wire.** Every tool is total — all 20,
  including the formerly array-shaped `get_thread`/`list_mailboxes`/
  `list_recent`: any path that could raise is either handled with a
  specific code or caught by a belt that logs the full traceback and
  returns `{ok: false, code: "internal_error", error, fix: "run doctor"}`.
  (The v0.10 carve-out that excused those three retired when they gained
  envelopes at v0.11 — verified by live poisoned-source probes.)
- Every tool return is JSON-serializable (dataclasses/datetimes converted
  at the boundary).
- **Secrets never appear in envelopes, logs, or audit events.** Secret
  *references* (a Keychain item name, an `op://` path) may be named in
  error prose — the secret *values* never leave the driver.
- Message bodies never appear in audit events (§6); error prose never
  embeds bodies.
- SendError/TriageError messages are caller-fixable by construction and
  safe to surface verbatim.

## 8. Versioning and compatibility

- **This contract is v1.** It binds from v0.10 forward; v0.11 completes
  conformance (§1); v1.0.0 publishes the commitment.
- **Additive-only after v1.0**: new tools, new optional input parameters,
  new envelope keys, new codes, new audit event types/fields may be added.
  Existing keys/codes/events never change meaning, get renamed, or get
  removed. A breaking change means v2.
- **Codes**: the namespace in §3 only grows. Dispatching on a code is safe
  forever; dispatching on error prose is not (prose may be reworded).
- **inputSchema: frozen at v0.10** (snapshot tests; inputs do not change in
  v0.11). Tool *descriptions* are excluded from the freeze — docstrings may
  evolve.
- **outputSchema: frozen at v0.11**, after the bare shapes (§1, tools
  2, 4–7) took their one allowed break into envelopes. That break was the
  known normalization debt named in the roadmap and happened exactly once.
  *Mechanism note (v0.11):* FastMCP (1.27) declares no outputSchema for
  `-> dict` tools, so the freeze is structural —
  `tests/snapshots/output_schemas.json` pins (a) the declared-schema map
  (all `null` today; a future typed return trips it deliberately) and
  (b) the success-envelope shape of ALL 20 tools, mutating tools
  included, probed against the mail fixture with only the
  transport/osascript/launchd boundaries faked. List shapes are
  element-unions: a key or type change in any element breaks the
  snapshot. `doctor` is pinned at envelope level only
  (ok/read_only/checks + the ledger check) — its per-check diagnostics
  vary with machine state by design (§2's documented exception). A
  missing snapshot file fails the suite loudly (never a silent
  re-freeze); regeneration is a deliberate act, stated explicitly in the
  change that carries it.
- **Audit schema**: `v` is bumped only for breaking changes to the event
  envelope; adding optional fields does not bump it. Readers must ignore
  unknown fields and tolerate mixed `v` within one file month.
- Spool manifests and plan files keep their compatibility story (older
  manifests without `identity`/`executor` fields keep working via
  defaults) — the ledger never requires migrating them.
