# Graph deferred-send probe — the v0.8 M2 gate (G0)

*Operational note, 2026-07-29. Companion to `docs/v0.8-concept.md`
(movement 2) and `docs/transport-design.md`. The probe itself is
`tools/graph_probe.py` — self-contained stdlib Python, zero imports from
`email_mcp`, runnable on any machine.*

## What it proves

v0.8's "durable schedules" movement hands a frozen message to Exchange as a
draft carrying **PidTagDeferredSendTime** (Graph
`singleValueExtendedProperties`, id `SystemTime 0x3FEF`), so Exchange — not
launchd on a possibly-sleeping Mac — transmits it at the deferred time. That
whole design stands on three tenant-controlled capabilities that no document
can confirm, only a live call can:

1. **Delegated device-code sign-in works** for a mailbox on this tenant with
   `Mail.ReadWrite` + `Mail.Send` + `offline_access` (offline_access is what
   later gives `email_mcp/graph.py` a refresh token for silent reconcile).
2. **The MIME-create path is open**: `POST /v1.0/me/messages` with
   `Content-Type: text/plain` and a base64-encoded RFC-822 body creates a
   draft from *our* frozen `.eml` bytes — the executor never re-composes.
3. **The extended property round-trips**: PATCHing `SystemTime 0x3FEF` onto
   the draft succeeds and a `GET … $expand=singleValueExtendedProperties`
   reads the same timestamp back. Tenants can silently strip or reject
   extended MAPI properties; the round-trip is the proof they didn't.

Phase A (the default) proves exactly those three and **deletes its draft** —
nothing is ever sent, the mailbox ends the run untouched. Phase A therefore
reports `deferred_send_honored: "untested"`: a stored property is necessary
but not sufficient; only a real send proves Exchange *acts* on it.

Phase B (`--live`) repeats the sequence with the deferred time ~3 minutes
out, then `POST …/send`. The final verdict field is deliberately left to a
human: the message should land in your own inbox ~3 minutes later **even if
you close the laptop lid the moment the probe prints its instructions** —
that lid-closed arrival is the entire point of the executor.

## Running it against CERN

```bash
python3 tools/graph_probe.py --tenant cern.ch --client-id <APP_ID>
# Phase B, once Phase A is green:
python3 tools/graph_probe.py --tenant cern.ch --client-id <APP_ID> --live
```

Any Python 3.10+ works; no venv or install needed (stdlib only).

**Tenant.** `cern.ch` resolves as the tenant domain. If it doesn't, fetch the
tenant GUID from
`https://login.microsoftonline.com/cern.ch/v2.0/.well-known/openid-configuration`
(the GUID appears in the `issuer` URL) and pass that instead. The default
`organizations` also works for a first try — Microsoft routes you to the
right tenant after you type your CERN address.

**Client id.** There is deliberately **no baked-in GUID**. The flow needs a
*public client* app registration that the tenant allows for device-code
sign-in:

- *Clean path:* register your own app (portal.azure.com → App registrations:
  single-tenant, delegated `Mail.ReadWrite` + `Mail.Send`, "Allow public
  client flows" = **Yes**) — if CERN lets ordinary users register apps.
- *Managed-tenant path:* ask the tenant admins (Service Desk → M365 team)
  which application id is approved for device-code use. CERN may require an
  admin-consented, approved id — an AADSTS650052/65001-style consent error
  in the verdict means exactly that.
- *The Azure-CLI trick (last resort):* Microsoft's own first-party client ids
  are pre-consented on many tenants, e.g. Azure CLI
  `04b07795-8ddb-461a-bbee-02f9e1bf7b46`. It often works — and it carries
  real risks: using a first-party id outside its product looks like token
  abuse to a security team, Conditional Access can block it selectively, and
  a refusal proves nothing about the capability itself (only that *that id*
  is blocked). If you use it, say so when reading the verdict.

**CERN SSO caveats.** The device-code page (`microsoft.com/devicelogin`)
hands off to CERN's federated sign-in, so expect the CERN SSO screen plus
2FA in the browser — the probe just polls until that completes. Conditional
Access policies commonly disallow the device-code grant class entirely
(it's a known phishing vector); if so the verdict carries the AADSTS error
verbatim — that is a *policy* NO, worth one round with the admins before
accepting it as final. The probe also assumes the mailbox lives in Exchange
Online; if the account's mail is still on a legacy/on-prem service,
`/me/messages` fails and the verdict's `reason` will show a Graph
`MailboxNotEnabledForRESTAPI`-style error — also a NO for this design.

## Reading the verdict

The probe prints one JSON object (pretty by default, compact with `--json`)
and exits 0 for YES, 1 for NO:

```json
{
  "probe": "YES",
  "tenant": "cern.ch",
  "client_id": "…",
  "scopes_granted": "Mail.ReadWrite Mail.Send",
  "mime_create": true,
  "extended_prop_roundtrip": true,
  "deferred_send_honored": "untested",
  "reason": ""
}
```

- `probe` — YES iff `mime_create` **and** `extended_prop_roundtrip` are true.
- `scopes_granted` — what the token actually carries; if the tenant trimmed
  a requested scope, it shows here.
- `deferred_send_honored` — `"untested"` in Phase A; in Phase B it becomes
  `"awaiting human confirmation"` and *you* close the loop: lid shut,
  message arrives ~3 minutes later → fully green. (A Phase-B message that
  arrives *immediately* means the property was stored but ignored — treat
  as NO for the executor even though `probe` says YES.)
- `reason` — empty on success; otherwise the exact failure, with AADSTS
  codes and Graph error codes passed through verbatim so an admin ticket
  can quote them.
- A failed Phase-A run tries to delete its draft anyway; if cleanup fails
  you'll see a stderr warning — remove the probe draft from Drafts by hand.

## What YES / NO means for v0.8 (stage S7)

- **YES** → S7 builds the Graph executor per the plan's M2 architecture:
  `email_mcp/graph.py` (device login + token cache +
  `create_deferred_draft`), `executor = "graph"` as an *identity*
  capability, schedule/cancel/dispatcher reconcile paths, launchd as
  fallback. The probe's tenant + client id go straight into
  `identities.toml` under `[cern.graph]`.
- **NO** → S7 becomes S7′, a documentation closure: no Graph code lands,
  `docs/transport-design.md` and the reference gain a dated "not possible on
  this tenant, and here is the verdict that proves it" note, and the launchd
  spool remains the sole executor — schedules stay honest about requiring
  the Mac awake. A NO costs v0.8 nothing else; every other movement is
  independent of this gate.

Keep the verdict JSON either way — it is the evidence the S7/S7′ decision
cites.
