# Email-MCP landscape survey — 2026-07-29

*Three parallel research agents surveyed ~40 email MCP servers across the
Gmail/Workspace, Outlook/M365/IMAP, and macOS/emerging ecosystems. This is the
synthesis; the per-server tables live in the session record. Purpose: position
email-mcp honestly and rank what to build or defend next.*

## The five legs, audited against the whole field

| Our claimed differentiator | Verdict after ~40 servers |
|---|---|
| Direct SQLite Envelope Index reads | **Commoditized.** Six other projects do it (Python/Swift/Rust); public schema writeups; imdinu/apple-mail-mcp publishes competitive benchmarks (~5-28 ms ops on 73.5K msgs) and adds an **FTS5 full-body index** — which beats our snippet-only search on coverage. |
| Outlook-safe RFC-822 send | **Rare, not unique.** sweetrb ships optional SMTP for the same blockquote bug; che-apple-mail-mcp solves it independently via Accessibility-driven compose. Ours is still the only one that *routes* composition per identity. |
| Scheduled send (frozen spool + launchd) | **One real rival:** codefuturist/email-mcp (83★, **same name as ours**) ships a 3-layer scheduler incl. a launchd/crontab daemon with retries + locking. Nobody else in ANY ecosystem has it — Gmail API has no endpoint, and **no Graph MCP exposes Exchange's native deferred send** (unclaimed niche, see below). |
| Identity → transport routing + external secret refs | **Unique, ecosystem-wide.** Multi-account exists (4 Gmail servers, several IMAP) but it's token-picking, not transport routing. **Not one server references 1Password/vault/any external secret manager.** |
| Plan → apply → verify triage | **Unique, ecosystem-wide.** Two projects market batch triage and execute blind; closest precedents are one dry-run flag (nikolausm) and one transaction-log undo (jgalea/mailbox-mcp). **Nobody verifies mutations by reading the store back.** |

**Net position:** no competitor has more than two of the five legs. The moat is
the combination — but the read leg is table stakes now and the scheduling leg
has a credible rival with our name.

## Threats, ranked

1. **Apple system-level MCP via App Intents** — MCP strings in macOS 26.1
   betas; not announced. If it ships for Mail, the AppleScript-bridge category
   dies overnight. Our SQLite reads + spool survive longest; triage's actor is
   the exposed flank. Watch the 26.x betas.
2. **codefuturist/email-mcp** — same name, more stars, launchd scheduler,
   47 tools, IMAP IDLE watcher. A discoverability problem and a
   feature-overlap problem in one repo.
3. **First parties commoditizing read paths** — Google's official remote Gmail
   MCP (the claude.ai connector IS Google's server; deliberately cannot send),
   Microsoft Work IQ Mail (preview; ETags, KQL server-side search, Defender
   tracing). Hosted read/draft is now free; local-first send/schedule/triage
   is what remains contested.
4. **Envelope Index commoditization** — the read story must move up-stack:
   body FTS, threading quality, analytics.
5. **Hosted agent-mail platforms** (AgentMail $6M seed, MailMCP) — different
   category (mail *for* agents), erodes the framing more than the features.

## Ideas worth stealing, prioritized

1. **FTS5 full-body index** (imdinu) — the one real gap in OUR read layer:
   Envelope Index `summaries` holds only first-line snippets, so body search
   silently misses. Highest-value single improvement available.
2. **Graph native deferred send** for the CERN identity —
   `singleValueExtendedProperties` 0x3FEF (PidTagDeferredSendTime): scheduled
   mail that survives a closed laptop, server-side. **Nobody in the ecosystem
   exposes it.** Fits as a fourth transport driver or an ssh-driver upgrade;
   needs Graph/EWS auth to the CERN mailbox — feasibility check first.
3. **Sensitive-op segregation** (Google/Anthropic pattern) — destructive verbs
   as separately-named tools so a fumbled parameter can't reach them. Cheap;
   applies to triage `delete`.
4. **Permission diagnostics as tools** (che) — `check_fda` /
   `check_automation` / `check_accessibility` with remediation hints; the #1
   support burden in this category, and we already hit all three grants.
5. **Benchmark page** (imdinu's playbook) — we'd win the write/schedule/verify
   columns outright; the read columns keep us honest.
6. **`--read-only` flag** + **view-level enums** (MINIMAL/METADATA/FULL per
   read tool) + **batch content fetch** — trust envelope + token budgets; all
   cheap against SQLite.
7. **Per-identity `extra_info`** (mcp-gsuite) — a free-text note in
   identities.toml telling the model what each identity is *for*.
8. **IMAP IDLE / MailKit push** (codefuturist, jayvee6) — event-driven triage
   is an unclaimed high-value slot; pairs with our launchd infra.
9. **Prompt-injection hardening on message bodies** (jgalea fencing,
   tecnologicachile's LLM-malformed-field rejection) — we compose RFC-822 from
   model output; this class applies to us.
10. **Name decision** — codefuturist/email-mcp owns "email-mcp" mindshare at
    83★. Either rename (e.g. the repo is registered as `apple-mail` in MCP
    configs already) or explicitly position against it in the README.

## Corrections to our own scorecard

The 5-competitor table (2026-07-28) was a narrow slice of a ~40-server field.
Two cells need asterisks: read-layer 5/5 holds on speed but **imdinu beats us
on body-search coverage** (FTS5 vs snippets); scheduled-send 5/5 holds on
semantics (frozen spool, identity-aware, verified delivery) but is no longer
*unique*. The two uncontested 5s ecosystem-wide are the ones nobody else even
attempts: plan/apply/verify and identity×transport with external secrets.
