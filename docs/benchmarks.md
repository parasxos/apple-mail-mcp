# Benchmarks

Every number this project quotes is produced by a script in `tools/`, run on a
real mailbox, and reported with the method. Run them on your own Mac; mailbox
sizes and Mail versions differ, and so will your numbers.

## Addressing one message in a large mailbox

Script: [`tools/bench_addressing.py`](../tools/bench_addressing.py)

The operation: fetch one known message (sender, subject, date) out of the
largest mailbox in the store.

| Mechanism | What it does | Result |
|---|---|---|
| Envelope Index (this server) | `SELECT ... WHERE ROWID = ?` against Mail's own SQLite store, read-only | **< 0.1 ms** median (10 runs) |
| AppleScript whose-clause | `first message of mailbox M whose subject is S` via Mail.app | **7.2–9.9 s** (7 runs, three scan depths) |

The whose-clause time was measured at three target depths (10%, 50%, 90% into
the mailbox) and is essentially depth-independent on this Mail version —
about 9.3 s median wherever the message sits.

Measured 2026-08-26 on Apple Silicon (arm64), macOS 26.5.2, against a live
store of 298,980 messages; the haystack mailbox held 71,344 messages. The
whose-clause is the *primitive* that AppleScript-based tooling builds on; how
any particular tool composes it varies, and a cold Mail.app or a bigger
mailbox pushes the AppleScript side well past this.

Earlier in this project's history the same operation measured 85.6 s on a
comparable mailbox; Mail's scripting layer appears faster today. We quote the
current reproducible number, not the historical one.

## Caveats, stated plainly

- The index side reads Mail's store directly and requires Full Disk Access.
- AppleScript timings vary with Mail.app state (cold vs warm); the reported
  range covers 7 runs across two sessions.
- Search-speed claims ("milliseconds over hundreds of thousands of messages")
  are FTS5 queries against the local body index; build time for that index on
  first setup is minutes and is reported by `status`.
