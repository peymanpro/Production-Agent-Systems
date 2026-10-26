# Production Agent Systems

Production Agent Systems is a provider-neutral Python library for making agent tool effects safer to execute, recover, and test under real-world failures. It defines explicit contracts for external operations, preserves ambiguous outcomes instead of guessing, and provides fault-injection tests that verify what actually happened at the effect boundary.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/peymanpro/Production-Agent-Systems/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/peymanpro/Production-Agent-Systems/actions/workflows/quality.yml)
[![Latest release](https://img.shields.io/github/v/release/peymanpro/Production-Agent-Systems)](https://github.com/peymanpro/Production-Agent-Systems/releases)

> **The core problem:** an agent calls an external service, the service commits the operation, and the response is lost. The agent sees a timeout—not proof of failure. Retrying blindly may charge a customer twice; stopping may leave required work unfinished.

Production Agent Systems is designed for that gap between **what the agent believes happened** and **what the external system actually committed**.

## Table of contents

- [Why this library exists](#why-this-library-exists)
- [Capabilities](#capabilities)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How effect assurance works](#how-effect-assurance-works)
- [Reliability testing and fault injection](#reliability-testing-and-fault-injection)
- [Storage and execution guarantees](#storage-and-execution-guarantees)
- [Integrations](#integrations)
- [Security model](#security-model)
- [What this library does not promise](#what-this-library-does-not-promise)
- [Research behind the design](#research-behind-the-design)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Why this library exists

Agent frameworks can retry tool calls, persist execution state, and resume interrupted runs. Those features do not, by themselves, establish whether a write to another system took effect. The application still needs a sound contract for each external operation.

Consider a payment tool:

1. The agent submits a capture request.
2. The payment provider commits the capture.
3. The acknowledgement is lost in transit.
4. The agent receives a timeout and resumes or retries.
5. Without an idempotency contract or reliable reconciliation, another capture may occur—or the application may incorrectly report failure.

The same failure pattern appears in email delivery, ticket creation, cloud resource provisioning, deployment, inventory updates, and other operations with external side effects.

Production Agent Systems addresses this at the **effect boundary**. It does not assume that a timeout means failure, that a checkpoint proves an external action happened once, or that an LLM's final answer is evidence of the real-world outcome.

## Capabilities

- **Explicit effect contracts** — describe an operation's identity, effect type, provider idempotency support, outcome probe, postcondition, and unknown-outcome policy.
- **Stable operation identity** — give retries and recovery a business-level operation ID that remains stable for the same logical operation.
- **Request fingerprinting** — reject reuse of an operation ID with materially different request data.
- **Durable effect ledger** — atomically record claims, dispatch state, attempts, provider references, observed outcomes, and reconciliation history.
- **Outcome-aware recovery** — distinguish confirmed success, confirmed non-execution, pending work, and an outcome that cannot yet be established.
- **Safe handling of ambiguity** — quarantine uncertain mutations instead of silently retrying them when the contract cannot prove a retry is safe.
- **Duplicate suppression and result replay** — reuse a committed result for the same operation rather than re-running its handler.
- **Concurrency control** — prevent competing workers from independently claiming the same operation when the selected ledger backend supports the documented atomicity contract.
- **Fault-injection testkit** — reproduce failures such as lost acknowledgements after commit, worker crashes, duplicate delivery, stale read-back, and concurrent retries.
- **Machine-readable reports** — export scenario results for continuous integration, compare behavior across versions, and retain reproducible evidence.
- **Provider-neutral core** — no model API key or model-provider dependency is needed to test runtime behavior.
- **Framework adapters** — use the core directly or connect it to supported agent orchestration frameworks without making the effect ledger part of model reasoning.

## Installation

Install the core library:

```bash
python -m pip install production-agent-systems
```

Install an optional integration when needed:

```bash
python -m pip install "production-agent-systems[postgres]"
python -m pip install "production-agent-systems[langgraph]"
```

The default core is suitable for local development and single-host execution. Select a shared transactional backend for deployments in which multiple workers must coordinate the same operations.

## Quick start

The following example shows the intended public API around a payment operation. The provider's idempotency key is derived from the stable business identity—not generated anew for each attempt.

```python
from production_agent_systems import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    SQLiteEffectLedger,
    UnknownOutcomePolicy,
)

ledger = SQLiteEffectLedger("agent-effects.sqlite3")
effects = EffectExecutor(ledger=ledger)


def capture_payment(arguments, *, idempotency_key):
    # Pass the same key to the provider on every attempt for this operation.
    return payment_provider.capture(
        invoice_id=arguments["invoice_id"],
        idempotency_key=idempotency_key,
    )


def find_payment_outcome(arguments, *, idempotency_key):
    # Return a typed probe result: COMMITTED, NOT_COMMITTED, PENDING, or UNKNOWN.
    return payment_provider.lookup_by_idempotency_key(idempotency_key)


payment_contract = EffectContract(
    name="capture_payment",
    operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
    provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
    outcome_probe=find_payment_outcome,
    unknown_outcome=UnknownOutcomePolicy.QUARANTINE,
)

result = effects.execute(
    contract=payment_contract,
    arguments={"invoice_id": "INV-1042"},
    handler=capture_payment,
)

if result.status is EffectStatus.CONFIRMED:
    print("Payment capture is confirmed.")
elif result.status is EffectStatus.OUTCOME_UNKNOWN:
    print("The result is uncertain; automatic re-execution is blocked.")
    print(f"Operation to reconcile: {result.operation_id}")
```

The example is deliberately explicit about two separate identities:

- **Operation ID** identifies the logical business operation across retries and recovery.
- **Provider idempotency key** is passed to the external service when that service supports the corresponding contract.

Do not derive either identifier from a new random value on every retry. Do not reuse the same operation ID for two distinct business operations. The library also fingerprints the request so accidental key reuse with different arguments is rejected.

## How effect assurance works

Each operation moves through a durable state machine rather than treating every tool result as a simple success/failure flag.

```text
PREPARED
   |
   v
DISPATCHED ----------------------+
   |                             |
   | authoritative evidence      | timeout, crash, lost response
   v                             v
CONFIRMED / REJECTED       OUTCOME_UNKNOWN
                                  |
                                  v
                             RECONCILING
                           /      |       \
                          v       v        v
                    CONFIRMED  RETRYABLE  QUARANTINED
```

The exact transitions depend on the tool contract and the evidence available. A probe that returns no record is not automatically proof that no operation occurred: the target service may still be processing the request, or its read path may be eventually consistent.

### Effect contracts

A contract defines the operation-specific rules the runtime needs to make a recovery decision:

| Contract field | Purpose |
| --- | --- |
| Operation identity | Derives a stable ID for a single logical business operation. |
| Effect classification | Distinguishes reads, mutations, and operations with multiple external effects. |
| Provider idempotency | Defines whether and how a stable idempotency key is sent to the destination. |
| Outcome probe | Queries the destination for authoritative evidence when available. |
| Postcondition | Defines the evidence required before the operation is considered confirmed. |
| Unknown-outcome policy | Specifies whether the runtime waits, probes again, quarantines, or requests trusted human intervention. |
| Retry policy | Restricts retries to cases allowed by the contract and the observed outcome. |

An LLM may propose an action, but it cannot grant itself permission, override the operation contract, or convert an unknown outcome into a confirmed success.

### Recovery behavior

When an operation is interrupted, the runtime loads its durable record and evaluates the recorded state together with the operation contract. It does not infer external success solely from the last model message or from the existence of an agent checkpoint.

- **Confirmed commit:** return the recorded result; do not execute the effect again.
- **Confirmed non-execution:** retry only if the contract says the operation is safe to retry.
- **Still in flight or eventually consistent:** wait or probe according to a bounded policy.
- **Unknown outcome:** preserve the ambiguity and quarantine the operation when safe resolution is not possible.
- **Conflicting operation identity:** reject the request instead of treating different work as a duplicate.

This makes recovery inspectable and testable. It does not eliminate the need for idempotency or transaction support in the external system.

## Reliability testing and fault injection

A reliability claim is useful only when the relevant failure can be reproduced and the resulting external effects can be counted independently. The testkit provides deterministic scenarios and a fake effect ledger so a test can verify both the agent's reported outcome and the number of effects actually committed by the destination.

Example CLI workflows:

```bash
# Run the reliability suite
agent-effect test --suite tests/reliability

# Reproduce a lost acknowledgement after a destination commit
agent-effect test --scenario tests/reliability/commit-then-drop-ack.yaml

# Export a machine-readable report for CI
agent-effect report artifacts/reliability.json --format json
```

The testkit supports scenarios such as:

| Injected fault | What the test verifies |
| --- | --- |
| Failure before dispatch | A proven non-execution can be retried according to policy. |
| Destination commits, acknowledgement is dropped | The operation is not blindly executed a second time. |
| Worker exits after commit but before local result persistence | Recovery consults the ledger/provider contract instead of assuming the effect did not happen. |
| Duplicate delivery or concurrent workers | The configured backend's atomic claim prevents two independent executions of one operation. |
| Delayed or stale read-back | A temporarily absent result is not misclassified as definitive non-execution. |
| Same operation ID with different arguments | The fingerprint conflict is rejected before dispatch. |
| Provider cannot establish the outcome | The operation remains explicitly unknown or quarantined; the report does not claim a false success. |

Reports include scenario configuration, injected and observed faults, committed-effect counts, recovery decisions, attempts, terminal operation state, and pass/fail reasons. CI can fail on a violated contract.

The same scenario should be run against the baseline and the candidate version. Improvements are meaningful only when the workload, fault point, and expected effect ledger remain the same.

## Storage and execution guarantees

Choose the backend based on the failure and deployment model. The strength of the guarantee is part of the backend contract—not a generic claim shared by every store.

| Backend | Intended use | Coordination boundary |
| --- | --- | --- |
| In-memory ledger | Unit tests and local examples | One process; state is lost on restart. |
| SQLite ledger | Local tools, jobs, and single-host deployments | Durable on one host with atomic claims supported by the configured transaction model. |
| PostgreSQL ledger | Shared workers and multi-host deployments | Uses transactional coordination for operations sharing the same database. |

The runtime records operation identity, request fingerprint, state transitions, attempts, provider references, and reconciliation evidence. The stored result is replayed only for the same logical operation and compatible request.

### What the library guarantees

Subject to the selected backend's documented atomicity and the external system's contract, the library:

- prevents competing callers from independently claiming the same operation ID;
- returns a recorded committed result for a repeated request with the same operation identity;
- rejects operation-ID reuse with a mismatched request fingerprint;
- avoids blind re-execution when a dispatched mutation has an unknown outcome and the contract cannot establish that retry is safe;
- makes recovery decisions and fault-test results inspectable.

### What still depends on the external system

A library cannot independently guarantee that the outside world changes exactly once when the destination offers neither an idempotency contract nor an authoritative way to determine the result of a request. In that case, the safe choice may be to stop and request reconciliation rather than automatically retry.

For operations that support it, pass the stable provider idempotency key through to the provider. When several effects must commit together, use the destination's transaction, deduplication, or workflow contract rather than assuming the Python process can make remote systems atomic.

## Integrations

The core is framework-agnostic and can guard tool handlers directly. Optional adapters integrate effect contracts and recovery metadata with supported orchestration frameworks.

| Component | Responsibility |
| --- | --- |
| Production Agent Systems | Effect contracts, operation ledger, outcome classification, recovery policy, and fault-injection testkit. |
| LangGraph or another orchestration framework | Graph state, routing, model/tool orchestration, and framework lifecycle. |
| External provider | Authoritative business result and any native idempotency guarantee it supports. |
| Application | Authentication, resource-level authorization, business identity, and trusted human approval. |

The library complements durable workflow systems such as [Temporal](https://docs.temporal.io/develop/python/best-practices/error-handling) and stateful agent frameworks such as [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence). It is not a replacement for workflow orchestration, database transactions, or provider-native idempotency keys.

## Security model

Production Agent Systems treats model output, tool arguments, retrieved content, and task metadata as untrusted input.

- Tool input is validated against its declared schema before the handler is invoked.
- Unknown fields are rejected by default unless the contract explicitly allows and validates them.
- Runtime policy and trusted application code—not model text—control permission and mutation approval.
- Operation IDs and request fingerprints are validated before an effect is dispatched.
- Audit and test reports support configurable redaction so secrets and sensitive payloads do not need to be copied into traces.
- Unknown outcomes fail closed when the configured policy cannot establish that repeating a mutation is safe.

### Responsibilities that remain with the application

This library is not an authentication system or an OS/container sandbox. The integrating application remains responsible for authenticating users, authorizing access to the specific resource named in a tool request, protecting credentials, limiting tool privileges, and collecting genuine human approvals through a trusted channel. A handler must still enforce downstream permissions.

A timeout on a Python worker thread does not prove that an already-started handler or remote request stopped. Use downstream timeouts, stable idempotency keys, and reconciliation where possible.

## What this library does not promise

- **Universal exactly-once execution.** No library can infer every external commit from a timeout alone. Strong effect guarantees require cooperation from the destination or authoritative outcome evidence.
- **Automatic recovery from every unknown result.** Some operations must remain quarantined for later investigation.
- **Identical LLM behavior on replay.** The library records and tests effect-level execution decisions; model output, provider latency, and external data may still vary.
- **A complete agent framework.** Model selection, prompt design, multi-agent planning, and general-purpose workflow orchestration remain outside the core purpose.
- **A security boundary by itself.** The library does not replace application authorization, provider permissions, secret management, or infrastructure isolation.

## Research behind the design

The library's design is informed by research on duplicate external effects and reliability testing in agent systems. These studies motivate the contracts and fault models described here; they are not, by themselves, evidence that this library has reproduced the same results.

1. **Li, J. (September 2026). _Where Does Exactly-Once Live? Model, Harness, and Tool-Contract Effects on Duplicate Side Effects in LLM Agents._** [Microsoft Research](https://www.microsoft.com/en-us/research/publication/where-does-exactly-once-live-model-harness-and-tool-contract-effects-on-duplicate-side-effects-in-llm-agents/) · [arXiv:2609.29095](https://arxiv.org/abs/2609.29095). In 25,930 simulated episodes, duplicate-effect behavior depended on the available tool contract and fault; provider idempotency keys materially reduced duplicates in the tested conditions. The paper also shows why outcome verification alone cannot guarantee exactly-once effects when an operation can commit late.
2. **Tan et al. (2026). _AgentChaos: Chaos Engineering for Agent Systems via Programmatic Fault Injection._** [arXiv:2608.06790](https://arxiv.org/abs/2608.06790). This work studies runtime injection of crashes, omissions, and corrupted values at the model API boundary. Across its tested configurations, faults reduced agent pass@1 by as much as 50 percentage points, motivating systematic fault scenarios rather than happy-path-only tests.
3. **Mazumder and Lia (2026). _AgentCheck: A Reproduce-Intervene-Mitigate Workbench for LLM Agents over MCP._** [arXiv:2607.11098](https://arxiv.org/abs/2607.11098). The work presents repeatable tool-response perturbation and mitigation comparison. It motivates replaying the same failure conditions when verifying a reliability fix.
4. **Zhang et al. (2026). _When Agentic Executions Fail: Detecting and Localizing Runtime Faults from Telemetry._** [arXiv:2608.14680](https://arxiv.org/abs/2608.14680). This research evaluates runtime-fault detection and localization from agent execution traces, supporting the practice of retaining step-level evidence rather than judging reliability only from a final answer.
5. **Temporal documentation: _Error handling and idempotent Activities._** [Official guide](https://docs.temporal.io/develop/python/best-practices/error-handling). Temporal documents why retried Activities should be idempotent and recommends passing stable idempotency keys to external services.
6. **LangGraph documentation: _Persistence._** [Official guide](https://docs.langchain.com/oss/python/langgraph/persistence). LangGraph explains checkpointing for resumable graph state. Production Agent Systems treats that state persistence as complementary to—not a substitute for—an external effect contract.

Related work includes [swarmproof/exactly-once](https://github.com/swarmproof/exactly-once), a focused idempotency middleware for side effects. Production Agent Systems focuses on the combined developer workflow of effect contracts, explicit unknown-outcome recovery, a runtime boundary, and reproducible fault-injection testing. The projects address overlapping concerns and should be evaluated by their documented guarantees and integration requirements.

## Development

Create an environment and install the development dependencies:

```bash
python -m venv .venv
# Activate the environment using your platform's command.
python -m pip install -e ".[dev]"
```

Run the tests and quality checks:

```bash
python -m pytest -q
python -m ruff check .
python -m mypy src
```

Run the deterministic demonstration and reliability suite without model-provider credentials:

```bash
python examples/demo.py
agent-effect test --suite tests/reliability
```

External integration tests are opt-in and use isolated test services or explicitly configured test credentials. The default test suite must never contact production payment, messaging, or infrastructure services.

## Contributing

Contributions should include tests for the behavior they change, clear documentation of the guarantee being added, and the limits of that guarantee. Changes to operation states, persistence semantics, retry behavior, or provider contracts should include a design note and regression tests covering failure paths—not only successful execution.

Please never include production secrets, customer data, or live payment credentials in examples, fixtures, traces, or issue reports.

## License

Production Agent Systems is distributed under the [MIT License](LICENSE).
