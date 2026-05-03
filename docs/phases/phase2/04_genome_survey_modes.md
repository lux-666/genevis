# 04 Genome Survey Full And Fallback Modes

Task status: implemented.

## Goal

Add a real `genome_survey` contract after read QC with two modes:

- full mode with low-error short DNA reads;
- low-confidence fallback mode with CycloneSEQ long reads only.

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
with a deterministic planner/contract stage, not a full Jellyfish execution
stage.

The project already documents that `genome_survey` is skippable. Existing
assembly inputs and long-read-only CycloneSEQ projects must not be blocked here.

## Node Plan

Replace the placeholder with:

```text
enter_genome_survey
-> resolve_survey_inputs
-> select_survey_mode
-> plan_full_kmer_survey or plan_long_read_fallback or write_survey_skip
-> commit_genome_survey
```

Suggested nodes:

- `resolve_survey_inputs`: read asset registry refs for short DNA reads and
  long-read stats/FOFN refs.
- `select_survey_mode`: choose `full`, `fallback_low_confidence`, or `skipped`.
- `plan_full_kmer_survey`: descriptor-backed plan for Jellyfish/Meryl and
  GenomeScope2/Smudgeplot.
- `plan_long_read_fallback`: compact coverage/genome-size estimate with low
  confidence.
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

- Store confidence explicitly: `high` only for valid short-read k-mer survey,
  `low` for CycloneSEQ-only fallback.
- Make assembly consume survey as a prior; do not make it a hard prerequisite.
- Keep abnormal spectrum/model-fit review for a later real survey execution
  task.

## Full Mode Contract

Condition:

- `clean_short_dna_reads` exists

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

## Fallback Mode Contract

Condition:

- only CycloneSEQ `raw_long_reads` / `clean_long_reads` exists

Allowed behavior:

- estimate coverage from total bases and human/user genome-size prior when
  available;
- produce rough genome size note only if input data supports it;
- set confidence low.

Required guardrail:

- Do not pretend long-read-only fallback is equivalent to Jellyfish/Meryl plus
  GenomeScope2/Smudgeplot.

## Tasks

- [x] Add route decision for full mode, fallback mode, and skipped mode.
- [x] Add `genome_survey_result` artifact schema.
- [x] Add skip reason `no_low_error_short_reads`.
- [x] Add low-confidence fallback summary for CycloneSEQ-only projects.
- [x] Add descriptor placeholders for Jellyfish, Meryl, GenomeScope2, and
  Smudgeplot.
- [x] Add tests for full mode with short reads.
- [x] Add tests for fallback mode with only long reads.
- [x] Add tests that long-read-only mode cannot be marked high-confidence k-mer
  survey.

## Implemented State

- `app/stages/genome_survey/builder.py` now builds a real deterministic
  subgraph instead of the generic placeholder.
- `app/stages/genome_survey/nodes.py` resolves asset registry inputs, chooses
  `full`, `fallback_low_confidence`, or `skipped`, writes a compact
  `genome_survey_result.json`, appends artifact metadata, and advances to
  `assembly`.
- `app/schemas/artifacts.py` defines `GenomeSurveyResult` for compact survey
  contract artifacts.
- `descriptors/packages/` and `descriptors/methods/genome_survey/` now include
  placeholders for Jellyfish, Meryl, GenomeScope2, and Smudgeplot. These are
  planning descriptors, not executable method implementations.
- Full mode requires `clean_short_dna_reads` and is marked `confidence: high`.
- CycloneSEQ long-read fallback is marked `confidence: low`, may compute rough
  coverage only when total bases and a genome-size prior are available, and
  carries the guardrail note `not_equivalent_to_kmer_survey`.
- Skipped mode writes `skip_reason: no_low_error_short_reads` so downstream
  stages can inspect the reason without blocking assembly.
- `tests/test_genome_survey.py` covers descriptor loading, missing registry,
  full mode, low-confidence fallback, and skip behavior.

## Non-Goals

- Do not run Jellyfish, Meryl, GenomeScope2, or Smudgeplot in this task unless a
  later method-specific task says so.
- Do not require genome survey before assembly.
- Do not infer precise genome size from noisy long reads without human-approved
  policy.

## Acceptance Criteria

- `genome_survey` can express full, fallback, and skipped states.
- Full mode depends on short DNA reads.
- Fallback mode is explicitly low-confidence.
- Assembly can later consume `genome_survey_result` as a prior, not a hard
  dependency.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_genome_survey tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
