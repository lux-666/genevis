# 07 Hi-C Scaffolding Or Skip

Task status: implemented.

## Goal

Make `hic_scaffolding` conditionally scaffold assemblies when Hi-C reads exist,
and otherwise produce a clean skip record so annotation can continue on the
contig-level genome.

## Files

- Modify: `app/stages/hic_scaffolding/nodes.py`
- Modify: `app/stages/hic_scaffolding/routing.py`
- Modify: `app/schemas/artifacts.py`
- Add descriptors later: `descriptors/methods/hic_scaffolding/`
- Test: `tests/test_hic_scaffolding.py`

## Project Context

`hic_scaffolding` is a fixed top-level stage but must be skippable. The current
placeholder always advances; this task should make skip behavior explicit and
artifact-backed.

The stage consumes the reviewed assembly, not raw inventory outputs.

## Node Plan

Replace the placeholder with:

```text
enter_hic_scaffolding
-> resolve_hic_inputs
-> select_hic_mode
-> plan_hic_scaffold or write_hic_skip
-> commit_hic_scaffolding
```

Suggested nodes:

- `resolve_hic_inputs`: load final assembly ref and optional Hi-C read refs.
- `select_hic_mode`: choose `scaffold`, `skipped_no_hic`, or
  `skipped_not_chromosome_level`.
- `plan_hic_scaffold`: candidate descriptors for YaHS or Juicer + 3D-DNA +
  Juicebox.
- `write_hic_skip`: artifact/skip reason that downstream annotation can consume.
- `commit_hic_scaffolding`: publish `final_assembly_for_annotation`.

## Reuse From `rna_agent`

Reusable patterns:

- skip artifact plus route advance;
- human review gate before accepting manually inspected outputs;
- artifact refs for generated bundles.

Do not copy:

- notebook trace semantics;
- benchmark output materialization.

## Optimization Notes

- Put Hi-C-specific QC here, not in generic read QC.
- Preserve pre-Hi-C assembly as fallback for annotation.
- Avoid defaulting to chromosome-level behavior just because Hi-C reads exist;
  project intent matters.

## Input Contract

Required:

- `final_assembly_for_scaffolding_or_annotation`

Required for execution:

- Hi-C reads registered in inventory
- chromosome-level assembly requested

## Candidate Methods

- YaHS
- Juicer + 3D-DNA + Juicebox

Optional preprocessing candidate:

- HiC-Pro if descriptors require separate Hi-C QC

## Outputs

When Hi-C scaffold planning is possible:

- planned `hic_qc_report`
- planned `chromosome_scaffolded_assembly`
- planned optional `juicebox_review_bundle`

When Hi-C is absent:

- skip record with reason `no_hic_reads`
- `final_assembly_for_annotation` pointing to the contig-level assembly

## Tasks

- [x] Add Hi-C skip record artifact schema.
- [x] Add scaffold bundle schema.
- [x] Add descriptor placeholders for YaHS and Juicer + 3D-DNA + Juicebox.
- [x] Add routing for no Hi-C reads.
- [x] Add routing for Hi-C present but chromosome-level assembly not requested.
- [x] Add tests that annotation continues after Hi-C skip.
- [x] Add tests that pre-Hi-C assembly remains available as fallback.

## Implemented State

- `app/stages/hic_scaffolding/builder.py` now builds a real scaffold/skip
  subgraph instead of the generic placeholder.
- `app/stages/hic_scaffolding/nodes.py` resolves the reviewed assembly from
  `assembly_review.final_assembly_ref` or
  `assembly_review.final_assembly_for_scaffolding_or_annotation`.
- Registered `hic_reads_registered` assets are resolved from the asset registry.
  If none exist, the stage writes `hic_skip_record.json`, records
  `skip_reason: no_hic_reads`, publishes `final_assembly_for_annotation` as the
  pre-Hi-C assembly, and advances to `repeat_annotation`.
- If Hi-C reads exist but `inventory.target_assembly_level` is not
  `chromosome`, the stage writes a skip record with
  `skip_reason: not_chromosome_level_requested` and keeps annotation
  non-blocking on the contig-level assembly.
- If Hi-C reads exist and chromosome-level assembly is requested, the stage
  writes `hic_scaffold_plan.json`, records YaHS and Juicer + 3D-DNA + Juicebox
  candidate methods, preserves the pre-Hi-C assembly as annotation fallback, and
  blocks with `supervision_state: blocked_human`.
- `HiCSkipRecord` and `HiCScaffoldPlan` are defined in
  `app/schemas/artifacts.py`.
- Descriptor placeholders exist for `yahs`, `juicer`, `3ddna`, and `juicebox`,
  with method entries for `hic_scaffolding.candidate.yahs` and
  `hic_scaffolding.candidate.juicer_3ddna_juicebox`.
- `tests/test_hic_scaffolding.py` covers descriptor loading, missing final
  assembly failure, both skip paths, annotation continuation, fallback assembly
  preservation, and the human-blocked scaffold planning path.

## Human Decisions Required

Stop and ask before choosing:

- contact-map accept/reject policy;
- misjoin correction approval rules;
- default path between YaHS and Juicer + 3D-DNA + Juicebox.

## Non-Goals

- Do not run Hi-C scaffolding tools in Phase 2 unless a later method-specific
  task scopes one path.
- Do not block annotation when Hi-C is absent.
- Do not merge Hi-C QC into generic read QC.

## Acceptance Criteria

- Hi-C absent path is explicit and non-blocking.
- Hi-C present path has candidate methods and artifact contracts.
- Annotation can consume contig-level or chromosome-level assembly by ref.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_hic_scaffolding tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
