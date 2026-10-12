# Production Agent Systems

A production-oriented, provider-neutral agent runtime that demonstrates **state, tools, runtime policy, reliability, checkpoint/recovery, trace, audit, replay, and dependency protection** as explicit engineering boundaries.

## V1

V1 is a runnable core runtime, not a prompt demo. It executes an end-to-end agent trajectory with deterministic planning, structured tool contracts, permission checks, bounded retries/timeouts, idempotent side effects, checkpoints, recovery, circuit breaking, separate audit records, and replayable events.

    Planner decision
          ↓
    Runtime policy
          ↓
    Tool validation
          ↓
    Retry / timeout / circuit breaker
          ↓
    State + checkpoint
          ↓
    Trace + audit
          ↓
    Replay

## Quick Start

    python -m pip install -e .
    python -m pytest -q
    python examples/demo.py

## Repository Role

This project is the capstone in a portfolio progression from agent concepts and LLM application engineering toward reliable production AI systems. V1 deliberately stays provider-neutral so reliability and runtime boundaries can be tested without network access or a specific model vendor.

See docs/v1.md for the V1 boundary and docs/architecture.md for the runtime architecture.

## Related engineering work

- [Building Software With LLMs](https://github.com/peymanpro/Building-Software-With-LLMs) focuses on the application boundary around model calls, tool use, and deterministic tests.
- [AI Evaluation Engineering](https://github.com/peymanpro/AI-Evaluation-Engineering) provides reusable evaluation, adversarial regression, and comparison workflows.
- [Evidence-Grounded-RAG](https://github.com/peymanpro/Evidence-Grounded-RAG) explores retrieval and citation-grounded generation.

These are separate portfolio projects; this runtime does not require them as packages or services.

## Quality

Development dependencies are declared for pytest, Ruff, and mypy. CI runs the test suite and static quality gates on pushes and pull requests.

## Roadmap After V1

The larger execution plan continues with richer model gateway/routing, caching, AI-security controls, deterministic degradation, cost/latency telemetry, fault-injection benchmarks, evaluation integration, API/operational surfaces, and architecture evidence.

## Automated releases

The version-driven release workflow reads `[project].version` from `pyproject.toml`, runs the test suite, Ruff, and mypy, builds the Python distributions, and publishes a GitHub Release with generated notes and the build artifacts.

For the next release, increment the project version to a new `MAJOR.MINOR.PATCH` value and push to `main`, or push a matching `vMAJOR.MINOR.PATCH` tag. Existing tags and releases are left unchanged. Release validation uses the provider-neutral runtime and deterministic tests; it does not require live model-provider credentials.
