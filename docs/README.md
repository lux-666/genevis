# Documentation Map

Coordination layer for Gene Agent. Treat as part of the system, not a
side note.

## Start Here

If you are taking over:

1. `handoff.md` — current state, next best task, verification, risks.
2. `governance.md` — coordinator responsibilities, source-of-truth order,
   boundary rules.
3. `architecture.md` — parent graph, modules, state categories.
4. `stage_contracts.md` — stable inputs / outputs / planning rules per
   biological stage.
5. `descriptor_spec.md` — descriptor registry, package and method
   descriptors.
6. `phases/phase_completion_plan.md` — long-range roadmap, with pointers
   into per-phase folders for history.
7. The phase folder relevant to the current work
   (`phases/phase1/` … `phases/phase5/`).

If you are setting this project up on a new host, read
`deployment/migration.md` for the conda env / install checklist.

## Document Roles

| File / Folder | Purpose |
|---|---|
| `handoff.md` | Current status, next task, verification, known risks, stop conditions |
| `governance.md` | Coordinator responsibilities, source-of-truth order, boundary rules |
| `architecture.md` | Parent graph, modules, state categories, extension rule |
| `stage_contracts.md` | Stable inputs, outputs, planning rules, and review triggers per stage |
| `descriptor_spec.md` | Descriptor registry, package descriptor, method descriptor, asset rules |
| `deployment/mac_dev_server_pull.md` | Mac-dev / server `git pull` workflow contract |
| `deployment/migration.md` | New-host setup checklist (conda envs, installs, paths) |
| `examples/` | Manifest and run-config templates |
| `phases/phase_completion_plan.md` | Long-range roadmap |
| `phases/phaseN/` | Per-phase retrospectives and numbered task handoffs |
| `archive/` | Original biology source documents (kept for reference) |

## Update Rule

When code behavior changes, update the closest matching document in the
same work session. If the next agent needs different instructions,
update `handoff.md` before stopping. Avoid restating in `handoff.md`
what already lives in `README.md`, `stage_contracts.md`, or a phase doc
— prefer one source of truth and a pointer.
