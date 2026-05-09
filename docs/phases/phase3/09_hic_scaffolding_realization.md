# 09 Hi-C Scaffolding Realization

Task status: implemented.

## Goal

Run Hi-C scaffolding only when Hi-C reads exist and the project goal is
chromosome-level assembly.

## Scope

Start with the lighter automated path, preferably YaHS, while preserving Juicer
+ 3D-DNA + Juicebox as a heavier/manual route for later.

The existing skip behavior remains valid when Hi-C is absent or chromosome-level
assembly was not requested.

Candidate methods/roles:

- `hic_input_probe`
- `hic_align_to_assembly`
- `yahs_scaffold`
- `juicer_3ddna_scaffold`
- `contact_map_bundle`
- `hic_review_request`

## Boundaries

- Do not make Hi-C scaffolding mandatory for ONT/CLR projects.
- Do not run both YaHS and Juicer/3D-DNA by default.
- Do not skip contact-map/manual review requirements for chromosome-scale
  promotion.
- Do not lose the pre-Hi-C assembly as annotation fallback.
- Do not treat Hi-C data presence alone as permission to produce a chromosome
  assembly; target level must be explicit.

## Expected Outputs

- optional `chromosome_assembly`
- `hic_qc_report`
- optional `hic_review_bundle`
- preserved `final_assembly_for_annotation` fallback

## Tasks

- [x] Keep Hi-C absent or non-chromosome targets non-blocking with explicit skip artifacts.
- [x] Add an explicit local YaHS route only when `stage_outputs.hic_scaffolding.execution_mode == "local"` and the selected/primary method is YaHS.
- [x] Require a materialized pre-Hi-C assembly plus a materialized Hi-C alignment input for local YaHS; raw Hi-C read alignment remains deferred.
- [x] Register YaHS scaffold candidate FASTA, optional AGP, review bundle, command stdout/stderr, and ledger attempt events.
- [x] Preserve the pre-Hi-C assembly as `final_assembly_for_annotation` until human/contact-map review approves promotion.
- [x] Keep Juicer, 3D-DNA, Juicebox, contact-map scoring, thresholds, and RemoteRunner deferred.

## Implemented State

- `app/stages/hic_scaffolding/nodes.py` preserves existing scaffold/skip behavior by default and only freezes a YaHS attempt for explicit local YaHS requests.
- Local YaHS execution requires both `stage_outputs.hic_scaffolding.execution_mode == "local"` and an explicit `primary_method`/`selected_method_name` of `hic_scaffolding.candidate.yahs`; it validates the descriptor, writes deterministic `yahs -o <prefix> <assembly> <hic_alignment>` attempts, runs them through `LocalRunner`, and records ledger attempt events.
- YaHS success registers a chromosome scaffold candidate FASTA, optional AGP, a review bundle, and command stdout/stderr artifacts, then still blocks human review. The scaffold is not promoted automatically.
- YaHS failure registers command stdout/stderr artifacts, records `attempt_failed`, preserves the pre-Hi-C fallback, and blocks for human decision instead of silently advancing a failed scaffold.
- If local YaHS is explicitly requested with raw Hi-C reads but no materialized alignment input, the stage writes the scaffold plan and blocks with `alignment_required_before_yahs`; no local attempt is started. If `execution_mode == "local"` is set without an explicit YaHS method, behavior remains planning-only.
- `descriptors/methods/hic_scaffolding/yahs_candidate.yaml` and `descriptors/packages/yahs.yaml` describe the current alignment-input YaHS execution path. Raw alignment generation remains a later `hic_align_to_assembly` realization.

改动要点：

- `app/stages/hic_scaffolding/builder.py` and `routing.py` insert the optional YaHS freeze/run/collect branch before the existing human block.
- `app/schemas/artifacts.py` defines artifact constants for Hi-C scaffold plan, scaffold candidate FASTA, AGP, review bundle, and QC report.
- The human interrupt includes scaffold candidate and review bundle refs when YaHS has produced them.
- `tests/test_hic_scaffolding.py` covers default planning, fake-YaHS success, fake-YaHS failure, missing alignment behavior, and Juicer planning-only behavior.

## Acceptance

- Hi-C present plus chromosome target can produce scaffold outputs.
- Hi-C absent or non-chromosome targets remain non-blocking.
- Any chromosome-scale promotion is auditable and reviewable.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_hic_scaffolding -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_hic_scaffolding tests.test_review_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_assembly_review tests.test_hic_scaffolding tests.test_review_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
