# Effect Contracts and Recovery

This document defines the contract expected by the standalone effect executor and by tools configured to use it through AgentRuntime.

## Define one logical operation

An operation ID represents one business intent, not one attempt. Derive it from trusted business identifiers and keep it stable across retries and recovery.

```python
contract = EffectContract(
    name="capture_payment",
    operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
    provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
    provider_idempotency_guaranteed=True,
    outcome_probe=lookup_payment,
)
```

The executor computes a canonical JSON fingerprint for the request. Reusing an operation ID with a different contract name, request fingerprint, or provider idempotency key raises `OperationConflictError` before another handler dispatch. The fingerprint is an identity-conflict guard; it is not an authorization decision.

## Handler and probe contracts

The handler has the following shape:

```python
def handler(arguments: Mapping[str, object], *, idempotency_key: str | None) -> object:
    ...
```

The handler should forward the idempotency key when the provider documents support for it. If the provider cannot guarantee idempotency, leave `provider_idempotency_guaranteed=False`.

An outcome probe returns an `OutcomeProbeResult` with one of four states:

- `COMMITTED`: authoritative evidence that the effect happened, with a replayable JSON result when available.
- `NOT_COMMITTED`: authoritative terminal proof that the previous request cannot commit later. This enables a retry.
- `PENDING`: the operation is still in progress; do not dispatch another write.
- `UNKNOWN`: current evidence cannot prove either success or non-execution.

A missing record, a temporary 404, or an eventually consistent read is not automatically a terminal `NOT_COMMITTED` result.

## Postconditions and rejections

A postcondition receives the request mapping and the handler/probe output. It must return true only when the output establishes the result the application requires. If the check fails, the effect is not treated as confirmed.

Raise `EffectRejected` only when the provider definitively rejects the operation without applying any external effect. Do not use it for generic exceptions, timeouts, broken connections, or server errors with an unknown commit state.

## Status meanings

| Status | Meaning |
| --- | --- |
| `PREPARED` | The operation is recorded and eligible to be claimed for dispatch. |
| `DISPATCHED` | One worker owns a time-limited dispatch claim; a concurrent caller may receive `in_progress=True`. |
| `CONFIRMED` | A handler result or probe has passed the contract's confirmation checks and was persisted. |
| `OUTCOME_UNKNOWN` | The result is not established yet. |
| `QUARANTINED` | Automatic dispatch is stopped because the result cannot safely be established. |
| `REJECTED` | The handler reported a definitive no-effect rejection. |

A dispatch lease is coordination metadata, not proof that a remote request was cancelled. After lease expiry, the executor must reconcile the provider state or use a documented provider idempotency guarantee.

## AgentRuntime integration

Add an `effect_contract` to a `ToolDefinition` and pass an `EffectExecutor` to `AgentRuntime`:

```python
tool = ToolDefinition(
    name="capture_payment",
    description="Capture an invoice payment",
    input_schema={
        "required": ["invoice_id"],
        "properties": {"invoice_id": {"type": "string"}},
    },
    effect=ToolEffect.MUTATION,
    version="1",
    handler=capture_payment,
    effect_contract=contract,
)

runtime = AgentRuntime(
    planner=planner,
    registry=ToolRegistry([tool]),
    policy=policy,
    state_store=InMemoryStateStore(),
    checkpoint_store=CheckpointStore(),
    event_sink=InMemoryEventSink(),
    effect_executor=EffectExecutor(
        ledger=SQLiteEffectLedger("agent-effects.sqlite3")
    ),
)
```

Effect-backed tools require a configured executor. Their handler timeout is routed through the effect executor so an expired Python wait does not get mistaken for proof that the remote operation stopped. A running Python thread cannot be forcibly terminated; the contract must still preserve the ambiguity and rely on provider evidence where needed. Configure provider-native network timeouts as well.

When an effect result is `OUTCOME_UNKNOWN`, `QUARANTINED`, `DISPATCHED`, or `PREPARED`, AgentRuntime transitions the run to `WAITING`. Resuming the same task replays the same logical action through the ledger, allowing the provider result to be reconciled before the planner continues.


## Inspect and manually resolve an uncertain operation

List recent operations or filter for quarantined records:

```python
ledger = SQLiteEffectLedger("agent-effects.sqlite3")
quarantined = ledger.list_operations(status=EffectStatus.QUARANTINED)
for operation in quarantined:
    print(operation.operation_id, operation.status, operation.error)
    print(ledger.events(operation.operation_id))
```

When an authoritative provider lookup is unavailable, a trusted operator may resolve a quarantined record after independently establishing its outcome:

```python
# Use only after checking the authoritative provider record.
executor.resolve_manually_confirmed(
    operation_id,
    output={"payment_id": "pay-123", "status": "captured"},
    provider_operation_id="pay-123",
    actor="on-call-operator",
    reason="Verified the capture in the provider dashboard",
    evidence_reference="INC-1042/provider-event-1",
)
```

If the operator instead establishes that the previous request is terminal and definitely did not take effect, call `authorize_manual_retry(...)` with the actor, reason, and evidence reference. `resolve_manually_rejected(...)` records an evidenced terminal rejection. All three APIs accept only operations in `OUTCOME_UNKNOWN` or `QUARANTINED`; they do not override a confirmed or actively dispatched operation.

These APIs record an audit trail; they do not authenticate the operator or validate the contents of an evidence reference. The integrating application must authenticate and authorize the operator, verify evidence independently, and ensure a retry cannot race with an earlier request that is still capable of committing.

The `agent-effect-ops` command exposes the same operations for a local SQLite ledger:

~~~bash
agent-effect-ops --db agent-effects.sqlite3 list --status quarantined
agent-effect-ops --db agent-effects.sqlite3 events "invoice:INV-1042:capture:v1"
agent-effect-ops --db agent-effects.sqlite3 confirm "invoice:INV-1042:capture:v1" \
  --output-json '{"payment_id":"pay-123","status":"captured"}' \
  --actor on-call-operator \
  --reason "Verified provider capture record" \
  --evidence-reference INC-1042/provider-event-123
~~~

The list command omits request payloads and stored outputs by default. The events command may contain operational evidence; restrict access to the ledger and terminal output accordingly. The CLI is intended for trusted local operations, not as a remotely exposed administration API.

## Guarantees and boundaries

The library can atomically coordinate operation records in its SQLite ledger and prevent concurrent claims while a dispatch lease is active. It cannot atomically commit a remote payment and a local SQLite row. In particular, universal exactly-once external effects cannot be promised without a cooperating destination or authoritative evidence that lets the executor safely resolve an uncertain attempt.

The integrating application remains responsible for caller authentication, resource-level authorization, secret handling, real human approvals, business-operation identity, and honest provider contracts.
