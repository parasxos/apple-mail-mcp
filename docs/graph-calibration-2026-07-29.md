# Graph deferred-send executor — live calibration evidence (2026-07-29)

*The v0.9.0 release gate: every claim below was produced against the live
CERN tenant with the production code on branch `v0.9-graph` (post red-team,
post audit-PASS). Companion docs: `graph-probe.md`, `v0.8-concept.md` (M2).*

## 1. Probe (G0 gate) — YES

- Tenant `cern.ch` (GUID `c80d3499-4a40-4a8c-986e-abce017d6b19`).
- Client id: **Azure CLI (`04b07795-…`) REFUSED** — `AADSTS65002`
  (Microsoft's own first-party preauthorization hardening, not CERN policy).
- Client id: **Microsoft Graph Command Line Tools
  (`14d82eec-204b-4c2f-b7e8-296a70dab67e`) ACCEPTED** — device-code flow
  through CERN SSO + 2FA; delegated `Mail.ReadWrite` + `Mail.Send` granted
  (tenant adds pre-consented extras).
- Phase A verdict JSON: `mime_create: true`,
  `extended_prop_roundtrip: true` — the tenant creates drafts from raw MIME
  and does NOT strip `SystemTime 0x3FEF` (PidTagDeferredSendTime).

## 2. Environment fix found by calibration

First `--login` failed: `SSL: CERTIFICATE_VERIFY_FAILED` — the venv's
python.org framework build ships no root-CA bundle. Fixed in
`graph._ssl_context()`: prefer certifi's bundle when importable, fall back
to system defaults; cached; regression test
`test_ssl_context_prefers_certifi_and_caches`. Commit `5ddcec0`.

## 3. Production login

`python -m email_mcp.graph --login cern` → token cache
`~/.email-mcp/graph/cern.token.json` (0600, dir 0700),
`has_refresh_token: true` — silent launchd reconcile enabled.

## 4. Cancellation + revocation — PASS

Scheduled `20260729T190033Z-fc52f8` ("should NEVER arrive") for T+30 min via
the graph executor:

| Step | Result |
|---|---|
| schedule | `executor: graph`, draft id captured (two-phase manifest) |
| pre-cancel `draft_status` | `held` — Exchange holding it |
| `cancel_scheduled` | `{ok: true, status: cancelled}` |
| post-cancel `draft_status` | `cancelled_externally` — draft destroyed server-side, NOT sent |
| local spool | entry in `cancelled/` |

## 5. Lid-closed delivery — the point of the whole design

Scheduled `20260729T185919Z-914c3f` at 18:59:19Z for **19:02:19Z**, Mac lid
closed before the send time; Exchange transmitted server-side.

| Step | Timestamp (UTC) | Evidence |
|---|---|---|
| scheduled | 18:59:19 | entry `20260729T185919Z-914c3f`, `executor: graph`, draft id captured |
| lid closed | ~19:00 | Mac asleep through the send window |
| **Exchange transmitted** | **19:02:19 — the deferred time to the second** | Inbox copy `Date: 2026-07-29T19:02:19+00:00`; native **Sent Items copy** created by Exchange (the draft path populates Sent — no Bcc-to-self needed on this executor) |
| lid reopened | ~19:09 | — |
| dispatcher reconcile | 19:12:47 (after the designed 10-min grace) | `draft_status → sent` on positive Sent-Items evidence; entry moved to `sent/`, `executor: graph`, `last_error: clean` |

**Verdict: `deferred_send_honored: YES`.** The mail was delivered by
Exchange while the Mac was closed, and the local ledger reconciled itself on
wake without intervention.

## 6. Release

v0.9.0 = branch `v0.9-graph` (audit verdict PASS, red-team 9 findings fixed,
254→255 tests) + this calibration. The executor is opt-in per identity
(`executor = "graph"`); every other identity and every pre-existing manifest
behaves exactly as v0.8.0 (F13, test-enforced).
