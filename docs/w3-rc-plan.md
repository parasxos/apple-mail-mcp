# W3 — the v1.0-rc programme

*Written 2026-07-31 on branch `v0.11`. The fourth movement of
`docs/v1-roadmap.md` ("the RC — claims that survive repetition"),
executed against the frozen surface of `docs/v1-contract.md`. This
document is the plan `tools/rc_runner.py` runs: each phase names its
lane, its acceptance criterion, and whether a human is in the loop. It
is the thing to read before an RC pass and the thing to update when a
phase's meaning changes — the runner's `PLAN` table mirrors it and the
suite asserts the two agree.*

Sections: 1 the two lanes · 2 the Sentinel · 3 the phases P01–P18 ·
4 running it · 5 what "passed" means.

---

## 1. The two lanes

The RC has to prove two different things, and they do not fit in one
environment.

**The sandbox lane** answers *"does a stranger's Mac work?"* It runs in
a throwaway `$HOME` with a freshly installed wheel, its own
`~/.email-mcp` tree, its own identities, its own launchd agents. The one
thing it borrows from reality is the mail: the real Mail store is
attached through `EMAIL_MCP_MAIL_DIR` and only ever read. Nothing in the
sandbox lane is allowed to name the real state root — the runner refuses
such a command outright (`UnsafeAction`) rather than trusting the phase
body to have been careful.

**The prod lane** answers *"does it work here, on the real estate?"* It
operates the operator's actual install: real identities, real Graph
tokens, real agents, real Mail. Every send in this lane is self-only.
The prod lane exists because the sandbox cannot prove the things that
only a real account has — an Exchange deferred draft, a lid-closed
delivery, a full non-stale index, an FDA revocation.

Lane `both` means the phase runs its own comparison across the two: the
same verb, twice, with different expectations (P04 indexes with
`--limit` in the sandbox and asserts fullness in prod).

## 2. The Sentinel

Sandbox launchd actions share the per-user label space with the prod
agents. A sandbox phase that boots out `com.email-mcp.dispatcher` does
not have to *name* the operator's dispatcher to kill it — there is only
one. That single fact is why the RC carries a witness.

Before any phase runs, the Sentinel takes:

- a **sha256 + mode manifest** of every path under the real
  `~/.email-mcp` (symlinks recorded, never followed), and
- the **state of every prod launchd label** (`com.email-mcp.dispatcher`,
  `com.email-mcp.fts`, and the v0.9 legacy label P15 resurrects), read
  through `launchctl print`, digested with the volatile fields (pid,
  run count, exit code) stripped so ordinary ticking is not drift.

After the pass it takes both again and says exactly what changed. It
distinguishes the churn a run legitimately produces — the ledger gains
an event per mutation, the spool moves manifests between states, the
index rebuilds, plans are written and GC'd, a Graph send rotates its
token cache — from anything that touched `identities.toml`, `meta.json`,
an unknown new file, or the agents. A *removed* token cache is always
material, even though rewriting one is routine.

Three rules make the witness load-bearing rather than decorative:

1. **It refuses to start if it cannot read the state.** An absent tree,
   an unreadable subdirectory, a chmod-000 file: the run does not begin
   (exit 3). An RC that cannot see the estate it might damage has no
   business running.
2. **`--no-sentinel` may not be combined with `--execute`.** Planning
   without a witness is fine; acting without one is a usage error.
3. **A resumed pass compares against the ORIGINAL baseline.** Drift a
   crashed pass caused cannot be laundered by re-capturing.

P16 raises the bar for itself: its claim is that uninstall + purge leave
the real estate byte-identical, so it is verified in *strict* mode with
zero expected churn allowed, immediately after it runs.

## 3. The phases

Lane: `sandbox` · `prod` · `both`. Mode: `auto` (the runner asserts) ·
`MANUAL` (a human answers, and an unattended run records the step
PENDING rather than inventing a verdict).

