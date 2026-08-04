# Documentation Map

The working tree keeps current contracts, accepted planned design, explicit
conformance debt, and operational guidance. Superseded designs and
implementation histories remain available through Git history.

## Start here

1. [`architecture/README.md`](architecture/README.md) — architecture topic
   router and system graph.
2. [`architecture/topics/system-map.md`](architecture/topics/system-map.md) —
   shortest complete description of the running system.
3. [`../README.md`](../README.md) — current product scope and operator usage.

Use the architecture router instead of reading the directory linearly. Topic
pages explain flows and link to adjacent systems. Reference pages are loaded
only when exact schemas, ownership, paths, or invariants matter.

For installation and server execution, read
[`deployment/migration.md`](deployment/migration.md) and
[`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md).

## Document roles

| File or folder | Purpose |
|---|---|
| `architecture/topics/` | System map plus task-oriented subsystem nodes |
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
topic edge in the same change. Do not add a status snapshot, implementation
summary, phase document, or second description of an existing architecture
boundary.
