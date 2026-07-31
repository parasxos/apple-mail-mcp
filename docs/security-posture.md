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
victim directory. The rule (design D7) is: **modes are never changed at a
link or behind one.**

Stated precisely, because the absolute phrasing this once carried —
"every path behind a link is off-limits" — is not what the code does and
invited a reviewer to report supported behaviour as a breach. Relocating
the state root by symlinking `~/.email-mcp` onto another volume **is a
supported shape**: the link is followed, directories under it are ours to
create `0700`, and the link's target keeps its own mode. What is refused
is a link squatting on a managed *leaf* we own, such as
`~/.email-mcp/audit` (`AuditDirRefused`). Following a redirection the user
chose is not the risk; changing the mode of whatever sits behind it is.

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

### 1.6 One state root, and the created-only chmod rule

Everything the tool owns holds mail-derived data: the spool holds
fully-composed outgoing messages with bodies and attachments; plans hold
message metadata; the ledger holds recipients and subjects; the graph dir
holds OAuth token caches that grant delegated mailbox access.

**Since v0.11 there is exactly one configurable location for the state
this tool generates.**
`EMAIL_MCP_STATE_DIR` names a single root (default `~/.email-mcp`), and
`spool`, `plans`, `graph`, `fts` and `audit` are derived from it as fixed
leaf names. The five per-directory overrides are retired (§1.6.5).

Three things sit outside the root, and saying so is part of the claim
being true. `identities.toml` is CONFIG you author, with its own variable
(`EMAIL_MCP_IDENTITIES`); `meta.json` is the install stamp and stays
anchored at `~/.email-mcp` whatever the root is; and `attach_dir` sits
outside — deliberately, under `$TMPDIR`, because
materialised attachments are transient extracts the OS may reap, and
folding them into the root would make `uninstall --purge` delete them and
make them survive reboots.

This is not a tidier spelling of the old model. It is the reason the old
class of finding is gone: with one root there is a single place to
validate and a single thing to relocate. Three release gates in a row had
found a *different* way past the per-directory fences — a getter nobody
had fenced, a fence that ran after the mkdir it was guarding, a path
comparison that missed every spelling of `$HOME` on a case-insensitive
volume. The configuration surface was itself the defect, so it was removed
rather than fenced harder.

#### 1.6.1 Created-only chmod

**A directory this tool creates is 0700. A directory that already existed
is never chmodded at all.** That is the whole rule, and it lives in one
function (`config._make_ours`): `mkdir(exist_ok=False)` succeeds → we made
it → `chmod 0700`; `FileExistsError` → it was already there → we touch
nothing.

"email-mcp changed the mode of a directory I did not name" is therefore
**unrepresentable**, not fenced. Nothing on the configuration path — not
`setup`, not the first send, not the dispatcher — re-modes an existing
directory. Files the tool writes are 0600 (`identities.toml`, ledger
months, token caches, `meta.json`, the root marker); `write_meta()` and the
token-cache writer use tmp-in-the-same-directory, chmod, atomic rename.

The counterpart matters as much as the rule: a loose mode is still a
**finding**. `doctor` reports it (`state_root`, `spool_plans`, `audit`
checks) and `doctor --fix` repairs it — on explicit request. What
configuration will not do silently, the fixer will do openly.

#### 1.6.2 The ownership marker

A managed root carries `.email-mcp-root` (0600, JSON, `root_version 1`).
It is written when the root is adopted and is a **safety hint, not a
lock** — writing it is best effort, so a read-only volume cannot break
sending.

It answers one question: *may this tool manage this directory?*

#### 1.6.3 A non-empty unmarked root is refused

If `EMAIL_MCP_STATE_DIR` names an **existing** directory that holds files
and carries no marker, the tool refuses (`config.StateDirRefused`) —
**before any filesystem effect**. This is what makes `$HOME`, `/Users` and
every case-variant and firmlink of them refusable without comparing a
single path string. (`$HOME` and its ancestors are *also* refused
explicitly, by inode identity, so an empty home directory is refused too.)

Three qualifications, stated rather than assumed:

- The **default** root (`~/.email-mcp`, no variable set) is never refused
  for its contents. Every v0.10 install has one, full of our own files and
  with no marker; adopting it is the upgrade path.
- The marker itself does **not** count as foreign content. Counting it made
  a root the tool had just adopted look foreign — see §1.6.4.
