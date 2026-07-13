# Task 6: Hifiasm Initial Cutover And Same-Run Retry

**Phase**: 11
**Status**: Complete; Task 6A and Task 6B are implemented and regression-verified, with 10 approved hifiasm-family MVP cases
**Depends on**: Tasks 2-4 and the Phase 10 typed parameter-attempt lifecycle; the superseded Task 5 design is not a dependency
**Created**: 2026-07-10
**Updated**: 2026-07-13

## Decision

Task 6 is split into two independent delivery surfaces:

- **Task 6A:** initial-only numeric cutover with a fixed top-five shortlist and
  optional observational evaluation.
- **Task 6B:** same-run parameter retry using immutable attempt history, with
  independent execution and review ownership/budgets.

They share the typed parameter lifecycle and post-validation human approval
gate, but they do not share an evidence-retrieval backend for retry.

## Task 6A — Initial Top-5 Cutover

### Activation And Selection

The complete descriptor setting is:

```yaml
hifiasm_numeric:
  initial_cutover_enabled: true
```

When enabled, hifiasm ranks every eligible initial EvidenceCase with Task 3's
existing unified score (`method_match`, `read_type_match`, and missing-aware
numeric similarity). Score ties retain Task 3's ordering: `evidence_coverage`
descending, `numeric_distance` ascending, the existing clade tie-break, then
`case_id`.

Task 6A then selects the first five. With one to four eligible cases it selects
all; with none it preserves the existing no-case behavior. Task 6A changes only
the shortlist truncation and does not introduce a second combined score.

### Production Evidence Versus Offline Evaluation

The approved paper-derived EvidenceCase corpus is a production input, not an
observational sidecar. When the numeric path is enabled, its eligible cases are
loaded, scored, truncated to the top five, and projected into the Task 4
recommendation prompt. Consequently, case content and ranking can directly
change the recommended parameters. The term *observational* below applies only
to the offline comparison runner and its reports.

The deterministic offline runner may compare the legacy initial selector with
the numeric initial retriever and report Recall@k, nDCG@k, reciprocal rank,
evidence coverage, leakage/holdout observations, and replay hashes. Reports
must declare that they are observational and have no activation effect.

For the Phase 11 MVP, the curation target is **10 approved hifiasm-family initial
cases**. This is a delivery/completeness target for the production EvidenceCase
corpus, not a runtime activation threshold; synthetic evaluation fixtures do
not count toward it.

No offline report, evaluation fixture, label, metric, or operator assessment can
enable/disable the switch, reject a shortlist, or stop recommendation. This
does not make the approved paper corpus inert: its cases are the evidence ranked
and consumed by the production recommendation path. Reaching the 10-case MVP
target is not required to exercise that path, and there is no
10-query, permission, scientific-approval, recorded-recommendation, distance,
coverage, pass/fail, non-inferiority, or default-preservation gate. Evaluation
may be absent or unfavorable without changing forward progress.

### Task 6A Flow

```text
initial query
  -> eligible cases ranked by the existing Task 3 contract
  -> fixed top five / all 1-4 / existing no-case behavior
  -> Task 4 Methods-aware recommendation
  -> automatic schema/catalog/rule/capability/parameter validation
  -> parameter_approval human gate
       approve_parameters -> freeze attempt 0 and execute
       reject_parameters  -> terminate without execution
```

## Task 6B — Same-Run Retry

### Retry Basis

Retry uses only:

1. attempt 0's actual immutable profile, initial evidence/selector output,
   Methods citations, catalog, hard rules, capabilities, policy, and limits;
2. the immediately preceding attempt's frozen parameters and command;
3. that attempt's raw structured execution result or review diagnosis;
4. the remaining budget owned by the failure domain.

It performs no cross-run/cross-sample lookup, paper retrieval, policy reload,
case ranking, or evidence payload construction. Current observations may block
execution for safety, but they may not select or justify a different retry
delta. A policy snapshot mismatch fails closed and requires a new run.

Prompt rehydration inherits attempt 0's sample/assets/read-QC/genome-survey
profile and its L1/L2 catalog projections verbatim. It may add only the
immediately preceding attempt and the current execution/review diagnosis;
profile or catalog drift fails closed before model invocation.