| # | Phase | Lane | Mode | Acceptance criterion |
|---|---|---|---|---|
| **P01** | wheel install | sandbox | auto | The built wheel installs into the sandbox `$HOME` and `email-mcp version` reports the wheel's version — not the repo working tree's. |
| **P02** | scripted setup | sandbox | auto | `email-mcp setup`, driven from a fixture answer file, leaves a 0700 tree with a 0600 `identities.toml`, leaves the FTS index unbuilt, writes no secret VALUE anywhere, and prints an MCP config whose entry point is absolute. |
| **P03** | doctor | both | auto | Every check green in the sandbox. In prod, doctor reports the real estate healthy — and for anything it isn't, the failure names a concrete fix (a command or a Settings pane), per contract §2. |
| **P04** | index | both | auto | Sandbox: `fts --build --limit N` completes and its documents are searchable. Prod: `fts --status` shows a full, non-stale index — the number the operator would actually get. |
| **P05** | wire-level search/read | sandbox | auto | Driven through a real **MCP client subprocess** over stdio, not in-process: `search_emails` and `get_email` return contract envelopes, and no exception ever reaches the wire (contract §7). |
| **P06** | send | both | auto | A self-only send delivers on each lane and its Message-ID is found in the store afterwards. Both lanes, because delivery is the one claim a fake store cannot make. |
| **P07** | schedule via launchd, then cancel | sandbox | auto | `schedule_email` freezes a spool entry; the dispatcher agent ticks and delivers it; a second entry is cancelled with `cancel_scheduled` and lands in `cancelled/` with its manifest intact. |
| **P08** | schedule via graph | prod | auto | A Graph-executor schedule lands as a deferred draft in the real tenant (`PidTagDeferredSendTime`), and cancelling removes it cleanly. Prod-only: there is no sandbox tenant. |
| **P09** | lid-closed delivery | prod | **MANUAL** | A scheduled send delivers with the lid closed, within the tolerance documented in `docs/transport-design.md`. Evidence: the delivery timestamp and the time the lid went down. |
| **P10** | triage plans | sandbox | auto | Plan → apply for move and flag, verified against the store (not against the tool's own report), and the audit event carries the plan's summary line — the one that must outlive plan GC. |
| **P11** | trash plan | sandbox | auto | `triage_plan_delete` → apply lands exactly the planned messages in Trash, verified against the store; nothing outside the plan moves. |
| **P12** | audit inspection | both | auto | Exactly **one** audit event per mutation this run performed, no more and no fewer, and every event threads to its `operation_id` — the plan ids, spool ids and Message-IDs minted earlier in the pass are all findable in one `audit` call. |
| **P13** | failure matrix FM1-FM10 | sandbox | auto | Ten injected failures, each producing its coded envelope and losing no mail. See the matrix below. |
| **P14** | permission revoke / regrant | prod | **MANUAL** | With Full Disk Access revoked, doctor names the exact fix rather than crashing; after regrant every check is green again. Evidence: the doctor output from both sides. |
| **P15** | upgrade from v0.9 | sandbox | auto | A git worktree at `75c6f93` generates **real** v0.9 state — spool manifests in the old shape, the legacy launchd plist, a v0.9 config — and the current wheel operates it: migrations run, the legacy label is retired, and one v0.9-frozen spool entry actually delivers. Not a fixture: the old code writes the state. |
| **P16** | uninstall + purge | sandbox | auto (strict) | `email-mcp uninstall` removes the sandbox agents and token caches and purges its tree; the Sentinel then proves, in strict mode, that the real estate is byte-identical and the prod agents untouched. |
| **P17** | teardown | prod | auto | The prod agents are re-bootstrapped and the dispatcher is **observed to tick within 90 s** — the run is not over because the runner said so, it is over because the real dispatcher ran again. |
| **P18** | fresh macOS user account walk | prod | **MANUAL, once** | A brand-new macOS account reaches a working read-only server in under 15 minutes with no archaeology — the roadmap's lifecycle claim, tested by the only honest method. Recorded once; later passes skip it. |

### The failure matrix (P13)

| | Injection | Expected |
|---|---|---|
| FM1 | `kill -9` mid-FTS-build | the index is rebuildable, no partial rows served, doctor names it |
| FM2 | `kill -9` mid-dispatcher | the in-flight spool entry is recoverable; no double send |
| FM3 | the at-most-once window | **DEMONSTRATED, not reconciled** — the window where a delivery may be lost rather than duplicated is exhibited and its size measured. v1.0 does not claim exactly-once |
| FM4 | corrupt spool manifest | coded envelope, entry parked, siblings unaffected |
| FM5 | corrupt FTS db | search degrades to snippet-only, doctor offers the rebuild |
| FM6 | corrupt ledger line | `audit` skips the line and reports `skipped_lines`; the ledger keeps serving |
| FM7 | duplicate `triage_apply` of one plan | idempotent per contract §4; no second mutation, no second event |
| FM8 | chmod 000 on the state tree, then `doctor --fix` | the repair is proven **in anger**, not in a unit test |
| FM9 | token cache removed | a coded auth failure with a fix, never a traceback |
| FM10 | Mail.app quit mid-operation | coded `mail_unavailable`, nothing half-applied |

## 4. Running it

```
tools/rc_runner.py                       # dry run: prints the plan, does nothing
tools/rc_runner.py --list                # the plan table, no state read at all
tools/rc_runner.py --execute             # the real pass (the only opt-in)
tools/rc_runner.py --execute --phase P05-P09
tools/rc_runner.py --execute --resume    # continue the newest journal
tools/rc_runner.py --execute --interactive   # answer MANUAL steps at the prompt
tools/rc_runner.py --soak-report         # per-phase pass rate across all runs
```

**Dry-run is the default.** Without `--execute` the runner spawns no
process, writes no file, journals nothing, and renders the report to
stdout instead of `docs/`. Effects require a human to type the flag.

**The report** is `docs/rc-report-<date>.md`, appended to as the pass
goes so a crash keeps the evidence it earned. Each phase contributes a
section with its acceptance criterion as a checkbox — checked only on a
pass — and whatever evidence the phase recorded.

**The journal** is `~/.email-mcp-rc/rc-<timestamp>.json`, deliberately
outside the state root so the runner's own bookkeeping can never
register as drift in its own manifest (the runner refuses a
`--state-dir` inside it). It is flushed *before* each phase starts, so a
`kill -9` — which this RC performs on purpose — leaves a journal naming
the phase that was in flight. `--resume` re-runs that phase and skips
everything already settled.

**Manual steps** print what to do and take `pass` / `fail` / `skip`,
optionally followed by `: evidence`. With no operator attached the step
is recorded PENDING and the pass continues; a pending step keeps the
whole run INCOMPLETE. An unattended run never invents a human verdict.

Exit codes: `0` passed (or dry run) · `1` a phase failed or is unbound ·
`2` usage · `3` the Sentinel refused to start · `4` material sentinel
drift · `5` incomplete (manual steps outstanding).

## 5. What "passed" means

The roadmap's gate is *"E2E life story passes repeatedly, incl.
upgrade-from-v0.9; no critical/major findings open"*. Concretely, the
RC has passed when:

1. every automated phase P01–P17 is green **in the same pass**, on both
   lanes, with the Sentinel clean at the end;
2. that pass has been repeated — `--soak-report` shows the repetitions,
   and a phase that is green only sometimes is not green;
3. the three MANUAL steps have a recorded operator verdict with
   evidence, P18 at least once ever;
4. the report for the passing run is committed. The artifact carries the
   proof — that is the whole point of v1.0.

### Implementation status

`tools/rc_runner.py` ships **R1**: the runner core — Context, Report,
Sentinel, the phase registry, resume / `--phase` / `--dry-run` plumbing
and the manual-step protocol, covered by `tests/test_rc_runner.py`. The
18 phase bodies are R2; they attach with `@implements("P07")` and until
they do, a phase reports `unimplemented` rather than passing silently —
a live pass stops at the first hole in the life story.

R2 attaches stage by stage. **S1** (2026-08-02) binds the sandbox core
P01–P05: wheel install, scripted setup (fixture:
`tests/fixtures/setup_answers.json`, rewritten for today's wizard incl.
the two Exchange questions), doctor on both lanes, the bounded sandbox
index build + round-trip search probe with the prod fullness check, and
the wire-level search/read driven through a real MCP client subprocess.
Two plan-text corrections rode along: the version verb is
`email-mcp version` (the CLI has no `--version` flag), and setup's leaf
materialization now creates an *empty* `fts/` dir by design — the P02
claim is that the index stays unbuilt.
