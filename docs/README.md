# Documentation Map

The working tree keeps current contracts, accepted planned design, explicit
conformance debt, and operational guidance. Superseded designs and
implementation histories remain available through Git history.

## Start here

1. [`architecture/README.md`](architecture/README.md) — architecture topic
   router and system graph.
2. [`architecture/topics/system-map.md`](architecture/topics/system-map.md) —
   shortest complete description of the running system.
3. [`architecture/langgraph-implementation-map.md`](architecture/langgraph-implementation-map.md)
   — current parent/stage wiring, method execution status, parameter coverage,
   verification evidence, and explicit missing capabilities.
4. [`../README.md`](../README.md) — current product scope and operator usage.

Use the architecture router instead of reading the directory linearly. Topic
pages explain flows and link to adjacent systems. Reference pages are loaded
only when exact schemas, ownership, paths, or invariants matter.

For the fastest developer orientation, follow this reading chain:

```text
docs/README.md
  -> architecture/README.md
     -> topics/system-map.md for system ownership
     -> langgraph-implementation-map.md for implementation maturity
        -> topics/<subsystem>.md for behavioral explanation
           -> reference/<contract>.md for exact fields and invariants
```

For installation and server execution, read
[`deployment/migration.md`](deployment/migration.md) and
[`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md).

## Document roles

| File or folder | Purpose |
|---|---|
| `architecture/langgraph-implementation-map.md` | Cross-cutting, evidence-backed projection of current graph and method maturity |
| `architecture/topics/` | System map and task-oriented subsystem nodes |
| `architecture/reference/` | Exact architecture, stage, descriptor, governance, and conformance contracts |
| `deployment/` | Current host setup and Mac/server workflow |
| `user_guide/` | Current operator-facing feature guides |
| `examples/` | Schema-valid manifest and run-config templates, including literal-order repeated-stage occurrences |

`superpowers/specs/` and `superpowers/plans/` are legacy, non-authoritative
process material scheduled for removal. Do not add or maintain design there.
Move any still-valid contract into its single owning topic or reference page
under `architecture/`; Git history retains obsolete rationale and plans.

## Status and update rule

Architecture pages use `implemented`, `planned`, and `conformance-debt`
explicitly. Planned behavior must not be described as available, and an
implementation exception must not silently weaken an accepted contract.

Keep the change-synchronization chain distinct from the reading chain:

```text
code or behavior change
  -> owning reference contract
  -> each affected topic
  -> implementation map when wiring, status, evidence, or debt changes
  -> README/router only when navigation relationships change
```

The implementation-map step is required when a graph node, registered stage,
registered method, execution adapter, collection boundary, parameter flow,
human gate, resource commit, verification classification, or cited conformance
status changes. Do not add phase reports, historical progress logs, completion
percentages, or a second description of an existing architecture boundary.

The single
[`langgraph-implementation-map.md`](architecture/langgraph-implementation-map.md)
page is the deliberate exception to the ban on status snapshots. It is a
maintained, evidence-backed projection of current graph nodes, registered
methods, parameter flows, tests, and conformance debt; it does not own or
weaken their exact contracts. A change to any mapped implementation boundary
must update that page in the same change. Historical states remain in Git
history rather than accumulating in the map.
