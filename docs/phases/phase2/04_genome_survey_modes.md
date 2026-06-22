# 04 Genome Survey K-mer Survey Modes

Task status: implemented.

## Goal

Add a real `genome_survey` contract after read QC with one executable survey
path over available DNA reads:

- Jellyfish + GenomeScope2 survey over short DNA reads when available;
- otherwise Jellyfish + GenomeScope2 survey over long DNA reads when available.

## Files

- Modify: `app/stages/genome_survey/nodes.py`
- Modify: `app/stages/genome_survey/routing.py`
- Modify: `app/schemas/artifacts.py`
- Add: `descriptors/packages/{jellyfish,meryl,genomescope2,smudgeplot}.yaml`
- Add: `descriptors/methods/genome_survey/`
- Modify: `descriptors/registry.yaml`
- Test: `tests/test_genome_survey.py`
- Keep baseline: `tests/test_project_scaffold.py`

## Project Context

`genome_survey` is currently a placeholder stage. This task should replace it
with a deterministic planner/contract stage and later executable Jellyfish +
GenomeScope2 method path.

The project already documents that `genome_survey` is skippable only when no
DNA-read survey input exists. Long-read-only ONT/CLR/CycloneSEQ projects still
run the survey path.

## Node Plan

Replace the placeholder with:

```text
enter_genome_survey
-> resolve_survey_inputs
-> select_survey_mode
-> plan_full_kmer_survey or write_survey_skip
-> commit_genome_survey
```

Suggested nodes:

- `resolve_survey_inputs`: read asset registry refs for raw/clean short DNA
  reads and raw/clean long DNA reads.
- `select_survey_mode`: choose `full` survey for available short or long DNA
  reads, or explicit skip/failure when no DNA reads are available.
- `plan_full_kmer_survey`: descriptor-backed plan for Jellyfish/Meryl and
  GenomeScope2/Smudgeplot.
- `write_survey_skip`: explicit skip record for no usable inputs.

## Reuse From `rna_agent`

Reusable patterns:

- deterministic route selection by available artifacts;
- skip records that downstream stages can inspect;
- compact bundle/artifact metadata, not large report tables in state.

Do not copy:

- h5ad structure summaries;
- benchmark-ready materialization logic.

## Optimization Notes

- Store the survey input family explicitly so downstream consumers know whether
  the run used short or long DNA reads.
- Make assembly consume survey as a prior; if neither user nor Survey provides
  genome size, stop before assembly with `GENOME_SIZE_UNAVAILABLE`.
- Keep abnormal spectrum/model-fit review for a later real survey execution
  task.

## Full Mode Contract

Condition:

- `raw_short_dna_reads`, `clean_short_dna_reads`, `raw_long_reads`, or
  `clean_long_reads` exists

Candidates:

- Jellyfish
- Meryl
- GenomeScope2
- Smudgeplot

Outputs:

- `genome_survey_result`
- k-mer histogram
- genome size estimate
- heterozygosity estimate
- repeat fraction estimate
- confidence label

## Tasks

- [x] Add route decision for full survey mode and skipped/failed mode.
- [x] Add `genome_survey_result` artifact schema.
- [x] Add stable failure/skip handling when no DNA reads exist.
- [x] Add long-read survey support for ONT/CLR/CycloneSEQ-only projects.
- [x] Add descriptor placeholders for Jellyfish, Meryl, GenomeScope2, and
  Smudgeplot.
- [x] Add tests for survey mode with short reads.
- [x] Add tests for survey mode with only long reads.
- [x] Add tests that user-provided genome size remains the assembly prior even
  when survey also produces a genome-size estimate.

## Implemented State

- `app/stages/genome_survey/builder.py` now builds a real deterministic
  subgraph instead of the generic placeholder.
- `app/stages/genome_survey/nodes.py` resolves asset registry inputs, chooses
  `full` survey or explicit skip/failure, writes a compact
  `genome_survey_result.json`, appends artifact metadata, and advances to
  `assembly` only when a user or survey genome-size prior exists.
- `app/schemas/artifacts.py` defines `GenomeSurveyResult` for compact survey
  contract artifacts.
- `descriptors/packages/` and `descriptors/methods/genome_survey/` now include
  placeholders for Jellyfish, Meryl, GenomeScope2, and Smudgeplot. These are
  planning descriptors, not executable method implementations.
- Full survey mode runs when short or long DNA reads are available.
- If the operator supplied genome size, assembly uses that value; otherwise it
  uses the survey-derived genome size.
- Skipped mode is reserved for explicit user genome size with no DNA-read survey
  input. Failure mode covers no usable DNA reads without explicit genome size, or
  survey execution that cannot produce genome size without explicit genome size.
- `tests/test_genome_survey.py` covers descriptor loading, missing registry,
  short-read survey, long-read survey, explicit genome-size precedence,
  estimate-less survey failure, and no-usable-input behavior.

改动要点：

- `app/stages/genome_survey/builder.py` 将 `genome_survey` 从 placeholder 换成
  full survey / skip-fail 规划子图。
- `app/stages/genome_survey/nodes.py` 解析 asset registry 中的 short DNA reads、long
  reads，选择 Jellyfish + GenomeScope2 survey 或显式 skip/fail。
- `app/schemas/artifacts.py` 的 `GenomeSurveyResult` 是下游 assembly 读取的 compact
  survey contract；图状态只保存 ref、mode、confidence、summary。
- `descriptors/packages/{jellyfish,meryl,genomescope2,smudgeplot}.yaml` 和
  `descriptors/methods/genome_survey/` 只注册规划候选，不执行工具。
- `tests/test_genome_survey.py` 覆盖 short-read survey、long-read survey、用户 genome size 优先、无可用输入失败、解析不到 genome size 失败、缺 registry 失败。

## Non-Goals

- Do not run Jellyfish, Meryl, GenomeScope2, or Smudgeplot in this task unless a
  later method-specific task says so.
- Do not start assembly without a user-provided or survey-derived genome size.
- Do not let survey output override an operator-provided genome size.

## Acceptance Criteria

- `genome_survey` can express full survey and skipped/failed states.
- Full mode depends on short or long DNA reads.
- User-provided genome size remains preferred over survey-derived genome size.
- Assembly can later consume `genome_survey_result` as a prior, not a hard
  dependency.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_genome_survey tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
