# Project Status

A living index of what the Gene Agent pipeline can execute today, how execution
works, and what is still on the roadmap. This document links to the detailed
phase docs rather than repeating them.

Last updated 2026-07-01.

## Execution model

The system is a LangGraph pipeline whose capabilities are declared in a
descriptor registry, not hardcoded. `descriptors/registry.yaml` maps three
things: `packages` (tool-level `PackageDescriptor`: version constraint,
language, install channel, resource defaults), `methods` (business-level
`MethodDescriptor`: stage, `package_ref`, required/expected IO, review rules),
and `by_stage` (the ordered list of methods each stage may run). `DescriptorRegistry.load`
validates that every method points at a known package and sits in its declared
stage. See `app/schemas/descriptors.py` and `docs/descriptor_spec.md`.

Each stage runs its selected method through a uniform freeze → run → collect flow:

1. A planning node resolves inputs and parameters for the chosen method.
2. `build_frozen_attempt` (`app/execution/method_runner.py`) writes a shell
   script and freezes it into a `FrozenAttemptSpec` (script path, working dir,
   expected outputs, env / conda fields, metadata) — a deterministic, replayable
   execution unit.
3. `run_frozen_attempt` executes the spec via `LocalRunner`
   (`app/execution/local_runner.py`, dispatching through `conda run` when the
   attempt declares an env), records `attempt_started` / `attempt_failed` ledger
   events, and cleans up partial outputs on failure.
4. A collect node registers artifacts, materializes any candidate bundle, and
   advances stage status, recording `attempt_succeeded` / `artifact_registered`.

Stages share this via the `MethodLifecycle` + `build_method_graph` convention
(`app/stages/assembly/method_application/lifecycle.py`): a method module owns
only how it freezes an attempt and collects its artifacts; the run step and
ledger bookkeeping are common. `flye.py` and `hifiasm.py` are the reference
adapters. The parent graph (`app/graph/builder.py`) is a thin router that
composes the per-stage subgraphs and returns to a controller between stages.

## Invariant: registered = executable

Every method registered in `descriptors/registry.yaml` is executable. The older
"candidate-only vs executable" distinction has been removed: the registry no
longer carries placeholder methods that have no wired runner.

This is enforced by `tests/test_registry_executable_invariant.py`, which asserts
that the set of methods in `registry.methods` equals the set of wired stage
runners. Adding a method to the registry without a runner (or vice versa) fails
that test. Un-built tools live in the Roadmap below, not in the registry.

## Stage & tool matrix

Stages in pipeline order, with the methods registered under each stage's
`by_stage` entry (the authoritative source).

| Stage | Registered (executable) methods | Notes |
|---|---|---|
| `inventory` | — | No registered methods; assembles the run manifest. |
| `read_qc` | `read_qc.long_read_stats.seqkit_stats`, `read_qc.length_filter.seqkit_seq`, `read_qc.make_fofn.local` | Stats always run; length filter can be skipped (planner offers a skip role); FOFN handoff always runs. |
| `genome_survey` | `genome_survey.kmer_count.jellyfish`, `genome_survey.model.genomescope2` | k-mer count feeds the GenomeScope2 model. |
| `assembly` | `assembly.candidate.flye`, `assembly.candidate.hifiasm`, `assembly.candidate.nextdenovo` | Candidate assemblers; route/candidate selection may open a human gate. |
| `assembly_dedup` | `assembly_dedup.purge_dups` | Haplotig / duplicate purging. |
| `assembly_review` | `assembly_review.eval.quast`, `assembly_review.eval.busco` | Evaluation methods feeding a mandatory review gate. |
| `hic_scaffolding` | `hic_scaffolding.candidate.yahs`, `hic_scaffolding.candidate.juicer_3ddna_juicebox` | Hi-C scaffolding candidates; soft-blocks when execution mode is non-local. |
| `t2t` | `t2t.alignment.minimap2` | Telomere-aware alignment / patch planning. |
| `repeat_annotation` | `repeat_annotation.masking.repeatmasker` | Repeat masking. |
| `gene_annotation` | `gene_annotation.candidate.augustus`, `gene_annotation.candidate.miniprot` | Candidate gene predictors. See internal-helper note below. |
| `function_annotation` | `function_annotation.candidate.diamond` | Homology-based functional assignment. |

Gene-annotation internal helpers: `gene_annotation` also runs three executable
helper steps that are intentionally not in `by_stage` because they are not
operator-selectable candidate methods: a gffread materialize step
(`gene_annotation.materialize.gffread`, produces standardized proteins.fa /
cds.fa from a candidate GFF), an EVM merge step (`gene_annotation.merge.evm`, a
post-review action when multiple candidates are kept), and a gene-BUSCO QC step
(`gene_annotation.busco`, completeness scoring on the final gene set).

## Roadmap (planned tools, not yet built)

