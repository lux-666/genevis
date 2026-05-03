# 03 Read QC Filter And FOFN Contract

## Goal

Extend `read_qc` planning after stats with two downstream substeps:

```text
long-read stats
-> length_filter
-> make_fofn
```

This task may implement planning and artifact contracts first. Actual filtering
execution can stay disabled until thresholds are human-approved.

## Files

- Modify: `app/stages/read_qc/nodes.py`
- Modify: `app/stages/read_qc/routing.py`
- Modify: `app/schemas/plan.py`
- Modify: `app/schemas/artifacts.py`
- Add descriptors later: `descriptors/methods/read_qc/`
- Test: `tests/test_read_qc.py`

## Project Context

This task builds on the real read QC subgraph introduced by task 02. It should
extend that graph instead of creating a second `read_qc` path.

Current Phase 2 intent:

- SeqKit stats can run.
- Filtering is planned but not automatic.
- FOFN exists to stabilize assembly input, especially for NextDenovo-like
  methods.

## Node Plan

Extend read QC toward:

```text
enter_read_qc
-> resolve_raw_long_reads
-> long_read_stats
-> plan_length_filter
-> maybe_run_length_filter
-> make_fofn
-> commit_read_qc
```

Suggested nodes:

- `plan_length_filter`: decide `planned`, `skipped`, or `blocked_human` based on
  explicit thresholds/user request.
- `maybe_run_length_filter`: execute only after thresholds are explicit.
- `make_fofn`: write a small FOFN artifact that points to raw or filtered reads.
- `commit_read_qc`: publish `long_reads.fofn` or `filtered_long_reads.fofn` ref.

If filtering thresholds are absent, `make_fofn` may still create a FOFN from
`raw_long_reads`.

## Reuse From `rna_agent`

Reusable patterns:

- StagePlan-like fields for planned/skipped/executed method branches;
- explicit skip reasons instead of silent fallthrough;
- artifact refs instead of embedding generated file contents in state.

Do not copy:

- integration family routing;
- single-cell working representation;
- LLM planner behavior.

## Optimization Notes

- Treat filtering as a biological policy decision, not a default cleanup step.
- Keep FOFN writing deterministic and local; it is a tiny artifact and does not
  need a heavy runner.
- Add tests around skip reasons first, before enabling any filtering command.

## Substep Contracts

### `length_filter`

Candidates:

- `seqkit seq`
- Filtlong
- fastplong
- chopper

Input:

- `raw_long_reads` or approved stats artifact ref

Output:

- `filtered_long_reads`
- or skip record when filtering is not requested

Guardrail:

- Do not over-filter long reads before Flye unless the human or a documented
  review policy explicitly requests it.

### `make_fofn`

Input:

- `filtered_long_reads` when filtering ran
- otherwise `raw_long_reads`

Output:

- `filtered_long_reads.fofn` or `long_reads.fofn`

Purpose:

- Provide a stable input contract for assembly tools such as NextDenovo and any
  later stage that prefers FOFN input.

## Tasks

- [ ] Add read QC stage plan fields for `long_read_stats`, `length_filter`, and
  `make_fofn`.
- [ ] Record `length_filter` as planned/skipped/executed without requiring
  thresholds.
- [ ] Add FOFN artifact contract and tests.
- [ ] Add skip reasons for no filtering requested and threshold decision missing.
- [ ] Add candidate descriptor placeholders for SeqKit seq, Filtlong, fastplong,
  and chopper.
- [ ] Add tests proving assembly can later consume a FOFN ref without reading
  FASTQ paths directly from graph state.

## Non-Goals

- Do not choose default length, quality, keep-percent, or target-bases
  thresholds.
- Do not run filtering by default.
- Do not make Flye depend on filtered reads.
- Do not run assembly in this task.

## Acceptance Criteria

- Read QC has three documented substeps in state/plan contracts.
- FOFN output is a named artifact family.
- Filtering remains gated by human-approved thresholds or explicit user request.
- Later assembly tasks can reference `long_reads.fofn` or
  `filtered_long_reads.fofn`.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