- An **empty** directory is a valid relocation target: relocating onto
  another volume is supported and is why the rule is "must be ours", not
  "must be under `$HOME`".

#### 1.6.4 The adoption race (found and fixed at the v0.11 gate)

Two writers exist by design — the MCP server and the launchd dispatcher.
The refusal check was `_marked(root)` … then `any(root.iterdir())`, and
those are two separate syscalls. On the first mutation after a root was
configured, one process could pass the marker probe, have the *other*
process adopt the root underneath it, and then read that adoption — the
marker file itself — as "someone else's files". The mutation was refused.

Reproduced at roughly 1 run in 20 under load with two concurrent writers;
the cost was a dropped audit event or a failed `schedule_email`, on a
correctly configured system.

Fixed by excluding the marker from the emptiness scan **and** re-reading
the marker after the scan: the refusal is based on the marker as it stands
*after* the content is observed, not before.

*Proven by* — `test_marker_alone_does_not_make_a_root_look_foreign`,
`test_root_adopted_between_probe_and_scan_is_not_refused`,
`test_refusal_still_fires_for_genuinely_foreign_content`
(`tests/test_config_state_dirs.py`).

#### 1.6.5 Symlinks: a chosen root may be one, a managed leaf may not

- **Root symlink → followed, target's mode untouched.** Relocating
  `~/.email-mcp` (or `EMAIL_MCP_STATE_DIR`) with a link is a supported
  shape. The link is followed and the leaves are created under it, but the
  target keeps its own mode — we did not create it, so §1.6.1 applies.
- **Leaf symlink → refused.** A link squatting on `<root>/spool`,
  `<root>/audit`, etc. is a squat, not a choice: `mkdir` and `chmod` both
  resolve through it, so the ledger or the spool would be written into an
  arbitrary victim directory and its mode tightened. `StateDirRefused`.

A refused ledger follows the emit-failure policy: the event is dropped
with a logged warning. Receipts are lost; mail is never blocked.

#### 1.6.6 Read-side purity

Every managed getter takes `create=False`, which **resolves the path and
touches nothing**. `doctor`, `audit.query()`, `spool.entries()`,
`fts.status()` and `uninstall` planning all use it. A plain `doctor` over a
completely absent state tree creates nothing at all.

`state_root(create=False)` is **total** — it never raises, because
uninstall planning and `doctor` must be able to *name* a root even when it
is refusable. But it no longer hands out an unvalidated path to code that
then writes: ask `config.state_root_refusal()` (pure; stats, never
creates) for the verdict, and anything intending to write goes through
`create=True`, which raises. See §2.3.

**One honest exception, stated rather than glossed:** the debug logger
creates its own file (`~/Library/Logs/email-mcp.log`, and `~/Library/Logs`
if absent) when the package is imported. That is not state — it is outside
the root, `uninstall --purge` removes it, and `EMAIL_MCP_LOG_FILE=off`
suppresses it entirely. Measured: with logging off, a run of `doctor.run()`,
`audit.query()`, `uninstall_plan()` and `run_fixes(dry_run=True)` against a
`HOME` containing a deliberately 0755 state tree creates **nothing** and
changes **no** mode. The read-side guarantee is about state and modes; the
log is neither, and this paragraph exists so nobody has to discover the
difference by auditing.

*Proven by* — `tests/test_config_state_dirs.py`: created-only
modes, marker semantics, refusal without any filesystem effect (mode *and*
contents unchanged), root/leaf symlink behaviour, read-side purity,
`doctor --fix` repairing on request.

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
refuses outright — `config.StateDirRefused` is raised when the state root
resolves to `$HOME` or above, compared by **inode identity** (`st_dev`,
`st_ino`) rather than by spelling, so `$HOME/sub/..`, `//$HOME`, case
variants on APFS and the `/System/Volumes/Data` firmlink are all one
fence.

**It took three passes to actually close, and the record of that is part
of the disclosure.** Pass one fenced four getters and missed
`config.audit_dir()` entirely — the getter `audit.emit()` calls on *every
mutation*, so `EMAIL_MCP_AUDIT_DIR=/Users` still chmodded `/Users` **and
wrote the ledger month-file into it** on the first send. Pass one also
placed `spool_dir`'s check *after* its five-subdir mkdir loop, so the
refusal fired with `pending/ sending/ sent/ failed/ cancelled` already
created at the refused location. A second release gate caught both by
testing on the installed wheel rather than reading the diff.