These tools were previously registered as candidate-only placeholders and have
been removed from the registry. They are future work: adding one means
implementing its runner per the checklist below, then registering it. Until
then they must not appear in `descriptors/registry.yaml`.

| Stage | Planned tools |
|---|---|
| `read_qc` | `chopper`, `filtlong`, `fastplong` |
| `genome_survey` | `meryl`, `smudgeplot`, `merqury` |
| `assembly` | `canu` |
| `assembly_dedup` | `mapping_coverage` |
| `assembly_review` | `merqury`, plus the review helpers `purge_dups_check`, `contamination_check` |
| `repeat_annotation` | `edta`, `repeatmodeler`, `trf` |
| `gene_annotation` | `braker3`, `maker`, `gemoma`, `genewise`, `annevo`; RNA-seq evidence: `hisat2`, `isoquant`, `stringtie` |
| `function_annotation` | `interproscan`, `eggnog_mapper`, `kofamscan` |

Note: `merqury` appears under both genome_survey (read/assembly QV) and
assembly_review depending on where it is wired; register it under whichever
stage owns the runner. `mapping_coverage` and the review checks
(`purge_dups_check`, `contamination_check`) were placeholder review methods.

## How to add a tool (checklist)

Derived from how flye/hifiasm are wired end to end. To make a new tool
executable:

1. Author descriptors: add the method YAML under
   `descriptors/methods/<stage>/`, add (or reuse) the package YAML under
   `descriptors/packages/`, and register all three in
   `descriptors/registry.yaml` (`packages`, `methods`, and the stage's
   `by_stage` list).
2. Implement the runner: for a candidate stage, add a `method_application`
   adapter following the `MethodLifecycle` + freeze/collect pattern (see
   `app/stages/assembly/method_application/hifiasm.py`); otherwise add the
   stage's own freeze/run/collect nodes.
3. Wire it into the stage: update the stage `routing.py`, `builder.py`, and any
   method constants so the graph can dispatch to the new method.
4. Add a run-config parameter model and validation if the method is locally
   executable (parameters, defaults, and required-field checks).
5. Surface it to the planner: add an entry to `_STAGE_METHODS` in
   `app/entrypoints/ask_planner.py` (method name, required parameters,
   parameter defaults, selection role).
6. Add tests, including an execution test that runs the frozen attempt.
7. Satisfy the invariant: `tests/test_registry_executable_invariant.py` requires
   the newly registered method to have a wired runner.

## Current phase / next

The registry has been trimmed to executable-only methods; the candidate-only
mechanism and its ~24 placeholder methods have been removed. hifiasm was just
made executable and now runs through the shared assembly `MethodLifecycle`.

Next: TBD — see the linked phase docs for the active work stream.

## Known issues

Pre-existing test failures unrelated to current feature work:

- The 7 `AssemblyRerunTests` Flye failures are pre-existing and not caused by
  recent changes.
- `related_species_evidence` tests require the NCBI metazoa reference to be
  installed locally (`scripts/install_ncbi_metazoa_reference.py`).
- `test_durable_resume` can fail on macOS due to a `/private/var` vs `/var`
  symlink path discrepancy.

## Phase docs index

Coordination and design references:

- [docs/README.md](README.md) — documentation map / where to start.
- [docs/handoff.md](handoff.md) — current status, next task, verification, risks.
- [docs/governance.md](governance.md) — coordinator responsibilities, source-of-truth order.
- [docs/architecture.md](architecture.md) — parent graph, modules, state categories.
- [docs/stage_contracts.md](stage_contracts.md) — per-stage inputs/outputs/review triggers.
- [docs/descriptor_spec.md](descriptor_spec.md) — descriptor registry and descriptor contracts.
- [docs/beginner_mainline_guide.md](beginner_mainline_guide.md) — mainline walkthrough.
- [docs/deployment/migration.md](deployment/migration.md) — new-host setup checklist.
- [docs/deployment/mac_dev_server_pull.md](deployment/mac_dev_server_pull.md) — mac-dev / server git-pull workflow.

Phase roadmap and per-phase folders:

- [docs/phases/phase_completion_plan.md](phases/phase_completion_plan.md) — long-range roadmap.
- [docs/phases/phase1/phase1.md](phases/phase1/phase1.md)
- [docs/phases/phase2/phase2.md](phases/phase2/phase2.md)
- [docs/phases/phase3/README.md](phases/phase3/README.md)
- [docs/phases/phase4/README.md](phases/phase4/README.md)
- [docs/phases/phase5/README.md](phases/phase5/README.md)
- [docs/phases/phase6/](phases/phase6/) — HPC acceptance, resume CLI, TUI ask/plan preview.
- [docs/phases/phase7/README.md](phases/phase7/README.md) — review-gate generalization through function-annotation execution.
- [docs/phases/phase8/README.md](phases/phase8/README.md) — resource-resolution architecture (Phase 8 & 8.5).
- [docs/phases/phase9/README.md](phases/phase9/README.md) — end-to-end pipeline integration test.
