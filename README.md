# Production Agent Systems

A production-oriented, provider-neutral agent runtime that demonstrates **state, tools, runtime policy, reliability, checkpoint/recovery, trace, and replay** as explicit engineering boundaries.

## V1

V1 is a runnable core runtime, not a prompt demo. It executes an end-to-end agent trajectory with deterministic planning, structured tool contracts, permission checks, bounded retries/timeouts, idempotent side effects, checkpoints, recovery, and replayable events.

    Planner decision
          ↓
    Runtime policy
          ↓
    Tool validation
          ↓
    Retry / timeout
          ↓
    State + checkpoint
          ↓
    Trace
          ↓
    Replay

## Quick Start

    python -m pip install -e .
    python -m pytest -q
    python examples/demo.py

## Repository Role

This project is the capstone in a portfolio progression from agent concepts and LLM application engineering toward reliable production AI systems. V1 deliberately stays provider-neutral so reliability and runtime boundaries can be tested without network access or a specific model vendor.

See docs/v1.md for the V1 boundary and docs/architecture.md for the runtime architecture.

## Quality

Development dependencies are declared for pytest, Ruff, and mypy. CI runs the test suite and static quality gates on pushes and pull requests.

## Roadmap After V1

The larger execution plan continues with model gateway/routing, caching, AI-security controls, deterministic degradation, cost/latency telemetry, fault-injection benchmarks, evaluation integration, API/operational surfaces, and architecture evidence.
