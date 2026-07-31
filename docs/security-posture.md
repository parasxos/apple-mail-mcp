# email-mcp security posture — the destructive surface, stated plainly

*Written for v0.11.0 (branch `v0.11`), 2026-07-31. Companion to
`docs/v1-contract.md`, which says what the tools promise on the wire. This
document says what the tool can BREAK, what stops it, what does not, and
where the guards stop being meaningful at all.*

This tool reads your mail, sends mail as you, deletes mailboxes, installs
launchd agents, and — behind `email-mcp uninstall --purge` — deletes a
directory tree. It also stores a routing file next to your credentials.
Before you point it at a real mailbox you should know exactly how much of
that is fenced and how much is trust.

Every claim below is either backed by a named test or marked as unproven.
Nothing here is aspirational.

---

## 0. Threat model boundary — read this first

**email-mcp is a personal tool that runs as you, with your privileges, on
your Mac.** It is not a service, not multi-tenant, and has no privilege
boundary of its own. That single fact bounds everything else:

- An attacker who can already **write `~/.email-mcp`** can rewrite
  `identities.toml` and route your outgoing mail through their SMTP host.
  No fence in this codebase prevents that, and none can: the same attacker
  can edit `~/.zshrc`, drop a launchd agent, or replace the `email-mcp`
  binary on your PATH. They do not need this tool.
- An attacker who can **set the environment** of the process (`EMAIL_MCP_*`)
  can already run arbitrary code as you.
- An attacker who can **read your home directory** has your mail store
  (`~/Library/Mail`) directly. This tool's spool and FTS index are a
  convenience copy, not a new exposure class.

So what are the fences FOR? Two things, and only two:

1. **Blast-radius containment against the tool's own mistakes.** A path
   override that quietly `chmod 0700`s `/Users`, a purge that follows a
   symlink out of `~/.email-mcp`, a wizard that writes your app password
   into a plaintext TOML — these are things the tool does TO you when
   nobody is attacking. Most of the guards below exist for this case.
2. **A hostile-input boundary at the credential and path layer**, because
   the wizard takes pasted values and the config takes env values, and
   both flow into `mkdir`, `chmod`, `rmtree` and a file that lives beside
   secrets.

Where a finding below is dismissed with "the attacker already has your
home directory", that is not hand-waving — it is the boundary above, and
you should check that you agree with it before you accept the residual
risk.

**Not in scope at all:** the security of Apple Mail, of the SMTP/Exchange
servers you send through, of the macOS Keychain, or of 1Password. This
tool references your credentials; it never holds them.

---

## 1. What is fenced, and how it is proven

### 1.1 `--purge` only ever removes a hardcoded path

`purge_state()` (`email_mcp/lifecycle.py`) removes
`Path.home() / ".email-mcp"` and nothing else, ever. The path is not
configurable and is not derived from any `EMAIL_MCP_*` variable. Fences,
each raising `PurgeRefused` **with nothing removed**:

| Fence | Refuses |
|---|---|
| `raw_home.is_absolute()` on the RAW `$HOME` | a relative `$HOME`, which `realpath` would silently anchor to the cwd |
| `realpath($HOME) != "/"` | `HOME=/` and `HOME=/..` (pathlib preserves `..`, so the value compare is done post-`realpath`) |
| `root.parent == home` | anything not directly under the resolved home |
| `stat.S_ISLNK(os.lstat(root))` | a symlink squatting on `~/.email-mcp` — the link is never followed |
| `stat.S_ISDIR(...)` | a regular file squatting on the path |

Removal is `shutil.rmtree`, whose macOS implementation is fd-based and
does not follow directory symlinks found inside the tree. Env-overridden
state directories are **never** removed by purge; `uninstall_plan()` puts
them in `print_only` and the CLI tells you to delete them yourself.

*Proven by mutation* — `tests/test_lifecycle_redteam.py`:
`test_purge_env_override_at_home_never_removes_home`,
`test_purge_refuses_symlinked_root`,
`test_purge_symlinked_subdir_unlinked_not_followed`,
`test_uninstall_and_purge_idempotent_on_empty_home`. The module docstring
records the specific mutations each guard was broken with (neutering the
`S_ISLNK` branch; swapping `os.lstat` for `os.stat`; replacing `rmtree`
with a resolve-and-descend remover) and that the tests were observed to
FAIL against each.

### 1.2 Nothing is ever chmod'd or deleted THROUGH a symlink

