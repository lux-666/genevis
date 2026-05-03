# 08 Repeat Annotation

## Goal

Place repeat annotation before gene annotation and produce the repeat library,
repeat GFF3, and softmasked genome needed by downstream gene prediction.

## Files

- Modify: `app/stages/repeat_annotation/nodes.py`
- Modify: `app/stages/repeat_annotation/routing.py`
- Modify: `app/schemas/artifacts.py`
- Add descriptors later: `descriptors/methods/repeat_annotation/`
- Test: `tests/test_repeat_annotation.py`

## Project Context

`repeat_annotation` sits before `gene_annotation` in the fixed stage order. This
task should make that dependency explicit by producing a `softmasked_genome`
artifact ref.

The current placeholder should be replaced by a contract/planning subgraph
before any repeat tool execution is attempted.

## Node Plan

Replace the placeholder with:

```text
enter_repeat_annotation
-> resolve_repeat_inputs
-> select_repeat_strategy
-> plan_repeat_library
-> plan_repeat_masking
-> commit_repeat_annotation
```

Suggested nodes:

- `resolve_repeat_inputs`: load `final_assembly_for_annotation` and optional
  provided repeat library.
- `select_repeat_strategy`: provided library versus de novo discovery.
- `plan_repeat_library`: EDTA/RepeatModeler/TRF candidates.
- `plan_repeat_masking`: RepeatMasker output contract.
- `commit_repeat_annotation`: publish `softmasked_genome` and repeat bundle refs.

## Reuse From `rna_agent`

Reusable patterns:

- deterministic stage mode selection from available artifacts;
- artifact bundle record for downstream stage consumption;
- review warning when quality is low but stage can continue.

Do not copy:

- marker-summary or LLM annotation paths.

## Optimization Notes

- Record repeat library source as `provided` or `de_novo`.
- Mark low-confidence repeat annotation when assembly fragmentation is high.
- Keep hardmasked genome optional; gene annotation should prefer softmasked.

## Input Contract

Required:

- `final_assembly_for_annotation`

Optional:

- trusted repeat library

## Candidate Methods

- EDTA
- RepeatModeler
- RepeatMasker

Optional helper:

- TRF for tandem repeats

## Outputs

- `repeat_library`
- `repeat_annotation.gff3`
- `softmasked_genome`
- optional `hardmasked_genome`

## Tasks

- [ ] Add repeat annotation plan schema.
- [ ] Add repeat annotation artifact bundle schema.
- [ ] Add descriptor placeholders for EDTA, RepeatModeler, RepeatMasker, and TRF.
- [ ] Add routing that requires repeat annotation before gene annotation.
- [ ] Add tests that provided repeat library can skip de novo discovery.
- [ ] Add tests that fragmented assemblies mark repeat annotation
  low-confidence instead of hiding the issue.

## Non-Goals

- Do not run repeat annotation tools in Phase 2 unless a later method task scopes
  one path.
- Do not start gene annotation from an unmasked genome unless an explicit
  fallback reason is recorded.
- Do not choose EDTA versus RepeatModeler/RepeatMasker as a default without
  descriptor policy.

## Acceptance Criteria

- Repeat annotation precedes gene annotation.
- `softmasked_genome` is the default gene annotation input.
- Repeat library source is recorded as provided or de novo.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_repeat_annotation tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
