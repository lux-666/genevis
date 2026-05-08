# 09 Hi-C Scaffolding Realization

Task status: planned.

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

## Acceptance

- Hi-C present plus chromosome target can produce scaffold outputs.
- Hi-C absent or non-chromosome targets remain non-blocking.
- Any chromosome-scale promotion is auditable and reviewable.