`chmod` and `mkdir` both resolve through a redirection, so a link
squatting on a managed path would land the operation on an arbitrary
victim directory. The rule (design D7) is: **the path AT a link, and every
path behind one, is off-limits.**

- `repairs._tree_links()` collects symlinks squatting on managed state
  dirs; `_files_not_0600()` / `_dirs_not_0700()` exclude them and every
  path behind them. Symlinked-but-wrong-mode paths are *reported* by a
  detect probe and refused by the apply.
- `config.audit_dir()` refuses outright (`AuditDirRefused`) when a symlink
  sits on the DEFAULT ledger path — the one path this package owns.
  `audit.emit()` catches that and drops the event with a warning, so a
  squatting link costs receipts, never a mutation (contract §6).
- `uninstall` refuses to sweep `*.token.json` through a symlinked
  `~/.email-mcp/graph`, and refuses to glob `~/Library/Logs` when that
  directory is a symlink.

*Proven by mutation* — `test_doctor_fix_never_acts_through_symlinked_state_dir`,
`test_uninstall_never_deletes_tokens_through_symlinked_graph`
(`tests/test_lifecycle_redteam.py`); `test_symlinked_audit_dir_works`,
`test_audit_dir_is_a_regular_file` (`tests/test_audit_redteam.py`).

**Two holes remain in this rule — see §2.5 (a check-then-act race in
`audit_dir`) and §2.6 (`~/Library/LaunchAgents` is not covered).**

### 1.3 `identities.toml` is backed up before every clobber

`lifecycle.write_identities()` validates first, then — only if the file
exists — copies it to `identities.toml.bak-<UTCSTAMP>` with `shutil.copy2`
(mode preserved), collision-looping the suffix so a repeated run can never
overwrite an earlier backup. Only then is the new content written to a tmp
file in the same directory, `chmod 0600`, and renamed over the target.
Because validation runs *before* the backup, a refused write touches
nothing at all. The rendered text is round-tripped through `tomllib.loads`
before installation, so a file the loader would reject as malformed is
never installed.

*Proven by mutation* — `test_setup_backs_up_before_clobber_no_bak_no_write`
and `test_interleaved_identity_writes_lose_no_generation`
(`tests/test_lifecycle_redteam.py`); the mutation was "skip the backup
block in `write_identities`".

### 1.4 The wizard refuses to write a secret into `identities.toml`

Four independent fences run on the **write** path
(`lifecycle._validate_tables`):

1. `_fence_secret_keys` — a key literally named `password` / `secret` /
   `token` / … is refused outright, at any nesting depth.
2. `_fence_unknown_keys` — an **allowlist**, not a denylist: the legal
   keys are the identity fields plus the driver constructor's own
   parameters, *derived by `inspect.signature`* so the fence cannot drift
   from the drivers it guards. A misspelled secret key (`smtp_pw`,
   `apikey`, anything) is refused because nothing reads it. A denylist of
   secret-looking names could never have won this.
3. `_fence_secret_refs` — `keychain` must have the shape of a Keychain
   item name; `op` must be a well-formed `op://vault/item/field`
   reference. This catches the realistic paste shapes: Gmail's
   `abcd efgh ijkl mnop` (whitespace), generated passwords (punctuation),
   and an `op://` value misfiled under `keychain`.
4. The identity **name** fence (`^[A-Za-z0-9_-]{1,64}$`) — the name becomes
   `graph/<name>.token.json`, so this is a path-traversal fence, not
   cosmetics.

Additionally, `from_addr` / `from_name` are refused if they contain
CR/LF/NUL, because a header value with a control character would make
every send from that identity fail the compose fence.

Secrets themselves live in the macOS Keychain or 1Password and are read by
the driver at send time. `uninstall` never deletes them — it prints the
exact `security delete-generic-password` commands for you to run.

*Proven by mutation* — `test_wizard_rejects_control_chars_in_header_values`,
`test_identity_name_fence_rejects_hostile_names`,
`test_f8_credential_sentinel_never_reaches_disk_ledger_or_logs`
(`tests/test_lifecycle_redteam.py`). **Limits of fence 3 are stated in
§2.1 — it is shape-based and a short all-alphanumeric password still gets
through.**

### 1.5 `doctor --fix` is a closed whitelist that never destroys anything

