# Documentation Map

Coordination layer for Gene Agent. Treat as part of the system, not a
side note.

## Start Here

If you are taking over:

1. `architecture/README.md` — specification registry, authority, and current
   conformance status.
2. `architecture.md` — parent graph, modules, and system boundaries.
3. `handoff.md` — current work snapshot, open conformance gaps, and verification.
4. `governance.md` — coordinator responsibilities and change rules.
5. `stage_contracts.md` — stable inputs / outputs / planning rules per
   biological stage.
6. `descriptor_spec.md` — descriptor registry, package and method
   descriptors.
7. `phases/phase_completion_plan.md` — long-range roadmap, with pointers
   into per-phase folders for history.
8. The phase folder relevant to the current work
   (`phases/phase1/` … `phases/phase8/`).

If you are setting this project up on a new host, read
`deployment/migration.md` for the conda env / install checklist.

## Document Roles

| File / Folder | Purpose |
|---|---|
| `handoff.md` | Short-lived current work snapshot; never a normative architecture source |
| `governance.md` | Coordinator responsibilities, source-of-truth order, boundary rules |
| `architecture.md` | Parent graph, modules, state categories, extension rule |
| `architecture/` | Accepted cross-cutting specifications and their implementation-conformance status |
| `stage_contracts.md` | Stable inputs, outputs, planning rules, and review triggers per stage |
| `descriptor_spec.md` | Descriptor registry, package descriptor, method descriptor, asset rules |
| `deployment/mac_dev_server_pull.md` | Mac-dev / server `git pull` workflow contract |
| `deployment/migration.md` | New-host setup checklist (conda envs, installs, paths) |
| `examples/` | Manifest and run-config templates |
| `phases/phase_completion_plan.md` | Long-range roadmap |
| `phases/phaseN/` | Per-phase retrospectives and numbered task handoffs |
| `archive/` | Original biology source documents (kept for reference) |

## Update Rule

When code behavior changes, update the owning specification in the same work
session. Keep `handoff.md` short and current; it should point to specifications,
tests, or phase history rather than copying them. Design status and current
implementation conformance must be recorded separately.
