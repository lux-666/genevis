# 05 Assembly Candidate Planning

## Goal

Make `assembly` candidate-driven instead of hardcoding one assembler.

Each selected method should produce a candidate assembly bundle. Candidate
comparison belongs to `assembly_review`.

## Files

- Modify: `app/stages/assembly/nodes.py`
- Modify: `app/stages/assembly/routing.py`
- Modify: `app/schemas/plan.py`
- Modify: `app/schemas/artifacts.py`
- Add descriptors later: `descriptors/methods/assembly/`
- Test: `tests/test_assembly.py`

## Project Context

`assembly` is currently a placeholder stage. This task should make it candidate
aware while still avoiding real assembler execution unless a later method task
scopes one assembler.

Upstream contracts available by this point:

- `raw_long_reads`
- optional `clean_long_reads`
- optional `long_reads.fofn` or `filtered_long_reads.fofn`
- optional `genome_survey_result`

## Node Plan

Replace the placeholder with:

```text
enter_assembly
-> resolve_assembly_inputs
-> build_assembly_candidate_set
-> select_primary_and_fallback
-> plan_candidate_attempts
-> commit_assembly_plan
```

Suggested nodes:

- `resolve_assembly_inputs`: prefer FOFN when present, otherwise read assets by
  ref.
- `build_assembly_candidate_set`: create candidate descriptors for Flye,
  hifiasm, NextDenovo, and Canu when compatible.
- `select_primary_and_fallback`: enforce one primary plus at most one fallback.
- `plan_candidate_attempts`: prepare candidate attempt specs or planned bundles,
  without running tools.
- `commit_assembly_plan`: route candidate refs to `assembly_review`.

## Reuse From `rna_agent`

Reusable patterns:

- StagePlan with `candidate_methods`, selected method, skip/block reason;
- descriptor compatibility validation before runner calls;
- frozen attempt shape when execution is eventually enabled.

Do not copy:

- Harmony family candidate logic;
- single-cell retry policies;
- h5ad artifact names.

## Optimization Notes

- Keep platform compatibility in descriptors, not `assembly/routing.py`.
- Make genome size absence a method compatibility issue, not a global failure.
- Store rejected/unselected candidates as refs or plan entries so review can
  explain why they were not run.

## Input Contract

Required:

- `clean_long_reads` or approved `long_reads.fofn`

Optional:

- `genome_survey_result`
- user-provided genome size
- platform profile decision for CycloneSEQ

## Candidate Set

Candidates:

- Flye
- hifiasm
- NextDenovo
- Canu

Each candidate method must declare:

- required inputs;
- accepted platform/read profile;
- genome size requirement;
- resource defaults;
- expected output FASTA;
- logs and method metadata;
- review signals to pass downstream.

## Planning Rules

- Select one primary method and at most one fallback automatically.
- Use `genome_survey_result` as a prior, not a universal hard dependency.
- If genome size is required but unavailable, either choose a compatible method
  or stop for human input.
- Ask the human before broadening beyond primary plus fallback.

## Tasks

- [ ] Add assembly candidate plan schema.
- [ ] Add candidate artifact bundle schema for `draft_assembly`.
- [ ] Add descriptor placeholders for Flye, hifiasm, NextDenovo, and Canu.
- [ ] Add tests for method selection requiring `clean_long_reads` or FOFN.
- [ ] Add tests for missing genome size handling.
- [ ] Add tests that only one primary plus one fallback can be auto-planned.
- [ ] Route produced candidate assemblies to `assembly_review`.

## Non-Goals

- Do not run assembly tools in Phase 2 unless a later method task explicitly
  scopes one assembler.
- Do not choose a default CycloneSEQ assembler without human approval.
- Do not compare assemblies here.
- Do not purge, polish, or scaffold here.

## Acceptance Criteria

- Assembly stage can represent a candidate set and selected attempt plan.
- Candidate assembly output shape is stable.
- Method selection constraints are testable.
- `assembly_review` receives candidate refs, not embedded FASTA or logs.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_assembly tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