`repairs.REPAIRS` is a frozen 8-entry tuple, and a test pins the ids
exactly. Each repair is a pure `detect()` (never mutates, so `--dry-run`
truly touches nothing) plus an `apply()`. The registry, by construction:

- never runs an auth flow;
- never builds or rebuilds derived state (the FTS index — `fts --build` is
  the only builder);
- never installs a launchd agent that was not already installed;
- **never deletes user data.** The one repair that removes an obstruction
  (`audit_path_is_file`, a regular file squatting on the ledger directory)
  *renames it aside* with a UTC stamp and a collision loop — exactly what
  the doctor's printed remedy tells you to do by hand.
- never reads a credential. It chmods `identities.toml`, ledger months and
  Graph token caches to 0600; it never opens them.

`run_fixes()` catches everything a detect or apply raises and reports it in
`failed[]` — the fixer must keep working precisely when things are broken.
An unwritable ledger costs the `doctor_fix` events, never the repairs.

*Proven by* — `tests/test_repairs.py` (registry id pin),
`test_run_fixes_degrades_without_launchctl`,
`test_run_fixes_with_unwritable_audit_dir_never_raises`,
`test_doctor_fix_refuses_state_override_at_home`
(`tests/test_lifecycle_redteam.py`).

### 1.6 State-dir mode discipline

Everything the tool owns holds mail-derived data: the spool holds
fully-composed outgoing messages with bodies and attachments; plans hold
message metadata; the ledger holds recipients and subjects; the graph dir
holds OAuth token caches that grant delegated mailbox access. All state
directories are created and kept at **0700** — including the five spool
subdirectories, which were `0755` until this was measured; `identities.toml`,
ledger months and token caches at **0600**. One qualification, stated rather
than assumed: when a state directory is itself a **symlink**, its mode is
left alone. Chmod follows links, so tightening it would change the mode of
whatever the link points at — a directory this tool does not own. `write_meta()` and the token-cache
writer use the same discipline: tmp file in the same directory, chmod,
atomic rename.

### 1.7 Degradation, not tracebacks

A missing `launchctl`, a corrupt `meta.json`, an unwritable ledger, an
absent Mail store — none of these produce a traceback or abort a lifecycle
run. `read_meta()` returns `{}` on any `OSError`/`ValueError`;
`audit.emit()` never raises (contract §6, emit-failure policy: an
unwritable ledger NEVER blocks mail); every launchd interaction that
cannot be performed is reported and the rest of the run continues.

*Proven by* — `test_corrupt_meta_json_never_tracebacks`,
`test_setup_launchd_step_degrades_without_launchctl`,
`test_uninstall_purge_degrades_without_launchctl`,
`test_bare_invocation_still_serves_after_lifecycle_edits`.

---

## 2. What is NOT fenced — open findings and accepted risk

This section is the point of the document. Each item states the blast
radius plainly, then the reasoning. Nothing here is softened.

Status vocabulary:
**OPEN** — found by the current red-team pass, reproduced, not yet fenced.
**ACCEPTED** — known, understood, deliberately not fixed.

### 2.1 ACCEPTED — a short alphanumeric password still passes the `keychain` fence

`keychain = "hunter2"` is written to `identities.toml` in plaintext.

**Blast radius:** one password, in a 0600 file in your home directory, on
a machine where the attacker who could read it already has your mail
store. It only happens if you paste a password into a field whose prompt
asks for a Keychain *item name*.

**Why not fixed:** a short all-alphanumeric password is
format-indistinguishable from an item name — there is no shape to test.
The fence catches every realistic paste: Gmail app passwords contain
spaces, generated passwords contain punctuation, and an `op://` value
misfiled under `keychain` is caught by prefix. Probing existence with
`security find-generic-password` is not available either: the wizard tells
you to create the Keychain item AFTER setup, so a non-existent item is the
normal case, not the suspicious one.

**What you can do:** after `email-mcp setup`, read `~/.email-mcp/identities.toml`
once. It is short and human-readable by design.

### 2.2 ACCEPTED — `identities.load()` has only the NAME fence, not the reference or allowlist fences

A hand-written `password = "…"` in `identities.toml` is loaded, lands in
the driver params, and surfaces as a caller-fixable `SendError` from
`get_transport` — it is not refused at load time.

**Blast radius:** a file you wrote by hand, containing a secret you chose
to put there, is read back. The tool never *created* that state.