**v0.11 stopped fencing this and deleted the surface instead.** There are
no per-directory overrides left to point at `$HOME`. One variable names one
root; the root is validated once, before any effect; and nothing
pre-existing is ever chmodded, so "the fence missed a getter" has no
meaning any more. §1.6 states the model that replaced this one.

**Two defects in the replacement were found by the v0.11 gate itself**, and
the record of that belongs here:

1. **`state_root(create=False)` short-circuited before validating.** The
   `create` flag was meant to skip the *effect*; it skipped the *checks*
   too. So the read-side resolver handed back `$HOME` as a state root, and
   `doctor --fix` — which resolved `<root>/spool` from it and then wrote —
   created `$HOME/spool`, `$HOME/plans`, `$HOME/graph`, `$HOME/audit`. The
   exact scattering this section was written about, reintroduced through
   the read-side door. Validation now belongs to resolution: only the
   effect is conditional on `create`, `state_root_refusal()` is the single
   pure definition of "refusable", and `repairs` creates through the
   production getters rather than a parallel `mkdir`+`chmod` of its own.
2. **The adoption race** — see §1.6.4.

**What is measured, not merely claimed** (`tests/test_config_state_dirs.py`):
for each spelling of `$HOME` (`$HOME`, `$HOME/sub/..`, `//$HOME`, case
variants) and for each managed getter — the refusal is raised, the
target's **mode** is unchanged, **and its contents are unchanged** (no
directory, no ledger file created before the refusal fired). The contents
assertion exists because a gate that measured only the mode declared this
closed while the mkdir still landed. `doctor --fix` over a refused root is
asserted to leave `$HOME` byte-for-byte and mode-for-mode as it found it.

**Residual:** a refusal is an exception, so a server or dispatcher started
with a refusable root fails loudly instead of running degraded. That is the
intended trade — the alternative is a mail tool silently changing the mode
of a directory the user did not name. `doctor` reports the refusal (the
`state_root` check) rather than leaving the tool mysteriously inert; before
v0.11's gate it did not, and a refused configuration produced a **green**
doctor while every mutation dropped its receipts.

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

### 2.5 CLOSED — the ledger's symlink refusal was check-then-act

The old shape probed `d.is_symlink()`, then `mkdir(exist_ok=True)`, then
`chmod` if the probe said "not a link" — so a path swapped to a symlink
between probe and `chmod` landed the `chmod` on the link's target.

**Closed by construction in v0.11, not by tightening the window.** A leaf
is created with `mkdir(exist_ok=False)`, and the `chmod` happens **only on
the branch where that `mkdir` succeeded** — i.e. only for a directory that
did not exist a syscall earlier and that we therefore created ourselves. A
link cannot be swapped onto a path that `mkdir` just created; if the swap
wins the race, `mkdir` raises `FileExistsError` and no `chmod` runs at all.
There is no probe to invalidate.

**Residual (minor, accepted):** a *symlinked leaf that already exists* is
detected with `is_symlink()` and refused, which is still a probe — but the
act it guards is a refusal, not a `chmod`. Losing that race means the tool
proceeds to `mkdir(exist_ok=False)` on the swapped path, which fails. The
failure mode is a dropped event, never a `chmod` through the link.

**Related, still open:** the ledger month-file is opened `O_NOFOLLOW` and
its mode set with `os.fchmod` **on the fd it just wrote** — so a link
swapped onto the month path between the write and the chmod cannot
redirect it. That is the `os.open`/`fchmod` fix this entry used to ask for,
applied where a second writer actually races
(`test_emit_chmods_the_fd_it_wrote_not_the_path`).

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

### 2.8 CLOSED — chmod policy is now uniform across every state getter

The five getters used to disagree: `audit_dir()` skipped the `chmod` for an
operator-named symlink while `spool_dir()`, `plans_dir()`, `graph_dir()`
and `fts_dir()` chmodded the target and its parent unconditionally.

There is now **one** implementation. Every leaf goes through
`config._leaf()` → `config._make_ours()`, so the created-only rule (§1.6.1)
and the leaf-symlink refusal (§1.6.5) apply identically to all five, and no
getter chmods a parent at all. `attach_dir()` — the one managed directory
outside the root — uses the same `_make_ours()`.

*Proven by* — the `LEAF_GETTERS` parametrisation in
`tests/test_config_state_dirs.py` runs the mode, symlink and
purity assertions across all five getters rather than one.

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

