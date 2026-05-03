# 07 Hi-C Scaffolding Or Skip

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

When Hi-C runs:

- `hic_qc_report`
- `chromosome_scaffolded_assembly`
- optional `juicebox_review_bundle`

When Hi-C is absent:

- skip record with reason `no_hic_reads`
- `final_assembly_for_annotation` pointing to the contig-level assembly

## Tasks

- [ ] Add Hi-C skip record artifact schema.
- [ ] Add scaffold bundle schema.
- [ ] Add descriptor placeholders for YaHS and Juicer + 3D-DNA + Juicebox.
- [ ] Add routing for no Hi-C reads.
- [ ] Add routing for Hi-C present but chromosome-level assembly not requested.
- [ ] Add tests that annotation continues after Hi-C skip.
- [ ] Add tests that pre-Hi-C assembly remains available as fallback.

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