**Why not fixed:** `load()` runs on **every send**. A fence there converts
one stray key in a hand-edited file into total send failure, with no
wizard in the loop to explain the remedy — the failure mode of the guard
is worse than the thing it guards. The **write** path is where the tool
itself could create the leak, and that is where all four fences live
(§1.4).

### 2.3 CLOSED — the degenerate-override fence now lives in the config getters

`lifecycle._degenerate_overrides()` and `repairs._degenerate()` refuse an
`EMAIL_MCP_*_DIR` that resolves to `$HOME` or an ancestor of it, but they
only protect `email-mcp setup` and `doctor --fix`. Every other path reaches
the getters directly: the MCP server via `spool.py` / `plans.py`, the
launchd dispatcher, `doctor.py`, the FTS agent and the Graph token cache.
The getters used to `mkdir` the target and then `chmod 0700` the target
*and its parent*, unconditionally.

**Originally reproduced 2026-07-31** (a throwaway `$HOME` under `/tmp`):

- `EMAIL_MCP_SPOOL_DIR=$HOME` → `$HOME`'s **parent** went `0755 → 0700`,
  and five directories (`pending sending sent failed cancelled`) appeared
  in the home directory.
- `EMAIL_MCP_SPOOL_DIR=<parent of $HOME>` → the **grandparent** went
  `0755 → 0700`. On a real Mac that is `chmod 0700 /Users` — a
  system-visible change breaking other users' traversal.

**Fixed in two steps, and the first was not enough.** `config._lock_down()`
first stopped chmodding the *parent* of an operator-named directory. That
closed the `EMAIL_MCP_SPOOL_DIR=$HOME` case but **not** the worse one: the
target itself was still chmodded, so pointing the override at `/Users`
still produced `chmod 0700 /Users`. A release gate checked only the first
scenario and reported the finding closed; it was not. The fence now also
refuses outright — `config.StateDirRefused` is raised when an overridden
state dir resolves to `$HOME` or above, comparing **resolved** paths so
`$HOME/sub/..` and `//$HOME` cannot walk past it.

**It took three passes to actually close, and the record of that is part
of the disclosure.** Pass one fenced four getters and missed
`config.audit_dir()` entirely — the getter `audit.emit()` calls on *every
mutation*, so `EMAIL_MCP_AUDIT_DIR=/Users` still chmodded `/Users` **and
wrote the ledger month-file into it** on the first send. Pass one also
placed `spool_dir`'s check *after* its five-subdir mkdir loop, so the
refusal fired with `pending/ sending/ sent/ failed/ cancelled` already
created at the refused location. A second release gate caught both by
testing on the installed wheel rather than reading the diff.

**The fence now covers, by name:** `spool_dir`, `plans_dir`, `graph_dir`,
`fts_dir`, `audit_dir`, and `attach_dir` (no chmod there, but a mkdir at
`$HOME` or above is still refused). In every getter the check runs
**before the first mkdir** (`config._refuse_degenerate`). A refused ledger
follows the emit-failure policy: the event is dropped with a logged
warning — receipts are lost, mail is never blocked.

**What is measured, not merely claimed** (regression tests:
`tests/test_config_state_dirs.py`): for each getter × each spelling
(`$HOME`, `$HOME/sub/..`, the parent of `$HOME`) — the refusal is raised,
the target's **mode** is unchanged, **and its contents are unchanged** (no
directory, no ledger file created before the refusal fired). The
contents assertion exists because a gate that measured only the mode
declared this closed while the mkdir still landed.

**Residual:** a refusal is an exception, so a server or dispatcher started
with a degenerate override fails loudly instead of running degraded. That
is the intended trade — the alternative is a mail tool silently changing
the mode of a directory the user did not name.

### 2.4 OPEN (major) — `identities.toml` as a FIFO hangs every lifecycle command forever

`identities.load()` does an unbounded `path.open("rb")` followed by
`tomllib.load(f)`. Neither has a timeout, and neither checks the file
type.

**Reproduced 2026-07-31:** `mkfifo ~/.email-mcp/identities.toml`, then any
call into `identities.load()` blocks indefinitely (probe killed by an
alarm at 5s; it does not return). The same applies to a symlink to
`/dev/zero`, which reads forever instead of blocking.

**Blast radius:** denial of service on every send, every schedule, every
`doctor`, every lifecycle command — with no error, no timeout, and no log
line, just a hung process. Because the launchd dispatcher also calls
`load()`, a hung dispatcher stops scheduled mail from going out silently.

