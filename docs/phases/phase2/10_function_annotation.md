# 10 Function Annotation

Task status: implemented.

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
- database source/ref;
- command or descriptor version;
- input `proteins.fa` artifact ref.

Database provenance is required. Do not record only “used UniProt/KEGG/Pfam”
without a version or snapshot reference.

## Tasks

- [x] Add function annotation plan schema.
- [x] Add function annotation artifact bundle schema.
- [x] Add descriptor placeholders for InterProScan, eggNOG-mapper, DIAMOND, and
  KofamScan.
- [x] Add tests that `proteins.fa` is required.
- [x] Add tests that database provenance is required in artifact metadata.
- [x] Add routing that marks workflow complete after function annotation.

## Implemented State

- `app/stages/function_annotation/builder.py` now builds a terminal planning
  subgraph instead of the generic placeholder.
- `app/stages/function_annotation/nodes.py` resolves `proteins.fa` from
  `gene_annotation.proteins_fasta_ref`, `predicted_proteins_ref`, or
  `predicted_proteins`, and keeps optional `genes.gff3` / `transcripts.fa` refs.
- Candidate planning records InterProScan, eggNOG-mapper, DIAMOND, and KofamScan
  without selecting a default primary method or running any tool.
- `validate_database_provenance` requires pre-seeded
  `stage_outputs["function_annotation"]["database_provenance"]`; missing,
  incomplete, or protein-ref-mismatched provenance fails terminally instead of
  becoming best-effort metadata.
- `FunctionDatabaseProvenance`, `FunctionAnnotationPlan`, and
  `FunctionAnnotationBundle` are defined in `app/schemas/artifacts.py`; Phase 5
  later tightens provenance with `database_source_ref` and materialized protein
  FASTA validation.
- The stage writes `function_annotation_plan.json` and
  `function_annotation_bundle.json`, records `execution_enabled: false` and
  `database_download_enabled: false`, and publishes planned function table refs.
- A successful commit appends `function_annotation_stage`, sets
  `current_stage: done`, and marks `supervision_state: completed`.
- Descriptor placeholders exist for `interproscan`, `eggnog_mapper`, `diamond`,
  and `kofamscan`, with method entries under
  `descriptors/methods/function_annotation/`.
- `tests/test_function_annotation.py` covers descriptor loading, missing protein
  input, missing/incomplete provenance, protein-ref mismatch, alias resolution,
  planned outputs, artifact writes, and terminal workflow completion.

改动要点：

- `app/stages/function_annotation/builder.py` 将 `function_annotation` 从 placeholder
  换成 `resolve -> plan -> provenance -> commit` 的真实终端 StateGraph。
- `app/stages/function_annotation/nodes.py` 实现 protein 输入解析、候选方法规划、
  database provenance 硬校验、plan/bundle artifact 写入和路由到 `done`。
- `FunctionDatabaseProvenance`、`FunctionAnnotationPlan`、`FunctionAnnotationBundle`
  在 `app/schemas/artifacts.py` 中定义，明确 database name/version/snapshot、
  database source/ref、input protein ref 以及 command 或 descriptor version 要求。
- InterProScan、eggNOG-mapper、DIAMOND、KofamScan descriptors 注册为 planning
  candidates；Phase 2 不下载数据库、不执行工具、不选择默认 function annotation 路径。
- `tests/test_function_annotation.py` 是上手入口：看 proteins.fa 必需、provenance 必需、
  provenance mismatch 失败、有效 provenance 后 workflow 完成四组测试。

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
