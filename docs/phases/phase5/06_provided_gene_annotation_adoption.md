# 06 Provided Gene Annotation Adoption

Task status: implemented. Server/HPC smoke pending.

## Goal

Let users enter the downstream backbone with real gene annotation outputs they
already have, instead of forcing `gene_annotation` to remain candidate-only.

The first useful slice is adoption, not execution:

```text
softmasked genome or final assembly
-> provided GFF3 / proteins / transcripts
-> registered gene annotation bundle
-> function_annotation provenance gate
```

## Scope

- Extend manifest and asset handling for user-provided gene annotation assets:
  - `gene_models` / GFF3
  - `predicted_proteins` / proteins FASTA
  - optional `transcripts.fa`
- Add a `gene_annotation` adoption path that validates and registers provided
  files without running BRAKER3, MAKER, AUGUSTUS, Miniprot, HISAT2, StringTie,
  IsoQuant, or GeMoMa.
- Preserve the existing candidate-only behavior when no provided annotation
  assets exist.
- Pass the adopted `predicted_proteins` ref to `function_annotation`.
- Keep any evidence conflicts as review/report material, not automatic merge
  decisions.

## Non-Goals

- Do not run gene prediction tools in this task.
- Do not invent a gene model merge policy.
- Do not infer protein FASTA from GFF3 automatically unless a separate explicit
  extractor task is approved.
- Do not run function annotation tools.
- Do not download databases.

## Expected Outputs

- Manifest examples for existing assembly plus provided gene annotation assets.
- Gene annotation bundle status such as `adopted_existing_annotation`.
- Artifact records for adopted GFF3, proteins FASTA, optional transcripts, and
  adoption summary.
- Downstream `function_annotation` sees the adopted proteins ref and keeps the
  existing database-provenance requirement.
- Tests prove adoption, missing-file rejection, no-fake-output behavior, and
  function handoff.

## Tasks

- [x] Define manifest asset aliases and validation rules for provided gene
  annotation files.
- [x] Add or update examples for existing assembly + provided annotation entry.
- [x] Implement a `gene_annotation` adoption branch that writes a compact bundle
  and artifact refs.
- [x] Keep candidate-only behavior unchanged when provided annotation assets are
  absent.
- [x] Pass adopted proteins to `function_annotation`.
- [x] Add tests for successful adoption, missing protein ref, invalid/missing
  files, and downstream provenance gate.
- [ ] Add a server/HPC smoke that reaches the function provenance gate through
  adopted gene annotation assets.

## Acceptance

- A user with an existing annotation can use the workflow without rerunning gene
  prediction.
- Adopted annotation outputs are real registered artifacts, not planned paths.
- `function_annotation` receives a real `predicted_proteins` ref.
- The workflow still refuses terminal completion without database provenance.

## Implementation Notes

- `AssetType` and `_ACCEPTED_MANIFEST_ASSET_TYPES` extended with `gene_models`,
  `predicted_proteins`, `transcripts_fasta`.
- `_detect_format` and `validate_manifest` handle `.gff3`/`.gff`/`.gtf` and
  enforce format rules: gene_models must be GFF/GFF3/GTF; predicted_proteins and
  transcripts_fasta must be FASTA.
- `inventory/nodes.py:_entry_mode` treats the three annotation asset types as
  supporting assets (same as `repeat_library`), so `assembly_fasta_registered` +
  annotation assets still routes to `existing_assembly` / `assembly_review`.
- `gene_annotation/nodes.py:resolve_gene_annotation_inputs` detects provided
  annotation assets after genome resolution. If both `gene_models` and
  `predicted_proteins` are present it returns `status="provided_annotation_resolved"`
  with real file refs and `candidate_methods=[]`. Partial sets fail with a clear
  message naming the missing required asset.
- `route_after_resolve` short-circuits `provided_annotation_resolved` directly to
  `commit`, bypassing evidence profiling and method planning.
- `_commit_adopted_gene_annotation` registers `gene_annotation.provided_gene_models`,
  `gene_annotation.provided_predicted_proteins`, optionally
  `gene_annotation.provided_transcripts_fasta`, and `gene_annotation.bundle` with
  `status="adopted_existing_annotation"` and `evidence_profile="provided_annotation"`.
- `GeneAnnotationBundle` schema extended: `status` includes
  `"adopted_existing_annotation"`, `evidence_profile` includes
  `"provided_annotation"`, `transcripts_fasta_ref` and `evidence_report_ref` are
  optional.
- Example manifest: `docs/examples/phase5_existing_assembly_provided_annotation_manifest.template.yaml`.
- Smoke script: `scripts/server_phase5_provided_annotation_smoke.sh`.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_gene_annotation tests.test_function_annotation tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