**Why it is only "major":** planting the FIFO requires write access to
`~/.email-mcp`, which is the boundary in §0 — the same attacker can point
your mail at their SMTP host, which is strictly worse. The realistic case
is not an attacker but a botched shell redirection or a restore tool.

**Status:** not fenced. A `stat.S_ISREG` check before the open (with a
clear `IdentityError` when the path is not a regular file) closes it
without changing any success path.

### 2.5 OPEN (minor) — `config.audit_dir()`'s symlink refusal is check-then-act

```
linked = d.is_symlink()      # probe
...
d.mkdir(parents=True, exist_ok=True)
if not linked:
    d.chmod(0o700)           # act
```

A path swapped to a symlink between the probe and the `chmod` lands the
`chmod` on the link's target.

**Blast radius:** `chmod 0700` on one attacker-chosen directory, requiring
a same-machine race against a process that is already running as you.
Tightening, not loosening. This is a textbook TOCTOU, and it is here for
the textbook reason: `pathlib` has no `chmod`-on-fd. The fix is
`os.open(..., O_NOFOLLOW|O_DIRECTORY)` + `os.fchmod`.

**Status:** not fenced; accepted as low-value relative to §0 if it stays
unfixed.

### 2.6 OPEN (minor) — `~/Library/LaunchAgents` is still followed when it is a symlink

`uninstall` refuses to act through a symlinked `~/.email-mcp/graph` and a
symlinked `~/Library/Logs`, but `uninstall_plan()` and `run_uninstall()`
reach `~/Library/LaunchAgents/<label>.plist` with no such check.

**Blast radius:** with `~/Library/LaunchAgents` symlinked elsewhere,
`uninstall` deletes files at the link's target — but only files named
exactly `com.email-mcp.dispatcher.plist`, `com.email-mcp.fts.plist`, or
`com.paris.email-mcp-dispatcher.plist`. It is a real inconsistency with the
D7 rule applied everywhere else, with a very narrow reach.

**Status:** not fenced. Note that a symlinked `~/Library/LaunchAgents` is a
configuration nobody has by accident; if you have one, `uninstall` will
follow it.

### 2.7 ACCEPTED — `_fence_secret_refs` validates a stripped value; the writer stores the raw one

The fence tests `str(table.get(key, "")).strip()`, but `_render_identities`
serializes the raw table value. A value that is only *surrounded* by
whitespace (`" hunter2 "`) passes validation as `"hunter2"` and is stored
with the whitespace intact.

**Blast radius:** identical to §2.1 — one plaintext value in a 0600 file —
except that the stored value also will not resolve in the Keychain, so the
first send fails loudly with `credentials_unavailable`. What is validated
is not what is stored, which is the real defect; the security consequence
is a strict subset of §2.1.

**Why not fixed here:** normalizing the table before validation (strip once,
validate and store the same value) is the correct fix and is a one-line
change; it is called out so it does not get lost. Left open deliberately
rather than papered over.

### 2.8 ACCEPTED — inconsistent chmod policy across the state getters

