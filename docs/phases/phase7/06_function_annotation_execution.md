# 07-06 Function Annotation Execution Slices

Task status: planning (subject captured; specifics loose).

## Goal

Give the function-annotation methods real runners, **one method at a time**,
mirroring the assembly mainline pattern, while keeping the existing database
provenance gating.

## Current State (verified)

- `function_annotation` is entirely planning + provenance-gating today: no
  runner, no subprocess. Every commit path sets `execution_enabled: False`
  (`app/stages/function_annotation/nodes.py`).
- `validate_database_provenance` (nodes.py:109) only checks provenance records +
  that `input_proteins_ref` points to a materialized FASTA — no tool runs.
- Registry methods, all `planned_only` (`app/entrypoints/ask_planner.py:297-322`):
  `interproscan`, `eggnog_mapper`, `diamond`, `kofamscan`.
- Runner pattern to copy: same as task 04 (Flye/YaHS freeze→run→collect).

## Slice Strategy

- One method per slice. The four methods are largely independent (each annotates
  the predicted proteins on its own axis), so slices are cleanly separable.
- Each slice: freeze + runner + collect, conda env, flip `execution_support` to
  executable, retain the provenance gate as a precondition, tests, real-input
  validation, then HPC.
- Likely first slice: DIAMOND (fast homology, simplest DB surface) or
  InterProScan (the domain/GO backbone); decide based on DB availability.

## Scope (per slice)

- Real execution of one function-annotation method over the predicted proteins,
  producing durable annotation tables, only after provenance validation passes.

## Non-Goals

- Gene annotation / merge (tasks 04-05).
- Relaxing or removing the database provenance requirement — it stays.

## Open Questions

- Method order, driven by which annotation databases are actually available on
  the target environment and their sizes.
- Conda envs + database staging for each tool (Pfam/InterPro, Swiss-Prot/UniProt/
  NR for DIAMOND, KOfam profiles).
- How provenance for the chosen databases is recorded so the gate passes.