### 2.10 ACCEPTED — the documented event-loss window (audit-loss policy)

The audit ledger is an **index of truth, not a second source of it**.
Events are **at-most-once per mutation**. Three ways an event is lost:

1. the process is killed between a durable mutation and its `emit()`;
2. the ledger directory is unwritable (0500, full disk, absent parent);
3. the ledger location is **refused** — a symlinked `<root>/audit` leaf, a
   refusable root, a retired variable still set.

**In all three the mutation stands and the receipt is dropped.** `emit()`
never raises; it logs one warning per dropped event. An audit failure must
never block or undo an email mutation, and it never does — including for
the refusal cases, which are caught inside `emit()` and degraded to the
same drop-and-log. The one thing that *is* blocked by a refused root is a
mutation which needs the root for its own durability (`schedule_email`
needs somewhere to freeze the message) — that is an inability to mutate,
not an audit failure vetoing a mutation.

**Blast radius:** absence of an event is never proof that a mutation did
not happen. If you are reconciling what was sent, the primary artifacts —
the spool manifest, the plan file, the Bcc-to-self copy, Exchange Sent
Items — are authoritative; the ledger is the index.

This is contract §6 and it is deliberate: the alternative (block the send
until the receipt is durable) makes an unwritable disk into an outage.

*Proven by* — `test_f4_unwritable_ledger_never_blocks_mail` (0500 ledger
leaf: send, schedule, plan build and mailbox create all return their normal
results, five events dropped, five warnings, not one byte written),
`test_f6_kill_between_mutation_and_emit` (SIGKILL at the emit boundary:
the spool entry is durable and deliverable, the event is absent, and
neither `doctor` nor the audit tool crashes over the gap).

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
  read-only to this tool. The one way this was falsifiable has been closed
  twice over: pointing `EMAIL_MCP_SPOOL_DIR` at the mail store made a plain
  `email-mcp doctor` — no `--fix` — create the five spool subdirectories
  inside it and take it `0755 → 0700`, because the read-side check called a
  getter that creates. The state-dir getters now take `create=False`, and
  every read path (`doctor`, `spool.entries`, the dispatcher's log-path
  resolution) uses it. Since v0.11 the variable that aimed it there does
  not exist, aiming `EMAIL_MCP_STATE_DIR` at a mail store is refused (it is
  non-empty and unmarked), and nothing pre-existing is chmodded even if a
  path did reach a getter. Verified: `doctor` over a completely absent
  state tree in a pristine `HOME` creates nothing.
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
.venv/bin/python -m pytest tests/test_config_state_dirs.py \
                          tests/test_lifecycle_redteam.py \
                          tests/test_audit_redteam.py \
                          tests/test_lifecycle_uninstall.py \
                          tests/test_repairs.py \
                          tests/test_wire_belts.py -q
