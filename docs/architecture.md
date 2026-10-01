# Initial Architecture Boundary

The first repository unit establishes the physical project boundaries before agent behavior is introduced.

    Client / future API
           |
           v
    Agent Runtime (future)
           |
      +----+----+----------------+
      |         |                |
    State     Policy       Model Gateway
    (future)  (future)        (future)
      |         |                |
      +---------+----------------+
                |
             Tools (future)
                |
           Observability
              (future)

At this stage the diagram is an architectural boundary, not an implementation claim. Future units will add the runtime contracts one boundary at a time and verify each with executable tests.

## Design Constraint

The agent's eventual model/planner decision must never be treated as runtime authorization. The runtime will remain the authority for validation, permission, side effects, reliability policy, and state transitions.
