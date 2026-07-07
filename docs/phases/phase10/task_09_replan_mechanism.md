# Task 9: Implement Retry and Replan Mechanism

**Phase**: 10
**Status**: Implemented
**Depends on**: Task 8 and assembly_review
**Created**: 2026-07-02
**Updated**: 2026-07-06

## Goal

Make the parameter loop executable, replayable, able to fail, and able to retry.

There are two retry triggers:

1. command execution failure,
2. assembly_review quality failure.

Both triggers create a new immutable attempt entry with `attempt_id + 1`.

## Execution Failure Retry

Existing method lifecycle:

```text
freeze -> run -> collect
```

On run failure, `run_frozen_attempt()` already records stdout/stderr paths and an error. Add a gate after the method subgraph:

```text
hifiasm_ont
 -> execution_retry_gate
    -> retry: recommend_assembly_parameters
    -> fail: fail_assembly
    -> commit: commit
```

Retry gate behavior:

1. Detect `status: failed` from assembly method execution.
2. Find previous attempt's stdout/stderr paths.
3. Read last 100 lines, preferring stderr, then stdout.
4. Append `failure_log_tail` to the previous attempt.
5. If retry count < max, set retry context:
   - `status: retry_parameters_after_execution_failure`,
   - `current_parameter_attempt_id: previous + 1`,
   - `previous_parameter_attempt`.
6. Route back to `recommend_assembly_parameters`.
7. If retry count exceeded, fail terminal with the recorded error.

All retry paths must write attempt-specific directories and artifacts. Do not
reuse the old method-level paths such as `work/assembly/<session>/hifiasm` or
`artifacts/assembly/<session>/hifiasm` for retryable parameter attempts. Use an
attempt segment, for example:

```text
data/work/assembly/<session>/parameter_attempt_<N>/<method>/
data/artifacts/assembly/<session>/parameter_attempt_<N>/
```

After this task is complete, previous attempts must remain inspectable without
being overwritten by a later retry.

## Assembly Review Failure Retry

After review metrics are available, add a parameter quality gate:

```text
assembly_review.evaluate_rules
 -> parameter_quality_gate
    -> pass: existing comparison/human_gate flow
    -> retry: parameter_replan_candidate
    -> stop: existing human_gate or failed_terminal
```

Quality gate should classify a small set of failure modes:

| Failure mode | Signal |
|---|---|
| `under_purged` | high BUSCO duplication plus inflated assembly size |
| `over_purged` | low BUSCO completeness plus deflated assembly size |
| `fragmented` | low N50 plus high contig count |
| `low_completeness` | low BUSCO completeness with normal size |
| `execution_or_metrics_missing` | no reliable metrics |

For the MVP, thresholds can be conservative and configurable. The gate should produce a diagnosis, not final biological truth.

## Review Retry Context

Retry prompt receives:

```yaml
task: retry_after_review_failure
previous_attempt:
  attempt_id: 0
  recommended_params: {}
  argv: []
review_failure:
  failure_mode: under_purged
  metrics:
    busco_complete: 0.94
    busco_duplicate: 0.21
    n50_mb: 10
    contig_count: 300
    assembly_size_ratio: 1.35
  suggestions:
    purge_level: "consider increasing"
```

The LLM still may output only L2 params.

## Attempt Ledger Rules

- Attempts are append-only.
- Each retry increments `attempt_id`.
- A new attempt may reuse the same selected cases or select new cases, but must record `selected_case_ids`.
- Previous prompt/response refs remain intact.
- The command for every attempt is recorded as argv.
- Attempt directories include attempt id.

## Max Retry Policy

Add config:

```yaml
assembly:
  parameter_recommendation:
    max_execution_retries: 1
    max_review_retries: 2
```

Defaults:

- execution retries: 1,
- review retries: 2.

If exceeded, route to human review or terminal failure with a concise reason and refs to attempt ledger.

## Minimal Implementation Path

1. Implement execution retry inside assembly stage for hifiasm_ont only.
2. Store last 100 log lines in `parameter_attempts`.
3. Add review quality gate as a separate node after metrics evaluation.
4. Let review retry re-enter assembly attempt freeze/run with `attempt_id + 1`.
5. If retry is not possible, route to the existing human/terminal gate, but do
   not keep a second non-attempt retry implementation.

## Tests

- Command failure creates `failure_log_tail`.
- Command failure retry increments attempt id.
- Retry prompt includes previous prompt inputs and log tail.
- Max execution retry stops.
- Review under-purged metrics trigger retry.
- Review retry increments attempt id and records review metrics.
- Max review retry stops without mutating old attempts.

## Acceptance Criteria

- [x] Execution failure can retry with last 100 log lines.
- [x] Review failure can retry with metrics and failure mode.
- [x] All retry attempts are replayable.
- [x] Retry attempts use separate directories.
- [x] Retry caps prevent infinite loops.
- [x] Old method-level retry paths are removed from the parameter loop.
