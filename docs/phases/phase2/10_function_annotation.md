# 10 Function Annotation

## Goal

Finish the single-line workflow by defining function annotation from
`proteins.fa`.

## Files

- Modify: `app/stages/function_annotation/nodes.py`
- Modify: `app/stages/function_annotation/routing.py`
- Modify: `app/schemas/artifacts.py`
- Add descriptors later: `descriptors/methods/function_annotation/`
- Test: `tests/test_function_annotation.py`

## Project Context

`function_annotation` is the terminal biological stage. The current placeholder
advances to `done`; this task should make completion depend on `proteins.fa` and
function annotation artifact refs.

## Node Plan

Replace the placeholder with:

```text
enter_function_annotation
-> resolve_protein_inputs
-> plan_function_methods
-> validate_database_provenance
-> commit_function_annotation
```

Suggested nodes:

- `resolve_protein_inputs`: require `proteins.fa` artifact ref.
- `plan_function_methods`: InterProScan, eggNOG-mapper, DIAMOND, KofamScan
  candidates.
- `validate_database_provenance`: require database name/version/snapshot before
  a method can be considered executable.
- `commit_function_annotation`: publish function tables and mark workflow done.

## Reuse From `rna_agent`

Reusable patterns:

- terminal stage commit through normal route controller semantics;
- artifact bundle plus ledger facts;
- deterministic preflight before expensive method execution.

Do not copy:

- benchmark metrics;
- single-cell report/notebook semantics.

## Optimization Notes

- Treat database provenance as required, not best-effort metadata.
- Allow a fast DIAMOND/Kofam path before InterProScan only if documented as a
  method policy.
- Do not download databases automatically from the stage node.

## Input Contract

Required:

- `proteins.fa`

Optional:

- `genes.gff3`
- `transcripts.fa`

## Candidate Methods

- InterProScan
- eggNOG-mapper
- DIAMOND
- KofamScan

## Outputs

- `gene_function.tsv`
- GO annotation table
- KEGG annotation table
- Pfam/InterPro table
- optional `annotated_gene_models.gff3`

## Provenance Requirements

Record:

- database name;
- database version;
- snapshot date;
- command or descriptor version;
- input `proteins.fa` artifact ref.

Database provenance is required. Do not record only “used UniProt/KEGG/Pfam”
without a version or snapshot reference.

## Tasks

- [ ] Add function annotation plan schema.
- [ ] Add function annotation artifact bundle schema.
- [ ] Add descriptor placeholders for InterProScan, eggNOG-mapper, DIAMOND, and
  KofamScan.
- [ ] Add tests that `proteins.fa` is required.
- [ ] Add tests that database provenance is required in artifact metadata.
- [ ] Add routing that marks workflow complete after function annotation.

## Non-Goals

- Do not run function annotation tools in Phase 2 unless a later method-specific
  task scopes one path.
- Do not download databases automatically.
- Do not hide missing database provenance.

## Acceptance Criteria

- Function annotation consumes `proteins.fa`.
- Outputs include function table plus GO/KEGG/Pfam/InterPro families.
- Database provenance is part of the artifact contract.
- This stage is the terminal biological stage in the documented backbone.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_function_annotation tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
