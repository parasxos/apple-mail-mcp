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
  application/service.py       domain records
          │                    domain errors
          │ ports
          ▼
  outbound adapters
  source · delivery · triage · scheduling · refresh · operations · audit
          │
          ▼
Mail.app · local store · SMTP/SSH · Microsoft Graph · launchd · disk
```

## Dependency rule

Dependencies point inward:

1. `email_mcp.domain` contains provider-neutral records, errors, event types,
   error codes, and the mailbox-source protocol. It imports no application,
   MCP, macOS, network, process, database, or filesystem implementation.
2. `email_mcp.application` contains the 21 use cases and their outbound port
   protocols. It imports only the domain and Python's standard library.
3. `email_mcp.adapters` implements those ports using the existing Apple Mail,
   transport, Graph, queue, triage, diagnostics, and audit components.
4. `email_mcp.bootstrap` is the composition root. It is the only place that
   constructs the production application from all concrete adapters.
5. `email_mcp.mcp_api` and `email_mcp.server` are inbound adapters. They
   translate MCP or command-line calls into application use cases and retain
   the frozen v1 wire envelope.

These rules are executable. `tests/test_architecture.py` parses imports,
starts the application without loading a concrete integration, and exercises
read and send workflows using in-memory ports. A future accidental dependency
from a use case into AppleScript, Graph, a queue file, or MCP fails CI.

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
implementation under `adapters/`, and select it in `bootstrap.py`. Test the
adapter against its external system and test the use case with a fake port.

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
