# 08 Repeat Annotation

Task status: implemented.

## Goal

Place repeat annotation before gene annotation and produce the repeat library,
repeat GFF3, and softmasked genome needed by downstream gene prediction.

## Files

- Modify: `app/stages/repeat_annotation/nodes.py`
- Modify: `app/stages/repeat_annotation/builder.py`
- Modify: `app/stages/repeat_annotation/routing.py`
- Modify: `app/schemas/artifacts.py`
- Modify: `app/schemas/assets.py`
- Add: `descriptors/methods/repeat_annotation/`
- Add: `descriptors/packages/{edta,repeatmodeler,repeatmasker,trf}.yaml`
- Modify: `descriptors/registry.yaml`
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

- [x] Add repeat annotation plan schema.
- [x] Add repeat annotation artifact bundle schema.
- [x] Add descriptor placeholders for EDTA, RepeatModeler, RepeatMasker, and TRF.
- [x] Add routing that requires repeat annotation before gene annotation.
- [x] Add tests that provided repeat library can skip de novo discovery.
- [x] Add tests that fragmented assemblies mark repeat annotation
  low-confidence instead of hiding the issue.

## Implemented State

- `app/stages/repeat_annotation/builder.py` now builds a real planning subgraph
  instead of the generic placeholder.
- `app/stages/repeat_annotation/nodes.py` resolves
  `final_assembly_for_annotation` from Hi-C scaffolding output first, then from
  assembly review fallback refs.
- Optional registered `repeat_library` assets are treated as provided repeat
  libraries and skip de novo library discovery.
- When no provided library exists, EDTA and RepeatModeler are recorded as de novo
  library candidates, TRF is recorded as an optional helper, and method selection
  remains deferred instead of defaulting to one tool.
- RepeatMasker masking is planned for `repeat_annotation.gff3`,
  `softmasked_genome.fasta`, and optional `hardmasked_genome.fasta`; no repeat
  tools are executed in Phase 2.
- `RepeatAnnotationPlan` and `RepeatAnnotationBundle` in
  `app/schemas/artifacts.py` define the plan and downstream bundle contracts.
- `app/schemas/assets.py` includes `repeat_library` so trusted/provided repeat
  libraries can be registered in the asset registry.
- Descriptor placeholders exist for `edta`, `repeatmodeler`, `repeatmasker`, and
  `trf`, with method entries under `descriptors/methods/repeat_annotation/`.
- Explicit upstream low-confidence or fragmented-assembly signals are propagated
  into repeat annotation confidence without inventing numeric fragmentation
  thresholds.
- `tests/test_repeat_annotation.py` covers descriptor loading, missing assembly
  failure, provided-library planning, de novo candidate planning, low-confidence
  propagation, and routing to `gene_annotation`.

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
