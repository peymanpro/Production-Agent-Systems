# Architecture

Production Agent Systems separates the agent's proposed action from execution authority and from evidence about external side effects.

~~~text
                  +----------------------+
                  | Planner / Model Port |
                  +-----------+----------+
                              |
                              v
                  +-----------+----------+
                  |     AgentRuntime     |
                  +----+-------------+---+
                       |             |
                       v             v
              Permission Policy   Tool Registry
                                     |
                                     v
                              Input Validation
                                     |
                         +-----------+-----------+
                         |                       |
                         v                       v
                 Ordinary Tool             Effect-backed Tool
                 Retry / Timeout           EffectContract
                 Circuit Breaker                 |
                                                 v
                                        +--------+--------+
                                        | EffectExecutor  |
                                        +--------+--------+
                                                 |
                              +------------------+------------------+
                              |                                     |
                              v                                     v
                       SQLite Effect Ledger                 External Provider
                       State + Evidence                    Business Side Effect
                              |
                              v
                      Reconcile / Replay
                              |
                              v
                      Runtime WAITING or continue
~~~

The planner proposes an action. The runtime checks permissions and validates the input. If the tool declares an effect contract, the effect executor claims a stable operation ID in the SQLite ledger before dispatch and decides what can safely happen next from the returned result or provider evidence.

## Effect reliability boundary

An operation ID identifies one business operation, not one attempt. The request fingerprint prevents the same ID from being reused with different arguments, and the contract version prevents a recorded result from being reused under changed execution semantics.

The local ledger and an external provider do not share a transaction. If a provider commits but the acknowledgement is lost, the ledger must represent uncertainty until the provider contract or trusted reconciliation establishes an outcome. A lease expiration only means the local claim expired; it does not prove that a remote request stopped. A timed-out Python thread may still be capable of sending the request, so a negative provider probe alone cannot authorize an overlapping retry when provider idempotency is unavailable.

A repeated external call is eligible only when one of these conditions establishes safety:

- an authoritative provider probe establishes terminal non-execution;
- the provider explicitly guarantees idempotency for the same stable key and request;
- a trusted operator resolves the uncertain record with documented evidence.

When none applies, the operation remains unknown or quarantined rather than being retried blindly.

## Persistence boundaries

| Concern | Current implementation | Boundary |
| --- | --- | --- |
| Agent state and checkpoint | In-memory reference stores | Not durable across process restart |
| Generic runtime trace/events | In-memory sinks | Not a durable production telemetry backend |
| Generic audit sink | In-memory sink | Not a complete tamper-evident audit archive |
| Effect records and effect events | File-backed SQLite | Durable on a single host; no built-in multi-host coordination |
| Remote provider effect | Provider-owned | Guarantees depend on documented idempotency and outcome-query semantics |

## Recovery sequence

1. Derive the stable operation ID and canonical request fingerprint.
2. Persist the operation as `PREPARED`.
3. Atomically claim the dispatch lease and record `DISPATCHED`.
4. Invoke the handler using the same provider idempotency key for this operation.
5. Persist the confirmed result, or mark the operation as unknown if the handler times out or fails ambiguously.
6. Probe the provider when configured. A probe returns `COMMITTED`, terminal `NOT_COMMITTED`, `PENDING`, or `UNKNOWN`.
7. Return a stored result, permit a contract-safe retry, keep the result unknown, or quarantine it for trusted resolution.

## Current limitations

- The effect executor is integrated into `ToolDefinition` and `AgentRuntime`, but only tools configured with an `EffectContract` use the effect ledger.
- SQLite is a local/single-host backend; there is no PostgreSQL ledger or cross-host ownership guarantee yet.
- Python thread cancellation cannot terminate a running handler or reverse an external side effect.
- The local operator CLI is not an authenticated remote administration service.
- The package does not provide a hosted model provider, a general workflow engine, or universal exactly-once external effects.