```

| File | Covers |
|---|---|
| `tests/test_config_state_dirs.py` | the whole §1.6 model: created-only chmod, marker semantics, refusal of a non-empty unmarked root (mode *and* contents unchanged), the adoption race, root/leaf symlinks, read-side purity, `create=False` totality, `doctor --fix` on request |
| `tests/test_lifecycle_redteam.py` | purge fences, a state root at `$HOME`, symlinked state dirs/graph dir, identity-name traversal, backup-before-clobber, credential sentinel, launchctl-absent degradation, un-creatable root |
| `tests/test_audit_redteam.py` | two-process append integrity, torn/corrupt lines, unwritable ledger leaf, body sentinel, month rollover, single-`os.write` atomicity, symlinked and file-squatted ledger paths, kill-at-the-emit-boundary |
| `tests/test_lifecycle_uninstall.py` | the `EMAIL_MCP_STATE_DIR=$HOME` headline case, uninstall planning, print-only env overrides |
| `tests/test_repairs.py` | the frozen repair registry (id pin), detect purity, apply behavior, retired-variable rejection |
| `tests/test_wire_belts.py` | poisoned sources on every tool; exceptions whose `__str__` or type name itself raises |

The `tests/test_lifecycle_redteam.py` module docstring names the exact
mutation used to prove each guard (break the guard, observe the test fail,
restore). If you add a guard, add its mutation there — a test that passes
whether or not the code is correct is worse than no test.

**Every §1.6 regression in this pass was verified by reverting the fix and
observing the test fail** — seven of them, against the pre-fix source. Two
of the findings (§2.3.1, §1.6.4) were found *by* running the suite in
environments the repo checkout does not exercise: a pristine `HOME`, an
installed wheel on a different Python, and the suite under load and in
randomised order. The suite passing in one environment is not the same as
the surface being clean.

**Run it in more than one place.** The v0.11 gate ran the full suite four
ways — repo checkout, pristine `HOME` (no `~/Library/Mail`, no inherited
`EMAIL_MCP_*`), installed wheel in a clean venv on Python 3.12, and three
randomised orderings — and the wheel run is what surfaced a cross-module
test-isolation leak (`audit.set_process("cli")` escaping `run_fixes`) that
alphabetical ordering had been hiding.

---

## 5. Change log for this document

This file is updated whenever a finding is opened, closed, or accepted.
An accepted risk that later gets fixed moves to §1 with its test named; an
open finding that gets fenced does the same. Items are never deleted —
a reader comparing releases should be able to see what changed its status.

### v0.11 — one state root

- **§1.6 rewritten.** The per-directory model it described is gone. New:
  single root, ownership marker, created-only chmod, refusal of a non-empty
  unmarked root, root-symlink-followed / leaf-symlink-refused, read-side
  purity, `doctor` vs `doctor --fix`.
- **§2.3 CLOSED → replaced.** The degenerate-override fence has nothing
  left to fence; the surface was deleted. Two defects in the *replacement*
  were found by this gate and are recorded there: `state_root(create=False)`
  skipping validation (so `doctor --fix` created `$HOME/spool`), and the
  two-writer adoption race (§1.6.4).
- **§2.5 OPEN → CLOSED.** The check-then-act `chmod` is gone by
  construction: the `chmod` only runs on the branch where `mkdir`
  succeeded.
- **§2.8 ACCEPTED → CLOSED.** One `_make_ours()` for every getter; the
  policy cannot be inconsistent because there is only one of it.
- **§2.10 expanded** into the full audit-loss policy, naming all three loss
  causes and the guarantee that none of them blocks or undoes a mutation.
- **New:** `doctor` gained a `state_root` check. A refused configuration
  previously produced a *green* doctor while every mutation dropped its
  receipts.
- **New:** the five retired `EMAIL_MCP_*_DIR` variables are **rejected**,
  not ignored — see `docs/reference.md`, "Migrating from the per-directory
  variables". Ignoring them would silently relocate live state.

### v0.11 — findings from the independent Gate 4 audit

The gate ran against the committed tree and the installed wheel and
returned FAIL on the first pass. Three findings, all fixed and each with a
regression test in `tests/test_config_state_dirs.py`:

- **MAJOR — rejection was write-path only.** `retired_state_vars()` was
  consulted only inside `state_root_refusal()`, which only
  `state_root(create=True)` reached. Every `create=False` path skipped it,
  so with `EMAIL_MCP_SPOOL_DIR` set, `list_scheduled` resolved the DEFAULT
  root, found nothing, and answered `{"ok": true, "pending": []}` while the
  operator's queued mail sat in the old directory with nothing delivering
  it — verbatim the "mail that looks lost" outcome the rejection exists to
  prevent. `server.py`, `cli.py` and `dispatcher.py` had no startup gate at
  all. Now: a pure `retired_state_var_error()` consulted by the wire belt
  and by all three entry points; `doctor` is the single, deliberate
  exemption.
- **MAJOR — `create=False` was not total.** `~nosuchuser/foo` raised
  `RuntimeError` out of `expanduser()`, and a relative value with a deleted
  cwd raised `FileNotFoundError` out of `absolute()` — both escaping
  functions documented as "PURE" and "total by design", and both landing in
  uninstall planning, `doctor` and `audit.query`. Resolution now returns an
  unresolvable REASON instead of raising.
- **MINOR — intermediates created world-readable.** `mkdir(parents=True)`
  created every missing directory above the root at the process umask, so
  `EMAIL_MCP_STATE_DIR=/a/b/c/root` left `a/`, `b/` and `c/` at 0755 —
  directories a mail tool made that the user never named. They are not ours
  to chmod, so they are no longer created: a missing parent is refused.

Also uniform now: `graph_dir()` was the one leaf getter without a `create`
parameter, so read-side callers rebuilt its path by hand.
- **Known residual risks after this pass:** §2.1, §2.2, §2.4 (major, open),
  §2.6, §2.7, §2.9, §2.10, §2.11 are unchanged by v0.11 and remain as
  stated.
