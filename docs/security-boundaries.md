# Security Boundaries

This document records the implemented safeguards and their limits. The package is a runtime library, not a complete authenticated production service. Effect ledger events are persisted in SQLite, but that ledger is not a tamper-evident audit archive and the local operator CLI does not authenticate its caller.

## Tool input validation

Each **ToolDefinition** declares an input schema. Before a handler is invoked, the runtime validates the supplied arguments against the supported schema subset:

- required fields and primitive types (string, integer, number, boolean, object, and array);
- numeric bounds, string lengths/patterns, enum values, array item schemas, item counts, and unique-item constraints;
- nested object properties and required fields;
- no unknown fields by default, including inside nested objects.

A tool may declare **additionalProperties** as **true** to accept arbitrary extra values or provide a schema that validates each extra value. Prefer an explicit schema. This is a deliberately bounded JSON-Schema-like subset, not full JSON Schema support.

Invalid arguments are rejected before the handler runs. The executor returns a failed **ToolResult**, and invalid-input failures are not retried by the default retry policy.

## Mutation approval

Mutation tools can require confirmation through **PermissionPolicy**. The approval signal is the explicit **confirmed_mutations** argument passed to **AgentRuntime.run** by a trusted outer layer:

```python
runtime.run(task, confirmed_mutations=frozenset({"suspend_customer"}))
```

**Task.metadata** is task data and **never grants permission**. Model output, task descriptions, tool arguments, and retrieved text must not be used to populate **confirmed_mutations**. Confirmations apply to that invocation and are not persisted into checkpoints.

The runtime cannot itself determine whether a human genuinely approved an action. The application or orchestration layer that calls **run** must collect approval through a trusted channel, authenticate the approver when required, and pass only the exact tools that were approved. A caller that blindly trusts user-controlled input can still misuse this API.

## Effect ledger and operator resolution

- The effect ledger durably stores operation status and event history in a file-backed SQLite database. Database-file access therefore grants access to operational metadata and may grant the ability to alter local state; protect it with filesystem permissions and backups.
- The local `agent-effect-ops` CLI records an actor identifier, reason, and evidence reference for manual resolution. Those strings are audit context, not proof of identity or proof that the evidence is valid. Put the CLI behind a trusted operator environment and authenticate/authorize the person before invoking it.
- Manual retry authorization is an assertion that the prior operation is terminal and cannot still commit. Do not authorize retry merely because a timeout occurred or a lookup temporarily returned no result.
- Effect ledger events are ordered but not cryptographically chained or externally anchored. They must not be represented as tamper-evident compliance evidence.

## Remaining limitations

- The runtime is provider-neutral; it does not implement a complete defense against prompt injection inside an LLM.
- Allowlisting and confirmation are tool-level controls, not resource-level authorization. A handler must still validate that the caller may modify the specific customer, file, account, or other resource named in its arguments.
- The package does not provide HTTP authentication, tenant isolation, secret management, an authenticated remote administration API, or OS/container sandboxing. Generic runtime state, trace, and audit sinks remain in-memory reference implementations.
- Tool handlers should use least privilege, avoid exposing secrets to model context, and treat every model-generated argument as untrusted.
- A timeout of a Python worker thread does not forcibly terminate an already-running handler; external side effects need idempotency and downstream timeouts.

## Security and recovery regression coverage

**tests/test_tool_validation.py** checks strict input validation and confirms invalid arguments do not reach a handler. **tests/test_runtime.py** checks that task metadata cannot approve a mutation and that explicit caller-supplied approval still works. **tests/test_effects.py** checks operation-ID conflict detection, unknown-outcome handling, safe provider-key replay, timeouts, and evidence-backed manual resolution. **tests/test_effect_cli.py** checks that the local operations CLI emits summaries without request payloads and records manual-resolution events.

These tests establish the tested library behavior; they do not prove the security of every surrounding application or tool handler.
