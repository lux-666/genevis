# Documentation Map

The working tree keeps current contracts, accepted planned design, explicit
conformance debt, and operational guidance. Superseded designs and
implementation histories remain available through Git history.

## Start here

1. [`architecture/README.md`](architecture/README.md) — architecture topic
   router and system graph.
2. [`architecture/topics/system-map.md`](architecture/topics/system-map.md) —
   shortest complete description of the running system.
3. [`architecture/topics/langgraph-implementation-map.md`](architecture/topics/langgraph-implementation-map.md)
   — current parent/stage wiring, method execution status, parameter coverage,
   verification evidence, and explicit missing capabilities.
4. [`../README.md`](../README.md) — current product scope and operator usage.

Use the architecture router instead of reading the directory linearly. Topic
pages explain flows and link to adjacent systems. Reference pages are loaded
only when exact schemas, ownership, paths, or invariants matter.

For the fastest developer orientation, read the system map first to understand
ownership, then the LangGraph implementation map to see what is actually wired
and executable. Follow a reference contract only when the task needs exact
fields, invariants, or conformance boundaries.

For installation and server execution, read
[`deployment/migration.md`](deployment/migration.md) and
[`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md).

## Document roles

| File or folder | Purpose |
|---|---|
| `architecture/topics/` | System map, maintained LangGraph implementation map, and task-oriented subsystem nodes |
| `architecture/reference/` | Exact architecture, stage, descriptor, governance, and conformance contracts |
| `deployment/` | Current host setup and Mac/server workflow |
| `user_guide/` | Current operator-facing feature guides |
| `examples/` | Schema-valid manifest and run-config templates |
| `archive/` | Original biological source material retained for reference |

## Status and update rule

Architecture pages use `implemented`, `planned`, and `conformance-debt`
explicitly. Planned behavior must not be described as available, and an
implementation exception must not silently weaken an accepted contract.

When behavior changes, update the single owning reference and every affected
topic edge in the same change. Do not add phase reports, historical progress
logs, completion percentages, or a second description of an existing
architecture boundary.

The single
[`langgraph-implementation-map.md`](architecture/topics/langgraph-implementation-map.md)
page is the deliberate exception to the ban on status snapshots. It is a
maintained, evidence-backed projection of current graph nodes, registered
methods, parameter flows, tests, and conformance debt; it does not own or
weaken their exact contracts. A change to any mapped implementation boundary
must update that page in the same change. Historical states remain in Git
history rather than accumulating in the map.
