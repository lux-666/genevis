# 09 Gene Annotation

Task status: implemented.

## Goal

Define gene annotation on top of `softmasked_genome` with optional RNA-seq,
Iso-Seq, long RNA, and protein evidence.

## Files

- Modify: `app/stages/gene_annotation/builder.py`
- Modify: `app/stages/gene_annotation/nodes.py`
- Modify: `app/stages/gene_annotation/routing.py`
- Modify: `app/schemas/artifacts.py`
- Modify: `app/schemas/assets.py`
- Add: `descriptors/methods/gene_annotation/`
- Add: `descriptors/packages/{braker3,maker,augustus,miniprot,hisat2,stringtie,isoquant,gemoma}.yaml`
- Modify: `descriptors/registry.yaml`
- Test: `tests/test_gene_annotation.py`

## Project Context

`gene_annotation` should consume the `softmasked_genome` produced by
`repeat_annotation`. The current placeholder does not enforce this. This task
should make annotation evidence explicit without running a large annotation
pipeline yet.

## Node Plan

Replace the placeholder with:

```text
enter_gene_annotation
-> resolve_gene_annotation_inputs
-> select_evidence_profile
-> plan_gene_methods
-> plan_evidence_merge_or_review
-> commit_gene_annotation
```

Suggested nodes:

- `resolve_gene_annotation_inputs`: require `softmasked_genome` unless an
  explicit unmasked fallback reason exists.
- `select_evidence_profile`: no evidence, RNA-seq, Iso-Seq/long RNA, protein,
  or mixed evidence.
- `plan_gene_methods`: BRAKER3, MAKER, AUGUSTUS, Miniprot candidates based on
  evidence.
- `plan_evidence_merge_or_review`: block or warn when evidence conflicts and no
  merge policy exists.
- `commit_gene_annotation`: publish `genes.gff3`, `transcripts.fa`, and
  `proteins.fa` refs.

## Reuse From `rna_agent`

Reusable patterns:

- input resolution priority with structured failure when missing;
- provided evidence skip path versus generated evidence path;
- human gate for semantic/evidence conflicts.

Do not copy:

- `celltype_key`;
- marker label-map JSON;
- h5ad mutation behavior.

## Optimization Notes

- Keep evidence generation and evidence merge as separate plan units.
- Make no-evidence ab initio planning explicit, not an implicit fallback.
- Do not let missing RNA/protein evidence silently change stage order.

## Input Contract

Required:

- `softmasked_genome`

Fallback:

- `final_assembly_for_annotation` only with an explicit unmasked fallback reason

Optional evidence:

- RNA-seq
- Iso-Seq
- long RNA
- reference protein evidence
- lineage/species hints

## Candidate Methods

- BRAKER3
- MAKER
- AUGUSTUS
- Miniprot

Optional evidence helpers:

- HISAT2
- StringTie
- IsoQuant
- GeMoMa

## Outputs

- `genes.gff3`
- `transcripts.fa`
- `proteins.fa`
- annotation evidence report

## Tasks

- [x] Add gene annotation plan schema.
- [x] Add gene annotation artifact bundle schema.
- [x] Add descriptor placeholders for BRAKER3, MAKER, AUGUSTUS, and Miniprot.
- [x] Add optional evidence helper descriptors as backlog.
- [x] Add routing that consumes `softmasked_genome`.
- [x] Add tests for no-evidence ab initio planning.
- [x] Add tests for RNA/protein evidence changing method candidate planning.
- [x] Add review trigger when evidence sources conflict.

## Implemented State

- `app/stages/gene_annotation/builder.py` now builds a real planning subgraph
  instead of the generic placeholder.
- `app/stages/gene_annotation/nodes.py` consumes
  `repeat_annotation.softmasked_genome_ref` or `softmasked_genome` by default;
  unmasked `final_assembly_for_annotation` fallback is accepted only when an
  explicit fallback reason is already recorded.
- Optional evidence is resolved from the asset registry as RNA-seq,
  Iso-Seq/long-RNA, and `reference_proteins` evidence.
- Evidence profile is recorded as `no_evidence`, `rnaseq`, `long_rna`,
  `protein`, or `mixed_evidence` without changing stage order.
- Candidate planning records BRAKER3, MAKER, AUGUSTUS, and Miniprot where
  compatible, but does not choose a default primary method.
- Helper planning records HISAT2/StringTie for RNA-seq, IsoQuant for long RNA,
  and GeMoMa for reference protein evidence.
- Mixed evidence and RNA/protein evidence combinations record review triggers
  instead of inventing an evidence merge policy.
- `GeneAnnotationPlan` and `GeneAnnotationBundle` in `app/schemas/artifacts.py`
  define the plan and downstream function-annotation bundle contracts.
- `app/schemas/assets.py` includes `reference_proteins` for optional protein
  evidence registration.
- Descriptor placeholders exist for `braker3`, `maker`, `augustus`, `miniprot`,
  `hisat2`, `stringtie`, `isoquant`, and `gemoma`, with method entries under
  `descriptors/methods/gene_annotation/`.
- The stage publishes planned `genes.gff3`, `transcripts.fa`, `proteins.fa`, and
  `evidence_report.json` refs, including `predicted_proteins_ref` and
  `proteins_fasta_ref` for `function_annotation`.
- `tests/test_gene_annotation.py` covers descriptor loading, missing softmasked
  genome failure, no-evidence planning, RNA/long-RNA/protein evidence planning,
  mixed-evidence review triggers, explicit unmasked fallback, and routing to
  `function_annotation`.

## Human Decisions Required

Stop and ask before choosing:

- final evidence merge policy;
- default method for evidence-rich versus evidence-poor projects;
- species/lineage training assumptions;
- whether unmasked fallback is acceptable.

## Non-Goals

- Do not run gene annotation tools in Phase 2 unless a later method-specific task
  scopes one path.
- Do not assume RNA/protein evidence exists.
- Do not merge conflicting evidence automatically.

## Acceptance Criteria

- Gene annotation consumes `softmasked_genome` by default.
- Outputs are `genes.gff3`, `transcripts.fa`, and `proteins.fa`.
- Evidence availability affects candidate planning without changing stage order.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_gene_annotation tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
