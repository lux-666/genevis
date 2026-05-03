# Documentation Map

This directory is the coordination layer for Gene Agent. Treat it as part of
the system, not a side note.

## Start Here

If you are taking over:

1. `handoff.md` tells you the current state and next best task.
2. `governance.md` tells you how to act as the coordinator.
3. `architecture.md` tells you the system boundaries.
4. `stage_contracts.md` tells you what each biological stage must do.
5. `descriptor_spec.md` tells you how method/package descriptors work.
6. `phases/phase1/phase1.md` gives the current Phase 1 critique and task order.
7. `phases/phase2/phase2.md` gives the current CycloneSEQ Phase 2 design.
8. `phases/phase2/01_*.md` through `07_*.md` give detailed Phase 2 handoffs.
9. `phases/phase_completion_plan.md` remains the long-range roadmap.

## Document Roles

| File | Purpose |
|---|---|
| `handoff.md` | Current status, next task, verification commands, known risks |
| `governance.md` | Coordinator responsibilities, source-of-truth order, boundary rules |
| `architecture.md` | Parent graph, modules, state categories, extension rule |
| `stage_contracts.md` | Stable inputs, outputs, planning rules, and review triggers per stage |
| `descriptor_spec.md` | Descriptor registry, package descriptor, method descriptor, asset rules |
| `phases/phase1/phase1.md` | Current Phase 1 critique, summary, and task order |
| `phases/phase1/task*.md` | Focused executable task handoffs |
| `phases/phase2/phase2.md` | CycloneSEQ full-flow contract and first SeqKit vertical slice |
| `phases/phase2/01_*.md` through `07_*.md` | Phase 2 named handoffs with human-help triggers |
| `phases/phase_completion_plan.md` | Phase-by-phase implementation roadmap |

## Update Rule

When code behavior changes, update the closest matching document in the same
work session. If the next agent needs different instructions, update
`handoff.md` before stopping.
