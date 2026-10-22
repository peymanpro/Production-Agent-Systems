# Security Boundaries — V1

This document records the runtime's implemented safeguards and their limits. The core package is a runtime library, not a complete authenticated production service.

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

## Remaining limitations

- The runtime is provider-neutral; it does not implement a complete defense against prompt injection inside an LLM.
- Allowlisting and confirmation are tool-level controls, not resource-level authorization. A handler must still validate that the caller may modify the specific customer, file, account, or other resource named in its arguments.
- The runtime does not provide HTTP authentication, tenant isolation, secret management, durable audit storage, or OS/container sandboxing.
- Tool handlers should use least privilege, avoid exposing secrets to model context, and treat every model-generated argument as untrusted.
- A timeout of a Python worker thread does not forcibly terminate an already-running handler; external side effects need idempotency and downstream timeouts.

## Security regression coverage

**tests/test_tool_validation.py** checks strict input validation and confirms invalid arguments do not reach a handler. **tests/test_runtime.py** checks that task metadata cannot approve a mutation and that explicit caller-supplied approval still works.

These tests establish the tested library behavior; they do not prove the security of every surrounding application or tool handler.
