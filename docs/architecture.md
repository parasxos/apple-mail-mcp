# Architecture

email-mcp uses a ports-and-adapters architecture. The MCP SDK, Mail.app,
Exchange, SMTP, launchd, the filesystem, and the audit ledger are replaceable
edges around one provider-neutral application core.

```text
MCP client / command line
          │
          ▼
  inbound adapters
  server.py + mcp_api.py
          │
          ▼
  application use cases ──────► domain events
  reads · delivery · scheduling · triage · operations · background
          │                    domain errors
          │ ports
          ▼
  outbound adapters
  source · delivery · triage · scheduling · refresh · operations
  background delivery · audit
          │
          ▼
Mail.app · local store · SMTP/SSH · Microsoft Graph · launchd · disk
```

## Dependency rule

Dependencies point inward:

1. `email_mcp.domain` contains provider-neutral records, errors, event types,
   error codes, and the mailbox-source protocol. It imports no application,
   MCP, macOS, network, process, database, or filesystem implementation.
2. `email_mcp.application` contains the 21 public tool use cases, the
   scheduled-delivery worker, and their outbound port protocols. It imports
   only the domain and Python's standard library.
3. `email_mcp.adapters` implements those ports using the existing Apple Mail,
   transport, Graph, queue, triage, diagnostics, and audit components.
4. `email_mcp.bootstrap` is the composition root. It is the only place that
   constructs the production application from all concrete adapters.
5. `email_mcp.mcp_api` and `email_mcp.server` are inbound adapters. They
   translate MCP or command-line calls into application use cases and retain
   the frozen v1 wire envelope.

These rules are executable. `tests/test_architecture.py` parses imports,
starts the application without loading a concrete integration, and exercises
read, send, and background-delivery workflows using in-memory ports. It also
prevents outbound adapters from importing an inbound adapter, keeps the MCP
boundary free of concrete integrations, checks every production adapter
against its port, and stops compatibility facades from becoming monoliths
again. `tests/test_adapter_contracts.py` verifies lazy-provider concurrency
and normalised identity, Graph, and transport failures. A future accidental
dependency from a use case into AppleScript, Graph, a queue file, or MCP fails
CI.

## Application capabilities

`EmailApplication` is a stable facade, not a large implementation. Its
capabilities are split by reason to change:

| Module | Owns |
|---|---|
| `application/reads.py` | search, individual and batch reads, threads, mailboxes, attachments, refresh |
| `application/delivery.py` | send, draft, reply, and scheduling requests |
| `application/scheduling.py` | scheduled-mail listing and cancellation |
| `application/triage.py` | plan, apply, and mailbox-management workflows |
| `application/operations.py` | health, transport, and audit queries |
| `application/background.py` | retry, crash recovery, queue dispatch, and safe Exchange/local hand-off |
| `application/query.py` | shared parsing and result-shaping policy |
| `application/ports.py` | the interfaces implemented by every outer adapter |

Large integrations are divided along the same responsibility lines while
their historical imports remain small compatibility facades:

- sending separates address policy, attachment policy, MIME construction,
  and transport execution;
- Graph separates authenticated HTTP from mailbox/draft operations;
- triage separates immutable plan construction from plan execution;
- full-text indexing separates index/database work from launchd and CLI
  presentation;
- the dispatcher facade contains only launchd/CLI concerns and delegates its
  safety-critical workflow to `BackgroundUseCases`.

## Runtime flow

For a send, for example:

1. The MCP adapter accepts and validates the public tool arguments.
2. `EmailApplication.send_email` calls the `DeliveryGateway` port.
3. The production delivery adapter selects the configured identity and
   transport. A test or another front end can supply an in-memory gateway.
4. The use case publishes a provider-neutral `DomainEvent`.
5. The audit adapter records that event without the use case importing or
   depending on the ledger.
6. The wire boundary converts the typed result or typed error to the stable
   MCP envelope.

The same direction applies to search, drafts, replies, scheduled mail,
cancellation, triage, mailbox management, refresh, diagnostics, and audit.
The launchd process follows it too: `dispatcher.py` invokes
`EmailApplication.dispatch_scheduled`, which owns retry, recovery, and the
double-send fence through a `BackgroundGateway`; the production adapter alone
knows spool files, identities, transports, Graph, and macOS notifications.

## Stable compatibility surface

This refactor changes internal ownership, not the public product:

- all 21 tool names and the 11-tool read-only surface remain unchanged;
- input and output JSON Schemas remain frozen;
- legacy JSON text and MCP 2 structured results remain identical;
- error codes and envelope behavior remain unchanged;
- established imports from `email_mcp.server`, `email_mcp.sources.base`,
  `email_mcp.sender`, `email_mcp.plans`, and `email_mcp.spool` are re-exported
  during the migration.

## Adding an integration

A new provider or delivery mechanism should not add conditionals to a use
case. Implement the relevant protocol in `application/ports.py`, place the
implementation under `adapters/`, and select it in `bootstrap.py`. Add it to
the adapter contract matrix, test provider-specific behaviour at the edge,
and test the use case with a fake port.

For a new mailbox provider, implement `domain.mail.EmailSource`. Existing
Apple Mail source imports through `sources.base` remain valid because that
module is now a compatibility export of the domain contract.

## Why this structure matters

- Provider failures cannot leak implementation details into core workflows.
- Business rules are testable on any operating system without Mail.app,
  credentials, subprocesses, or a real mailbox.
- MCP SDK upgrades affect the inbound adapter, not email behavior.
- New providers, transports, storage, or audit sinks can be added behind a
  port without rewriting every tool.
- One composition root makes production wiring explicit and reviewable.
- Interactive MCP calls and scheduled background work obey the same inward
  dependency direction and publish through the same event port.
