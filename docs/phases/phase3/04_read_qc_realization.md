# 04 Read QC Realization

Task status: implemented.

## Goal

Make `read_qc` the first fully usable production stage after inventory: stats,
stable FOFN handoff, optional QC report, and optional human-approved filtering.

This task should turn the current read QC scaffold into a dependable stage
without changing downstream workflow decisions.

## Scope

Split read QC into three method layers:

```text
read_qc_stats            automatic
length_filter_plan       automatic plan / skip record
length_filter_execution  only with human threshold or explicit config
```

Default real behavior:

- run or preserve `read_qc.long_read_stats.seqkit_stats`;
- generate `raw_reads_fofn` / `long_reads.fofn` as the stable assembly handoff;
- keep graph state limited to compact summaries and artifact refs.

Optional behavior:

- add long-read QC report routes such as NanoPlot or LongQC;
- plan length filtering and write a skip reason when thresholds are missing;
- execute exactly one approved long-read filter route when the user or review
  policy provides thresholds.

Candidate methods:

- `seqkit_stats`
- `nanoplot_report`
- `longqc_report`
- `length_filter_plan`
- `chopper_filter`
- `filtlong_filter`

Descriptor naming should follow the current registry style unless a separate
descriptor migration is approved. Do not rename existing working descriptors as
part of this task unless the migration is explicitly scoped.

## Boundaries

- Do not chain `chopper`, `fastplong`, `filtlong`, and `seqkit_seq`.
- Do not invent length, quality, keep-percent, or target-bases thresholds.
- Do not make filtering mandatory before assembly.
- Do not block the whole workflow just because filtering thresholds are absent;
  write a skip reason instead.
- Trigger a `length_filter_threshold` human gate only when the user explicitly
  requests filtering but the required threshold/config is missing or ambiguous.
- Do not start assembly in this task.
- Do not introduce `RemoteRunner`.

## Expected Outputs

- `read_stats`
- optional `long_read_qc_report`
- optional `clean_long_reads`
- `long_reads.fofn` or `filtered_long_reads.fofn`

## Tasks

- [x] Preserve `read_qc.long_read_stats.seqkit_stats` as the real default stats route.
- [x] Generate `long_reads.fofn` as the stable raw-read assembly handoff when filtering is skipped.
- [x] Execute `read_qc.length_filter.seqkit_seq` only with explicit approval and a positive `min_len` threshold.
- [x] Generate `filtered_long_reads.fofn` only after approved filtering succeeds.
- [x] Keep graph state limited to compact summaries and artifact refs after read QC commits.
- [x] Keep NanoPlot/LongQC and other report routes deferred; no new report descriptor or execution path is added in this task.

## Implemented State

- `app/stages/read_qc/nodes.py` keeps the read QC mainline as SeqKit stats followed by length-filter planning and deterministic FOFN writing.
- Filtering remains optional and single-route: only `read_qc.length_filter.seqkit_seq` runs, and only when `length_filter_request` includes approval plus a positive `min_len` threshold.
- Missing or unapproved filter thresholds produce a length-filter skip record and still write `long_reads.fofn`, so downstream assembly uses the FOFN ref rather than raw input FASTQ paths.
- Approved filtering writes `filtered_long_reads.fastq`, keeps it as a clean-reads artifact ref, then writes `filtered_long_reads.fofn` as the assembly input.
- `commit_read_qc` now explicitly constructs the committed payload from approved compact fields, excluding raw input paths, frozen attempts, execution results, env overrides, transient output paths, and filter request payloads.
- `tests/test_read_qc.py` covers fake-SeqKit stats execution, raw FOFN fallback, approved filtering, unapproved filtering skip behavior, artifact registration, and compact committed state.

改动要点：

- `app/stages/read_qc/nodes.py` 的 `commit_read_qc` 在进入 `genome_survey` 前显式构造 committed payload；`assembly_input_ref` 只指向 FOFN，approved filtering 可以保留 `filtered_long_reads_ref` 作为 clean reads artifact ref。
- `tests/test_read_qc.py` 增加断言，确保最终 graph state 不保留 `raw_long_reads_path`、`frozen_attempt`、`execution_result`、临时 stats 输出路径或 `length_filter_request`。
- 本任务没有新增 NanoPlot/LongQC descriptor、没有新增过滤阈值默认值、没有启用 RemoteRunner，也没有改变下游 stage 决策。

## Acceptance

- A normal ONT/CLR manifest can leave read QC with stats and a FOFN.
- Filtering happens only when a structured approval exists.
- Missing filter thresholds produce an auditable skip or review request, not an
  invented default.
- Downstream assembly consumes `assembly_input_ref` as a FOFN; approved filtering
  may still leave a clean-reads artifact ref for provenance.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_assembly tests.test_project_scaffold -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
