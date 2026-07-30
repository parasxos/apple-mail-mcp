# v1.0 — The product keeps its own receipts

*Concept + roadmap, 2026-07-30. Fourth in the series: `triage-design.md`
(selection × disposition), `transport-design.md` (identity × driver),
`v0.8-concept.md` (removing the asterisks). Incorporates two external
review plans; what was absorbed and what was trimmed is recorded at the end.*

## The one idea

The v0.x arc built capability and proved every claim live — probe verdicts,
lid-closed deliveries, red-team reports, calibration docs. But that proof
lives in session transcripts and the builder's memory. A product's trust
cannot require having watched it being built.

> **v1.0 moves the burden of proof from the builder into the artifact.**

Nothing new happens to mail in v1.0. What changes is that everything that
happens becomes *accountable*: stated in advance (the contract), recorded in
retrospect (the ledger), adoptable without archaeology (the lifecycle), and
demonstrated on demand (the RC). Four movements, one idea.

The line continues: v0.7 made every claim routable; v0.8 made every claim
total; v0.9 made schedules durable; **v1.0 makes every claim accountable.**

## The four movements

### 1. The contract — promises in writing (design FIRST)

A short specification stage before any implementation — the external review's
ordering correction, adopted: building the ledger against today's mixed tool
outputs would bake inconsistency into the permanent record.

Freeze on paper: the standard envelope (`{ok, code, error, fix,
operation_id}` on failure; `ok: true` + data on success — every tool, no
exceptions escaping to the wire); the error-code vocabulary (the codes
already exist across triage/transports/identities — this catalogs and
completes them); idempotency and retry rules per mutating tool; plan TTL and
batch caps; the audit-event schema; the compatibility policy (**additive-only
after v1.0; breaking = v2**).

Known normalization debt this will resolve: `get_email` returns a bare
shaped dict (v0.7-compat choice), `search_emails` returns an envelope,
triage returns `ok`-envelopes — v1.0 is precisely the moment the bare shapes
are allowed to break.

### 2. The ledger — deeds on record

The one substantive functional gap. Today four record systems exist and
don't talk: triage plans (frozen, but GC'd after 7 days), spool manifests,
immediate sends (a Bcc-to-self and a log line), mailbox create/delete
(a log line). Nobody can ask *"what did the tool change yesterday?"* and get
one answer.

**The ledger does not create truth — it indexes the truths already frozen.**
One append-only event per mutation (`operation_id` threading through plan
files, spool ids, Message-IDs), carrying: timestamp, tool, action, identity/
account, affected message ids, requested vs verified result, error code,
transport/executor. For triage it carries the plan's summary line — small,
body-free, and it **outlives plan GC**. Never message bodies. Storage:
append-only monthly JSONL under `~/.email-mcp/audit/` (0700) — the house
idiom (greppable, rotation for free, no schema migrations), not a database.

Surface: **one `audit` tool** (filters: since/plan_id/operation_id/tool),
not the reviewer's three — the same minimalism that made doctor absorb
`--transport-check`. Tool count 19 → 20; READ_ONLY set 10 → 11.

Horizon note, not scope: the ledger is the foundation an undo-workflow would
stand on (the field's only precedent: jgalea's transaction log).

### 3. The lifecycle — adoption without archaeology

`pipx install … && email-mcp setup` → working read-only server on a clean
Mac in under 15 minutes; write access enabled explicitly afterward. The full
lifecycle, not just first install: `setup` (detect Mail, walk permissions,
first identity, optional Graph, launchd agents, FTS build, print MCP config,
smoke test), `doctor --fix` for the safe repairs, `update` (config/spool
migrations — the compat tests for old manifests already exist), `uninstall`
(launchd bootout, token-cache removal, the works).

**This movement forces the deferred name decision:** `pipx install email-mcp`
collides with codefuturist's 83★ TypeScript project. Packaging is where the
name gets settled — publish under a distinct name (the MCP registration is
already `apple-mail`) or consciously contest the collision. Decide at the
v0.11 gate, not silently.

### 4. The RC — claims that survive repetition

One scripted end-to-end life story, run repeatedly: clean install →
permissions → identities → index → search/read → send → schedule (launchd
AND graph) → sleep/restart → triage move/flag → trash plan → **audit history
inspection** → index corrupt/rebuild → upgrade from v0.9 → uninstall.
Plus the failure matrix at *system* level — most of it already exists at
unit level (the red-team's F1–F14, mutation-tested); the RC's new value is
the lifecycle dimension (upgrade, permission revocation, corrupt state) and
running it against real accounts. Final gate: the RC runs in daily use
before the tag.

## What stays out (unchanged non-goals, restated once)

Web UI, cloud service, non-macOS, a static rules engine (*the agent is the
rule engine* — still), autonomous deletion, provider-specific transports
beyond the four executors/drivers that exist, permanent AI categories,
calendar/contacts. Undo: post-v1, on the ledger.

## Milestones

| Gate | Ships | Exit criterion |
|---|---|---|
| **v0.10 — governance** | Contract spec (§1) + ledger + `audit` tool | Every mutation of every kind produces exactly one event; "what changed yesterday" answerable in one call |
| **v0.11 — surface** | Envelope normalization + schema snapshot tests + lifecycle commands + name decision | Clean-Mac install < 15 min; snapshot tests lock the frozen contract |
| **v1.0-rc** | The RC programme | E2E life story passes repeatedly, incl. upgrade-from-v0.9; no critical/major findings open |
| **v1.0.0** | Docs from a stranger's perspective; tag | RC passed without architectural change; compatibility commitment published |

## What was absorbed from the external reviews, and what was trimmed

**Absorbed:** contract-design-before-ledger (the correct dependency — their
best point); the ledger as gap #1; lifecycle-not-just-install; the RC as a
programme, not a suite; additive-only compatibility policy; pipx-is-enough.

**Trimmed, with reasons:** three audit tools → one (verb-soup resistance);
audit "before state" field → reference the frozen artifacts instead of
duplicating them (plans already store `pre`; the ledger carries the summary
that survives GC); the failure matrix presented as new → largely exists at
unit level since the v0.9 red-team, so the RC scopes to what's genuinely
untested (lifecycle, real-account, upgrade); "everyday actions" and "decide
Graph" sections → already shipped (v0.6 triage verbs, v0.9 calibrated
Graph). Added, which both reviews missed: packaging forces the
codefuturist name-collision decision.

## Why this is the beautiful version

Each movement completes the existing philosophy instead of adding to it.
The spine has always been *frozen intent → deterministic execution →
verification against the store*. v1.0 adds the missing fourth clause:
**→ memory of it all** — and then wraps the whole in promises a stranger
can read (contract), install (lifecycle), and re-verify (RC) without ever
having met the builder. A user who adopts email-mcp at v1.0.0 inherits the
same standard of evidence Paris got live: they just get it from the
artifact instead of the session.
