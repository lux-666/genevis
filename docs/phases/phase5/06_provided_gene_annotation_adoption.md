# 06 Provided Gene Annotation Adoption

Task status: planned.

## Goal

Let users enter the downstream backbone with real gene annotation outputs they
already have, instead of forcing `gene_annotation` to remain planning-only.

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
- Preserve the existing planning-only behavior when no provided annotation
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

- [ ] Define manifest asset aliases and validation rules for provided gene
  annotation files.
- [ ] Add or update examples for existing assembly + provided annotation entry.
- [ ] Implement a `gene_annotation` adoption branch that writes a compact bundle
  and artifact refs.
- [ ] Keep planning-only behavior unchanged when provided annotation assets are
  absent.
- [ ] Pass adopted proteins to `function_annotation`.
- [ ] Add tests for successful adoption, missing protein ref, invalid/missing
  files, and downstream provenance gate.
- [ ] Add a server/HPC smoke that reaches the function provenance gate through
  adopted gene annotation assets.

## Acceptance

- A user with an existing annotation can use the workflow without rerunning gene
  prediction.
- Adopted annotation outputs are real registered artifacts, not planned paths.
- `function_annotation` receives a real `predicted_proteins` ref.
- The workflow still refuses terminal completion without database provenance.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_gene_annotation tests.test_function_annotation tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
