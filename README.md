# Production Agent Systems

Production Agent Systems is a provider-neutral Python library for making agent tool effects safer to execute, recover, and test under failure. It records durable operation state, uses explicit provider contracts to reconcile uncertain outcomes, and includes a deterministic fault-injection testkit for verifying external-effect behavior.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/peymanpro/Production-Agent-Systems/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/peymanpro/Production-Agent-Systems/actions/workflows/quality.yml)
[![Latest release](https://img.shields.io/github/v/release/peymanpro/Production-Agent-Systems)](https://github.com/peymanpro/Production-Agent-Systems/releases)

> **The failure this library targets:** an agent sends a write request, the destination commits it, and the acknowledgement is lost. The agent sees a timeout—not proof of failure. Retrying blindly may charge a customer twice; giving up may leave required work unfinished.

The library focuses on the boundary between **what an agent believes happened** and **what the external system actually committed**. It is complementary to an agent framework or workflow engine, not a replacement for either.

## Table of contents

- [Why this library exists](#why-this-library-exists)
- [Current capabilities](#current-capabilities)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Effect contracts and recovery](#effect-contracts-and-recovery)
- [Fault-injection testkit](#fault-injection-testkit)
- [Persistence and concurrency](#persistence-and-concurrency)
- [Security model and limits](#security-model-and-limits)
- [Research behind the design](#research-behind-the-design)
- [Development](#development)
- [Current scope and next steps](#current-scope-and-next-steps)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [License](#license)

## Why this library exists

Agent frameworks can retry tool calls, persist execution state, and resume interrupted runs. Those capabilities do not, by themselves, establish whether an external write took effect.

Consider a payment operation:

1. An agent submits a capture request.
2. The payment provider records the capture.
3. The acknowledgement is lost in transit.
4. The agent receives a timeout and retries or resumes.
5. Without a stable idempotency contract or reliable reconciliation, a second capture may occur—or the application may incorrectly report failure.

The same failure pattern appears in email delivery, ticket creation, resource provisioning, deployment, inventory updates, and other operations with external side effects.

Production Agent Systems records an operation before dispatch, uses a stable business identity, tracks whether the result is confirmed or uncertain, and only retries an uncertain write when the provider contract supplies a safe basis for doing so. It never treats a timeout or an LLM's final answer as proof of external success.

A local handler deadline is not cancellation: the Python worker thread can continue and send a request after the caller has timed out. In that case, even a provider lookup that currently reports no commit cannot, by itself, prove that retry is safe. The ledger records this possibility and blocks retry unless the provider's stable-key idempotency guarantee makes overlapping dispatch safe or a trusted operator establishes that the previous handler can no longer send the request.

## Current capabilities

The current core implementation includes:

- **Explicit effect contracts** — define a stable operation ID, provider idempotency key, optional outcome probe, postcondition, and unknown-outcome policy.
- **Stable operation identity, contract versions, and request fingerprints** — reject reuse of one operation ID for different request arguments, provider keys, or contract versions. Existing SQLite ledgers are migrated to contract version `1`.
- **Durable SQLite effect ledger** — persist operation state, attempt count, provider result, evidence, and ordered state-change events.
- **Outcome-aware recovery** — distinguish confirmed success, definitive rejection, active dispatch, unknown outcome, and quarantined operation.
- **Provider-aware retries** — retry after terminal NOT_COMMITTED evidence only when no earlier local handler can still dispatch; an overlapping retry requires an explicitly declared provider idempotency guarantee.
- **Conservative ambiguity handling** — quarantine uncertain writes by default when neither a reliable outcome probe nor a provider idempotency guarantee can make retry safe.
- **Postcondition validation** — require a caller-defined condition before treating a returned or probed result as confirmed.
- **Operation conflict detection** — detect request-fingerprint mismatches before another handler dispatch.
- **Operator inspection and audited resolution** — list operations by status, record evidence-backed manual confirmation or rejection, and explicitly authorize a retry after trusted verification.
- **Fault-injection demo** — reproduce failure before commit and commit followed by a lost acknowledgement, then report the number of committed fake effects.
- **No model-provider dependency** — the core tests and demo run without API keys or hosted model calls.

The effect core integrates with the existing agent runtime: attach an effect contract to a ToolDefinition and supply an EffectExecutor to AgentRuntime. An uncertain or quarantined effect transitions the run to WAITING, so the same logical action can be reconciled on resume rather than being reported as a definitive failure. The existing permission policy still runs before the tool handler.

## Installation

Install from a source checkout:

~~~bash
python -m pip install -e ".[dev]"
~~~

The effect ledger uses the Python standard library and SQLite; no external database or model provider is required for the core example.

## Quick start

The handler receives the stable provider idempotency key. The outcome probe uses the same key to ask the destination what happened.

~~~python
from production_agent_systems import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    OutcomeProbeResult,
    ProbeStatus,
    SQLiteEffectLedger,
)

# Implement these using your provider's documented API.
def capture_payment(arguments, *, idempotency_key):
    return payment_provider.capture(
        invoice_id=arguments["invoice_id"],
        idempotency_key=idempotency_key,
    )


def find_payment_outcome(arguments, *, idempotency_key):
    payment = payment_provider.lookup_by_idempotency_key(idempotency_key)
    if payment is not None:
        return OutcomeProbeResult(
            status=ProbeStatus.COMMITTED,
            output=payment,
            provider_operation_id=payment["id"],
            evidence={"source": "provider_lookup"},
        )

    # Return NOT_COMMITTED only if the provider can prove the earlier request
    # is terminal and cannot commit later. An eventually consistent empty read
    # must instead return PENDING or UNKNOWN.
    return OutcomeProbeResult(
        status=ProbeStatus.UNKNOWN,
        evidence={"source": "provider_lookup"},
    )


contract = EffectContract(
    name="capture_payment",
    operation_id=lambda args: f"invoice:{args['invoice_id']}:capture",
    provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
    provider_idempotency_guaranteed=True,
    outcome_probe=find_payment_outcome,
)

executor = EffectExecutor(ledger=SQLiteEffectLedger("agent-effects.sqlite3"))
result = executor.execute(
    contract=contract,
    arguments={"invoice_id": "INV-1042"},
    handler=capture_payment,
)

if result.status is EffectStatus.CONFIRMED:
    print("Payment capture is confirmed.")
elif result.status is EffectStatus.OUTCOME_UNKNOWN:
    print("The operation still needs reconciliation.")
elif result.status is EffectStatus.QUARANTINED:
    print("Automatic execution is blocked until the outcome is resolved.")
~~~

The example's provider functions are integration points, not a bundled payment SDK. The exact behavior depends on the external provider's documented idempotency and query semantics.

### Two identities matter

- **Operation ID** identifies the logical business operation across retries and recovery.
- **Provider idempotency key** is forwarded to a destination that documents support for idempotency.

Do not generate a new key on every retry. Do not reuse one operation ID for different business actions. The request fingerprint also prevents accidental reuse of an operation ID with changed arguments.

## Effect contracts and recovery

Every operation is tracked by an explicit state. A handler timeout does not mean the external effect failed.

~~~text
PREPARED
   |
   v
DISPATCHED --------------------+
   |                           |
   | handler returns           | timeout, crash, lost response
   v                           v
CONFIRMED                OUTCOME_UNKNOWN
                               |
                               v
                          RECONCILING
                         /     |      \
                        v      v       v
                 CONFIRMED  PREPARED  QUARANTINED
                            (retry)    or UNKNOWN
~~~

### Contract fields

| Field | Purpose |
| --- | --- |
| **name** | Stable name for the contract. Changing its semantics should use a new name or versioned name. |
| **operation_id** | Function mapping business arguments to a stable operation ID. |
| **provider_idempotency_key** | Optional function deriving the same destination key for every attempt. |
| **provider_idempotency_guaranteed** | Explicit assertion that the provider's documented key semantics make repeated sends safe. Defaults to false. |
| **outcome_probe** | Optional function returning COMMITTED, NOT_COMMITTED, PENDING, or UNKNOWN. |
| **postcondition** | Optional predicate that must hold for a result to be considered confirmed. |
| **unknown_outcome** | QUARANTINE by default, or KEEP_UNKNOWN when the caller manages quarantine elsewhere. |

A probe should return:

- **COMMITTED** only with evidence that the effect committed.
- **NOT_COMMITTED** only with authoritative terminal evidence that the earlier request cannot commit later.
- **PENDING** when the provider says the operation is still processing or the result is not final.
- **UNKNOWN** when available evidence cannot settle the outcome.

An empty lookup is not necessarily proof of non-execution. A provider may process a request asynchronously or expose an eventually consistent read path. If the probe cannot establish the outcome, the executor preserves uncertainty by default. It may dispatch a retry with the same provider key only when the caller explicitly asserts the provider's documented idempotency guarantee; that assertion must be grounded in the provider's actual contract, not merely in the presence of a key parameter.

### Definitive rejections

A handler can raise **EffectRejected** only when it has reliable evidence that the provider rejected the operation without applying the effect. Do not raise it for timeouts, connection resets, or generic server errors where the commit state may be unknown.

### What happens on recovery?

- **Confirmed operation:** return the recorded result for the same operation and request.
- **Active dispatch lease:** report that another worker is still handling the operation; do not start a competing dispatch.
- **Confirmed non-execution:** make the operation eligible for retry only when the probe's contract makes that result terminal.
- **Unknown outcome:** run the configured probe, use the provider's documented idempotency behavior, or quarantine.
- **Conflicting operation ID:** raise OperationConflictError before another external dispatch.
- **Result that cannot be stored as JSON:** preserve an unknown outcome rather than claiming a durable replay result that was not stored.

Operations can be inspected with **SQLiteEffectLedger.list_operations(status=EffectStatus.QUARANTINED)**; transitions are available through **SQLiteEffectLedger.events(operation_id)**. The separate `agent-effect-ops` CLI can list operation summaries, inspect event history, and record auditable manual confirmation, rejection, or retry authorization. These operations require an actor, reason, and evidence reference, but the CLI does not authenticate the actor; restrict access to the ledger and enforce operator authorization at the environment boundary. See [Effect Contracts and Recovery](docs/effect-contracts.md).

## Fault-injection testkit

The demo runs a fake payment provider and asserts on the count of committed effects—not only on the agent's reported status.

~~~bash
# Provider commits, but the acknowledgement is lost.
agent-effect --scenario after-commit-before-ack

# The request fails before the provider commits, so a safe retry is possible.
agent-effect --scenario before-commit

# Save the JSON report as a CI artifact.
agent-effect --scenario after-commit-before-ack --output artifacts/effect-report.json
~~~

Inspect an existing ledger and its quarantined operations:

~~~bash
agent-effect-ops --db agent-effects.sqlite3 list --status quarantined
agent-effect-ops --db agent-effects.sqlite3 events invoice:INV-1042:capture
~~~

After independently verifying the provider outcome, record a manual resolution with an auditable operator identity and evidence reference:

~~~bash
agent-effect-ops --db agent-effects.sqlite3 confirm invoice:INV-1042:capture \
  --output-json '{"payment_id":"pay-123","status":"captured"}' \
  --provider-operation-id pay-123 \
  --actor on-call-operator \
  --reason "Verified provider capture record" \
  --evidence-reference INC-1042/provider-event-123

# Only after establishing terminal non-execution:
agent-effect-ops --db agent-effects.sqlite3 authorize-retry invoice:INV-1042:capture \
  --actor on-call-operator \
  --reason "Provider confirms request cannot still commit" \
  --evidence-reference INC-1042/provider-terminal-query
~~~

The JSON report includes the scenario, operation ID, final status, attempt count, committed-effect count, duplicate detection, and event types. The built-in fake provider is deterministic and does not call a real payment service.

The test suite covers:

| Scenario | Expected behavior |
| --- | --- |
| Destination commits, acknowledgement is dropped | Probe finds the committed effect; the library confirms it without another dispatch. |
| Failure before commit | An authoritative negative probe allows one retry; one effect is committed. |
| Timeout without probe or idempotency guarantee | The operation is quarantined and the handler is not automatically run again. |
| Same operation ID, different arguments | The request fingerprint conflict is rejected. |
| Repeated invocation after confirmation | The stored result is replayed after reopening the SQLite ledger. |
| Provider reports pending | The operation remains unknown and is not retried blindly. |

This initial testkit exercises local contracts and a deterministic fake provider. A production-grade library should add more adapters and chaos scenarios only with the same independent effect-count assertions.

## Persistence and concurrency

SQLiteEffectLedger stores operation records and their event history in a file-backed SQLite database. Claims and state changes use database transactions; an active dispatch receives a time-limited ownership token so immediate concurrent invocations do not both start the same operation.

SQLite is intended for local and single-host workflows. A durable SQLite database does not make an external provider transactional, and it is not a substitute for a shared coordination backend in a multi-host deployment. A PostgreSQL ledger and its cross-host concurrency contract are not included in the current implementation.

Lease expiry also does not prove that a slow handler or remote request stopped. The effect executor can bound how long it waits for a handler, but a running Python thread cannot be forcibly terminated. Provider-native network timeouts are still required. After an uncertain timeout or lease expiry, the executor must reconcile the provider outcome; it must not infer that retry is safe from a timeout alone.

## Security model and limits

Production Agent Systems treats model output and tool arguments as untrusted. It records business-operation identity separately from the model's reasoning and does not let model text turn an unknown outcome into a confirmed success.

The integrating application remains responsible for:

- authenticating users and authorizing access to the specific resource named by a tool;
- protecting credentials and using least-privilege provider permissions;
- collecting real human approval through a trusted channel;
- providing correct operation identities and truthful provider contracts;
- preventing sensitive customer payloads or secrets from being copied into logs or evidence.

This package is not a payment processor, an authentication service, a host/container sandbox, or a universal guarantee of exactly-once execution.

### Guarantees and non-guarantees

Subject to the configured ledger and destination contract, the effect core provides durable operation records, request-conflict detection, recorded-result replay, conservative handling of ambiguous writes, and a reproducible way to test selected failure modes.

It cannot guarantee that every outside system changes exactly once when that system offers neither a reliable idempotency mechanism nor authoritative outcome evidence. When the state cannot be established, the correct result may be a quarantined operation requiring human or application-level reconciliation.

## Research behind the design

These studies motivate the failure models and testing approach. They are not evidence that this library itself has achieved the same benchmark results.

1. **Li, J. (September 2026). _Where Does Exactly-Once Live? Model, Harness, and Tool-Contract Effects on Duplicate Side Effects in LLM Agents._** [arXiv:2609.29095](https://arxiv.org/abs/2609.29095). The LIMBO study evaluates 25,930 episodes across nine models and multiple tool contracts and fault conditions. It finds that late commits and missing or weak tool contracts make verification-only strategies insufficient; stable provider idempotency keys reduced duplicates in the tested conditions.
2. **Tan et al. (August 2026). _AgentChaos: Chaos Engineering for Agent Systems via Programmatic Fault Injection._** [arXiv:2608.06790](https://arxiv.org/abs/2608.06790). This work injects faults at the LLM API boundary and reports that pass@1 fell by up to 50 percentage points across the tested fault configurations, supporting the use of repeatable fault injection rather than happy-path-only evaluation.
3. **Mazumder and Lia (July 2026). _AgentCheck: A Reproduce-Intervene-Mitigate Workbench for LLM Agents over MCP._** [arXiv:2607.11098](https://arxiv.org/abs/2607.11098). AgentCheck replays tool interactions with injected faults so developers can compare a mitigation under the same failure conditions before deployment.
4. **Zhang et al. (August 2026). _When Agentic Executions Fail: Detecting and Localizing Runtime Faults from Telemetry._** [arXiv:2608.14680](https://arxiv.org/abs/2608.14680). AGENTCHAOSBENCH studies runtime-fault diagnosis from execution telemetry and shows why the final task answer alone is insufficient to determine where an execution failed.
5. **Temporal documentation: _Error handling and idempotent Activities._** [Official guide](https://docs.temporal.io/develop/python/best-practices/error-handling). Temporal recommends idempotent activities and stable provider idempotency keys because activities can be retried after a worker fails.
6. **LangGraph documentation: _Persistence._** [Official guide](https://docs.langchain.com/oss/python/langgraph/persistence). LangGraph checkpoints make graph state resumable; they do not independently make a separate external write atomic with the checkpoint.

Related work includes [swarmproof/exactly-once](https://github.com/swarmproof/exactly-once), a focused idempotency middleware for agent side effects, as well as workflow systems such as Temporal and stateful orchestration frameworks such as LangGraph. These tools overlap in places and should be compared by their documented guarantees, supported stores, and integration boundaries—not by the claim of “exactly once” alone.

## Development

~~~bash
python -m venv .venv
# Activate the environment using your platform's command.
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests examples
mypy src
~~~

Run the deterministic fault scenarios without a model API key:

~~~bash
agent-effect --scenario after-commit-before-ack
agent-effect --scenario before-commit
~~~

The default tests must never contact production payment, messaging, or infrastructure services.

## Current scope and next steps

The current branch implements the standalone effect contract, SQLite ledger, effect-aware AgentRuntime integration, public API, and deterministic fault-injection demo. The next engineering steps are:

1. Add process-level crash tests and extend concurrency testing against independent SQLite ledger instances.
2. Add and validate a PostgreSQL ledger before claiming cross-host coordination.
3. Build framework adapters only after the contract and recovery state machine are stable.
4. Expand the fault matrix and compare results against a baseline using the same independently counted committed-effect ledger.
5. Add an operator-facing CLI for inspecting and resolving quarantined operations; the current resolution methods are available as a Python API.

These items are not claimed as completed until implementation, tests, and documentation all agree. See [CHANGELOG.md](CHANGELOG.md) for the current development scope and [SECURITY.md](SECURITY.md) for vulnerability disclosure and security boundaries.

## License

Production Agent Systems is distributed under the [MIT License](LICENSE).
