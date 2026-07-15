# Task 4: Hifiasm Methods-Aware Recommendation And Validation

**Phase**: 11
**Status**: Implemented and verified prompt/response boundary and shared post-validation parameter approval
**Depends on**: Task 2 EvidenceCase contract; Task 3 initial numeric retrieval
**Created**: 2026-07-10
**Updated**: 2026-07-12

## Goal

Use the Task 3 initial shortlist as bounded, source-grounded Methods evidence
for parameter recommendation, while keeping the prompt, response, citations,
validation, and replay artifacts deterministic and auditable. For same-run
retry, reuse attempt 0's immutable evidence snapshot and add only the preceding
attempt's structured failure signals; do not retrieve retry cases.

## Initial Recommendation Boundary

The initial hifiasm Methods contract:

- accepts only the attested Task 3 ordered shortlist;
- projects each EvidenceCase through the shared Task 2 prompt-safe schema;
- uses a closed top-level allowlist and drops raw source documents/debug data;
- applies deterministic case, span, excerpt, and serialized-byte budgets;
- copies a source quote in full or omits the entire span;
- validates case/span citations against prompt-visible evidence;
- validates every L2 parameter decision against the catalog and hard rules;
- preserves compatibility fields consumed by the typed parameter lifecycle.

Task 6A supplies at most five selected initial cases. Task 4's defensive prompt
budgets remain safety limits, not alternate retrieval selection rules and not
activation thresholds.

## Same-Run Retry Boundary

Retry has no second Methods corpus. Its recommendation basis is limited to:

1. attempt 0's immutable profile, initial selector/evidence, Methods spans,
   catalog, hard rules, capability policy, and recommendation rationale;
2. the immediately preceding attempt's frozen parameters and rendered command;
3. that attempt's structured execution result or review diagnosis;
4. the owning execution or review retry budget snapshotted by attempt 0.

The retry prompt contains a closed structured signal block with exact signal
IDs/paths. It does not admit free-form logs, caller-invented evidence, a retry
query/result, or historical retry cases. Changed parameters must name the
preceding failure signal as their basis; inherited initial Methods evidence may
explain parameter semantics but cannot be represented as proof that the retry
succeeded in another run.

Execution-origin recommendations must return a candidate parameter set when
execution budget remains. No classifier, repairability label, or repairability
gate may skip that recommendation. Review-origin retries retain their existing
directional review semantics. Neither path reloads evidence or policy.

## Canonical Prompt And Artifacts

The prompt builder creates one canonical prompt and publishes planned artifacts
only after validation succeeds. The replay prompt, model input, fallback input,
response validator, and downstream parameter validator consume the same
attested content. Attempt-scoped artifacts are immutable and no-clobber;
validation failures expose no partially written prompt.

The closed projection omits unknown containers, raw retrieval query/trace debug
content, caller-supplied Methods metadata, and full source documents. Initial
prompts contain no retry signals. Retry prompts contain only the closed
same-run signal projection and inherited attempt 0 references.

## Shared Parameter Approval Gate

Automatic validation is necessary but does not authorize execution. After a
recommendation passes schema, catalog, hard-rule, capability, and parameter
validation, all three tasks enter the same human gate:

```yaml
stage: assembly
gate_id: parameter_approval
decisions_allowed:
  - approve_parameters
  - reject_parameters
auto_decidable: false
```

This applies to:

- `recommend_initial`;
- `retry_after_execution_failure`;
- `retry_after_review_failure`.

The interrupt binds the proposed attempt/task, previous attempt for retry,
validated recommendation artifact and hash, complete parameter diff, bounded
rationale/failure facts, and inherited evidence/policy references. Resume must
explicitly name `gate_id: parameter_approval` and match the active attempt,
task, previous attempt, and recommendation hash.

- `approve_parameters`: freeze the already validated candidate, append the
  attempt, increment only the owning retry counter when applicable, and route
  to execution.
- `reject_parameters`: record the decision and terminate with no execution. A
  rejected retry appends no retry attempt and consumes neither budget.

Missing/wrong gate IDs, unknown decisions, stale hashes, changed attempt heads,
and duplicate decisions fail closed. The gate reuses the generic review/resume
lifecycle. TUI implementation was not part of Task 4 or Task 6; the later
[runtime contract](tui_hpc_runtime_contract.md) now defines how the generic gate
is presented and resumed without changing this recommendation boundary.

## Acceptance Criteria

- [x] Initial recommendations see only Task 3-attested, prompt-safe evidence.
- [x] Prompt and citation budgets are deterministic and auditable.
- [x] Parameter decisions remain catalog/rule validated.
- [x] Retry uses structured same-run failure signals and attempt 0 evidence only.
- [x] Retry performs no EvidenceCase retrieval or candidate promotion.
- [x] Every valid actionable initial or retry parameter candidate waits for
  the shared human `parameter_approval` decision before freeze or execution;
  `stop` and `expert_review` are not actionable and do not enter parameter
  approval.
- [x] Approval executes; rejection terminates without execution or retry-budget
  consumption.
