# Architecture

V1 keeps the planner's decision separate from execution authority.

    +-------------------+
    | Deterministic     |
    | Planner (V1)      |
    +---------+---------+
              |
              v
    +---------+---------+
    |   Agent Runtime   |
    +---+-----+-----+---+
        |     |     |
        |     |     +----------------+
        |     |                      |
        v     v                      v
      State  Policy                Events
        |     |                      |
        v     v                      v
    Checkpoint Tool Registry     Trace / Replay
                  |
                  v
            Timeout / Retry
                  |
                  v
                 Tool

The planner proposes. The runtime authorizes, validates, executes, checkpoints, and records what happened.

## Reliability Boundary

A successful side effect is checkpointed together with an idempotency key before execution continues. A resumed workflow therefore has enough information to avoid blindly repeating the same side effect.

## V1 Limitation

The checkpoint and event implementations are in-memory reference implementations. The interfaces are deliberately replaceable; durable storage, external tracing, and multi-process execution belong to later phases.
