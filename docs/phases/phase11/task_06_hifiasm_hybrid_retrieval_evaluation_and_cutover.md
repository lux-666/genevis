# Task 6: Hifiasm Initial Cutover And Same-Run Retry

**Phase**: 11
**Status**: Task 6A and Task 6B are implemented in the current worktree; final focused and regression verification pending
**Depends on**: Tasks 2-4 and the Phase 10 typed parameter-attempt lifecycle; the superseded Task 5 design is not a dependency
**Created**: 2026-07-10
**Updated**: 2026-07-12

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

When enabled, hifiasm ranks every eligible initial EvidenceCase with the
existing Task 3 ordering:

1. `evidence_coverage` descending;
2. `numeric_distance` ascending;
3. the existing clade tie-break;
4. `case_id`.

Then it selects the first five. With one to four eligible cases it selects all;
with none it preserves the existing no-case behavior. Task 6A changes only the
shortlist truncation and does not introduce a combined score.

### Evaluation Is Observational

The deterministic offline runner may compare the legacy initial selector with
the numeric initial retriever and report Recall@k, nDCG@k, reciprocal rank,
evidence coverage, leakage/holdout observations, and replay hashes. Reports
must declare that they are observational and have no activation effect.

No report, corpus, label, metric, or operator assessment can enable/disable the
switch, reject a shortlist, or stop recommendation. There is no 20-case,
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
| Initial ranking | Preserve Task 3 lexicographic order exactly |
| Initial shortlist | Fixed first five, all 1-4, existing no-case behavior for 0 |
| Evaluation | Observational only; no activation or progress effect |
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
- [x] Task 6B uses same-run immutable attempt history without retry retrieval.
- [x] Execution and review retry have independent ownership and budgets.
- [x] No repairability gate or runtime-case promotion remains.
- [x] Initial and retry recommendations share the post-validation human gate.
- [ ] Focused tests and the full regression suite pass for the final worktree.
