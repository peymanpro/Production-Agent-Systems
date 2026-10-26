# Changelog

Notable changes to Production Agent Systems are documented here.

## Unreleased

### Added

- Explicit, versioned effect contracts with stable operation IDs and canonical request fingerprints.
- File-backed SQLite effect ledger with transactional dispatch claims, operation results, and ordered effect events.
- Outcome-aware recovery for confirmed, rejected, pending, unknown, and quarantined operations.
- Provider idempotency and outcome-probe contracts to prevent unsafe retries after ambiguous failures.
- Effect-aware integration for configured `ToolDefinition` instances used by `AgentRuntime`.
- Deterministic fake payment provider with fault injection for pre-commit failure and commit followed by lost acknowledgement.
- `agent-effect` testkit command and `agent-effect-ops` CLI for inspecting operations and recording evidence-backed manual resolutions.
- Contract-version migration for existing SQLite ledgers, effect-specific documentation, and regression tests for concurrent claims and recovery.
- Subprocess crash-recovery regression test proving a committed external effect is reconciled after the worker exits before persisting the local result.

### Security and reliability notes

- Unknown outcomes are quarantined by default when neither authoritative provider evidence nor a declared provider idempotency guarantee makes retry safe.
- The SQLite ledger is a single-host backend; PostgreSQL coordination and framework adapters are not included.
- The library does not claim universal exactly-once external effects and does not replace application authentication, resource authorization, or provider-side guarantees.
