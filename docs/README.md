# Documentation Map

The working tree keeps current contracts and operational guidance. Superseded
designs, implementation summaries, and run histories remain available through
Git history.

## Start here

1. [`architecture.md`](architecture.md) — system flow and module ownership.
2. [`architecture/README.md`](architecture/README.md) — normative specification
   registry.
3. [`stage_contracts.md`](stage_contracts.md) — current biological stage
   inputs, outputs, methods, and review triggers.
4. [`descriptor_spec.md`](descriptor_spec.md) — package, method, and asset
   descriptor contracts.
5. [`../README.md`](../README.md) — current executable scope and usage.

For installation and server execution, read
[`deployment/migration.md`](deployment/migration.md) and
[`deployment/mac_dev_server_pull.md`](deployment/mac_dev_server_pull.md).

## Current document roles

| File or folder | Purpose |
|---|---|
| `architecture.md` | Parent graph, modules, state categories, and execution boundary |
| `architecture/` | One current owner for each cross-cutting contract |
| `stage_contracts.md` | Biological stage contracts; no cross-stage storage convention |
| `descriptor_spec.md` | Descriptor registry and capability metadata |
| `governance.md` | Current engineering and documentation boundary rules |
| `deployment/` | Current host setup and Mac/server workflow |
| `user_guide/` | Current operator-facing feature guides |
| `examples/` | Schema-valid manifest and run-config templates |
| `archive/` | Original biological source material retained for reference |

## Update rule

When behavior changes, update the single owning contract in the same change.
Do not add a status snapshot, implementation summary, phase document, or second
description of an existing architecture boundary.