### Explicit Absences

Task 6B has no retry database, shard, corpus, query, retriever, selector backend,
`hifiasm_retry` backend, retry retrieval artifact, runtime candidate
`EvidenceCase`, retention/curation/promotion workflow, failure classifier,
repairability label, or repairability gate.

### Execution-Failure Retry

The assembly execution stage owns execution failure and
`max_execution_retries`:

```text
execution fails and records structured facts
  -> seal the preceding attempt
  -> check execution retry budget
  -> build recommendation from attempt 0 + preceding attempt + execution facts
  -> automatically validate
  -> parameter_approval human gate
       approve -> freeze, append, increment execution counter, execute
       reject  -> terminate without append, increment, or execution
```

No repairability decision precedes this flow. If execution budget remains, the
stage emits the typed request. If exhausted, it terminates with the
execution-specific exhaustion outcome. A recommendation/schema/validation
error may terminate before the human gate.

### Review-Failure Retry

The assembly review stage owns directional review diagnosis and
`max_review_retries`:

```text
execution succeeds, review fails, and review records its diagnosis
  -> seal the preceding attempt
  -> check review retry budget
  -> build recommendation from attempt 0 + preceding attempt + review signal
  -> automatically validate
  -> parameter_approval human gate
       approve -> freeze, append, increment review counter, execute
       reject  -> terminate without append, increment, or execution
```

Review signals remain directional constraints such as under-purged,
over-purged, fragmented, low completeness, unexpected duplication, or assembly
size-ratio direction. They are not combined into a universal quality score.

### Independent Budgets

Execution and review counters/limits are independent. An execution retry
increments only the execution counter; a review retry increments only the
review counter. Pending approval, recommendation/validation errors, rejection,
stale resume, and duplicate decisions consume neither budget. A single run may
use both budgets in sequence.

## Shared Parameter Approval

Initial, execution-retry, and review-retry recommendations use the same gate
after automatic validation:

```yaml
stage: assembly
gate_id: parameter_approval
decisions_allowed: [approve_parameters, reject_parameters]
auto_decidable: false
```

The interrupt binds the exact validated recommendation, hash, proposed attempt,
task, previous attempt for retry, parameter diff, rationale/failure facts, and
evidence/policy refs. Resume must explicitly provide the gate ID and matching
identity/hash metadata. Only approval may freeze, append, increment, or
execute. Rejection terminates with no execution. The generic review/resume
contract is reused; TUI work is explicitly out of scope.

## Acceptance Matrix

| Area | Required result |
|---|---|
| Initial ranking | Preserve Task 3 unified score and its lexicographic tie-breaks exactly |
| Initial shortlist | Fixed first five, all 1-4, existing no-case behavior for 0 |
| MVP production evidence corpus | 10 approved hifiasm-family initial cases; delivery target only, never an activation gate |
| Production evidence | Approved paper cases are ranked and projected into the recommendation prompt; their content can affect the recommendation |
| Offline evaluation | Observational only; no activation or progress effect |
| Retry inputs | Attempt 0 snapshot + preceding attempt + structured failure only |
| Retry retrieval | No database/retriever/backend/query/shard or candidate promotion |
| Execution retry | No repairability gate; execution budget owned by execution stage |
| Review retry | Directional diagnosis and review budget owned by review stage |
| Approval | Every valid actionable initial/retry parameter candidate blocks at `parameter_approval`; `stop` and `expert_review` do not |
| Approval decision | Approve executes; reject terminates without retry budget use |
| Presentation | No TUI implementation in Task 6 |

## Completion Criteria

- [x] Task 6A preserves Task 3 ranking and fixes the production shortlist at five.
- [x] Offline evaluation is deterministic, replayable, and observational only.
- [x] The MVP production EvidenceCase corpus contains 10 approved
  hifiasm-family initial cases.
- [x] Task 6B uses same-run immutable attempt history without retry retrieval.
- [x] Execution and review retry have independent ownership and budgets.
- [x] No repairability gate or runtime-case promotion remains.
- [x] Initial and retry recommendations share the post-validation human gate.
- [x] Focused tests and the full regression suite pass for the final worktree.
