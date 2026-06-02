# 07-04 Gene Annotation Execution Slices

Task status: planning (subject captured; specifics loose).

## Goal

Give the gene-annotation predictors real runners, **one method at a time**,
mirroring the assembly mainline pattern. Each slice takes a single
currently-`planned_only` method to actually executable.

## Current State (verified)

- `gene_annotation` is entirely planning + adoption today: no `run_*_attempt`,
  no `LocalRunner`, no subprocess. Every commit path sets
  `execution_enabled: False` with note `execution_deferred_to_method_specific_task`
  (`app/stages/gene_annotation/nodes.py`).
- Registry methods, all `planned_only` (`app/entrypoints/ask_planner.py:247-296`):
  `braker3`, `maker`, `augustus`, `miniprot`, `hisat2`, `stringtie`, `isoquant`,
  `gemoma`.
- The runner pattern to copy: `run_flye_attempt`
  (`app/stages/assembly/nodes.py:381`) and `run_yahs_attempt`
  (`app/stages/hic_scaffolding/nodes.py:251`) — freeze a `FrozenAttemptSpec`,
  `LocalRunner().run(...)`, collect artifacts, register, ledger events, conda env
  via `resolve_method_env` / `attach_conda_fields`.

## Slice Strategy

- One method per slice. Recommended first slice: an **ab-initio path** (e.g.
  AUGUSTUS) — the no-evidence case from the stage contract, the simplest input
  surface.
- Each slice: add freeze + runner + collect nodes, wire conda env, flip the
  registry `execution_support` to `executable`, open the node-level
  `execution_enabled` gate for that method, add tests, validate on a small real
  input, then HPC.
- Evidence-bearing methods (Miniprot/GeMoMa with proteins; HISAT2/StringTie/
  IsoQuant with RNA) come after the ab-initio path proves the pattern.

## Scope (per slice)

- Real execution of one gene method producing candidate gene models as durable
  artifacts.
- Feeds the candidate-evidence review (task 03) with a real candidate.

## Non-Goals

- Merging candidates / final gene set (task 05).
- Doing all methods at once. Slices are independently shippable and HPC-verifiable.

## Open Questions

- Which method leads. Ab-initio AUGUSTUS is the proposed default; BRAKER3 is more
  capable but heavier and pulls in evidence handling.
- Conda env definitions for the gene predictors (extend `config/envs.yaml`).
- How candidate gene models are represented for downstream comparison/merge.