`config.audit_dir()` deliberately skips the `chmod` when the operator named
a symlink with `EMAIL_MCP_AUDIT_DIR` ("its target's mode is not ours to
set"). `spool_dir()`, `plans_dir()`, `graph_dir()` and `fts_dir()` do not
make that distinction — they chmod the target and the parent
unconditionally, symlink or not.

**Blast radius:** subsumed by §2.3. Fixing §2.3 in the getters is the
natural place to make the policy uniform.

### 2.9 ACCEPTED — secret *references* appear in error prose

Contract §7 promises that secret **values** never leave the driver. It does
not promise that references do — a Keychain item name or an `op://` path
CAN appear in an error message, and by design does, because that is what
makes the error fixable ("Keychain item `work-smtp` not readable").

**Blast radius:** an agent transcript or a log file may contain the *name*
of a Keychain item or a 1Password reference path. It never contains the
secret. If your item names are themselves sensitive, that is a leak for
you; for most users it is the difference between a fixable error and a
mystery.

`tests/test_lifecycle_redteam.py::test_f8_credential_sentinel_never_reaches_disk_ledger_or_logs`
and `tests/test_audit_redteam.py::test_f5_body_sentinel_grep_is_empty` pin
the value side of this: a sentinel credential planted in the flow is
grepped for across every file the tool writes, and must not appear.

### 2.10 ACCEPTED — the documented event-loss window

The audit ledger is an **index of truth, not a second source of it**. A
process killed between a durable mutation and its `emit()` loses that one
event; while the ledger directory is unwritable, events are dropped and the
drop is logged. Events are therefore **at-most-once per mutation**.

**Blast radius:** absence of an event is never proof that a mutation did
not happen. If you are reconciling what was sent, the primary artifacts —
the spool manifest, the plan file, the Bcc-to-self copy, Exchange Sent
Items — are authoritative; the ledger is the index.

This is contract §6 and it is deliberate: the alternative (block the send
until the receipt is durable) makes an unwritable disk into an outage.

### 2.11 ACCEPTED — scheduled delivery is at-least-once

A crash between the transport accepting a message and the `sending`→`sent`
rename leaves the entry in `spool/sending/`, and `_recover_stranded`
requeues it after 10 minutes precisely because the outcome is not knowable
from disk. **That window re-delivers.** Dedupe on the manifest's frozen
`Message-ID`. Contract §4 states this on the `schedule_email` row.

---

## 3. What the tool will never do

These are design commitments, not incidental behavior:

- **`~/Library/Mail` is never written.** All mail mutation goes through
  Mail.app's own scripting interface or through a transport; the store is
  read-only to this tool. The one way this was falsifiable has been closed:
  pointing `EMAIL_MCP_SPOOL_DIR` at the mail store made a plain
  `email-mcp doctor` — no `--fix` — create the five spool subdirectories
  inside it and take it `0755 → 0700`, because the read-side check called a
  getter that creates. The state-dir getters now take `create=False`, and
  every read path (`doctor`, `spool.entries`, the dispatcher's log-path
  resolution) uses it. Verified: `doctor` against a mail-store-pointed
  spool dir now creates nothing and leaves the mode untouched.
- **Keychain and 1Password items are never deleted.** `uninstall` prints
  the `security delete-generic-password` commands and stops.
- **`triage_plan` cannot delete.** `delete` through `triage_plan` raises
  `destructive_action`; deletion has its own tool with its own, smaller cap
  (50 vs 200 messages).
- **Caps reject, never truncate** on the mutation paths — an over-cap
  selection is refused, never silently trimmed to fit.
- **`EMAIL_MCP_READ_ONLY=1` removes the nine mutating tools from the
  registration**, verified at 11 tools registered vs 20. They do not exist
  in that session; there is no runtime check to bypass.
- **No exception crosses the MCP wire** (contract §7): every one of the 20
  tools is wrapped in a belt that maps exceptions to coded failure
  envelopes and logs the traceback to the log file, never to stdout.

---

## 4. How to re-run the attacks

```
.venv/bin/python -m pytest tests/test_lifecycle_redteam.py \
                          tests/test_audit_redteam.py \
                          tests/test_lifecycle_uninstall.py \
                          tests/test_repairs.py \
                          tests/test_wire_belts.py -q
```

| File | Covers |
|---|---|
| `tests/test_lifecycle_redteam.py` | purge fences, `$HOME` state overrides, symlinked state dirs/graph dir, identity-name traversal, backup-before-clobber, credential sentinel, launchctl-absent degradation |
| `tests/test_audit_redteam.py` | two-process append integrity, torn/corrupt lines, unwritable and unchmodable ledger, body sentinel, month rollover, single-`os.write` atomicity, symlinked and file-squatted ledger paths |
| `tests/test_lifecycle_uninstall.py` | the `EMAIL_MCP_SPOOL_DIR=$HOME` headline case, uninstall planning, print-only env overrides |
| `tests/test_repairs.py` | the frozen repair registry (id pin), detect purity, apply behavior |
| `tests/test_wire_belts.py` | poisoned sources on every tool; exceptions whose `__str__` or type name itself raises |

The `tests/test_lifecycle_redteam.py` module docstring names the exact
mutation used to prove each guard (break the guard, observe the test fail,
restore). If you add a guard, add its mutation there — a test that passes
whether or not the code is correct is worse than no test.

**The findings in §2.3–§2.6 are reproducible by hand, not by the suite** —
they were found after those tests were written and have no test yet.
That is stated here rather than hidden: the suite passing is not the same
as the surface being clean.

---

## 5. Change log for this document

This file is updated whenever a finding is opened, closed, or accepted.
An accepted risk that later gets fixed moves to §1 with its test named; an
open finding that gets fenced does the same. Items are never deleted —
a reader comparing releases should be able to see what changed its status.
