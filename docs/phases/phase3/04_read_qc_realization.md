# 04 Read QC Realization

Task status: planned.

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

## Acceptance

- A normal ONT/CLR manifest can leave read QC with stats and a FOFN.
- Filtering happens only when a structured approval exists.
- Missing filter thresholds produce an auditable skip or review request, not an
  invented default.
- Downstream assembly consumes a FOFN ref, not raw FASTQ paths embedded in graph
  state.
