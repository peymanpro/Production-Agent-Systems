# Security Policy

Production Agent Systems executes or coordinates tool handlers that may cause external side effects. Treat integrations and operation records as security-sensitive.

## Reporting a vulnerability

Please do not publish exploitable vulnerability details in a public issue.

Use GitHub's private vulnerability reporting for this repository if it is enabled. If private reporting is unavailable, contact the maintainer through GitHub to arrange a private disclosure channel before sharing exploit details.

Include the affected commit or release, the required preconditions, a minimal reproduction, the expected and observed behavior, and the potential impact. Do not include production credentials, customer data, payment details, or other secrets.

## Important trust boundaries

- The library does not authenticate operators who use `agent-effect-ops`. The surrounding environment must restrict who can read or mutate the SQLite ledger and who can invoke manual resolution commands.
- Actor names and evidence references in manual-resolution records are audit context, not independent proof of identity or evidence validity.
- Provider idempotency guarantees and `NOT_COMMITTED` probe results are assertions made by the integration. They must match the external provider's documented semantics.
- A Python thread timeout does not terminate an already-running handler or remote request. The library therefore blocks a retry after timeout when the old handler may still dispatch, unless a documented provider idempotency guarantee makes the overlapping call safe.
- SQLite effect events are ordered, but are not cryptographically chained or externally anchored.
- The application remains responsible for caller authentication, resource-level authorization, credential protection, least-privilege access, and real human approval.

See [Security Boundaries](docs/security-boundaries.md) for the implemented controls and known limits.
