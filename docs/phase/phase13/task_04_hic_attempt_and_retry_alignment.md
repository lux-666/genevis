# Task 04: Complete the Hi-C Parameter Attempt and Retry Loop

**Phase**: 13
**Status**: Planned
**Depends on**: Task 03

## Goal

Bring AutoHiC attempt progression and retry behavior into the same durable
shape as Assembly while retaining the Hi-C-specific model DTO, execution
adapter, and final adoption review.

This task does not expand the parameter catalog. Task 03 owns which parameters
are visible or adjustable. This task owns attempt sequencing, retry evidence,
routing, and recovery.

## Current State

AutoHiC already has:

- an immutable recommendation manifest;
- a hash-bound mandatory parameter approval gate;
- a compact `ParameterAttempt` created after approval;
- `ParameterAttemptOwner` for the shared execution lifecycle;
- a separate human adoption gate after collect.

The current flow supports only recommendation attempt `0`. An execution
failure terminates the stage, and adoption review cannot request another
parameter recommendation using report/contact-map feedback. The attempt array
is initialized as a one-item special case rather than managed as a validated
chain.

## Design

### Attempt tasks

Support these Hi-C recommendation tasks:

- `recommend_initial`;
- `retry_after_execution_failure`;
- `retry_after_review_failure`.

These values reuse the existing canonical `ParameterAttemptTask` literals and
extend the Hi-C request DTO beyond its current initial-only task. The response
remains the complete Hi-C parameter mapping defined by Task 03. There is no
model-controlled retry disposition.

Every task produces a new immutable recommendation record. A valid response
always enters the parameter approval gate. No retry recommendation is executed
automatically.

### Attempt chain

Use sequential integer attempt IDs. Each retry attempt records:

- `previous_attempt_id`;
- the previous sealed attempt manifest ref and seal hash;
- the retry task;
- immutable refs to the failure or review feedback supplied to the model;
- the new recommendation, approval decision, frozen attempt, execution
  evidence, and collect/review result as they become available.

Older attempts are never overwritten. Before creating attempt `N + 1`, seal
attempt `N` and verify the predecessor relation. Keep only compact cursors and
refs in checkpoint state; hydrate large logs, reports, and model bodies from
artifacts.

### Execution-failure retry

After the shared lifecycle persists a failed execution result, a Hi-C retry
gate may create a ref-only retry request containing:

- previous attempt ID and seal hash;
- process return code and bounded failure classification;
- stdout/stderr refs and a bounded log tail;
- previous approved parameters and constructed config ref.

Routing then returns to the recommender. The new recommendation pauses at the
same hash-bound human approval gate as the initial recommendation.

Retry budgets remain operator configuration, represented as a typed Hi-C stage
run-config field rather than a recommended parameter. Exhaustion stops with an
explicit terminal reason and does not synthesize a fallback parameter set.

Even when the model summary recommends stopping or escalating, a shape-valid
parameter mapping still opens the human approval gate. Only the human decision
or an exhausted operator-owned retry budget may stop the loop.

### Review-feedback retry

Keep the existing final decisions `accept_candidate`, `keep_prior_assembly`,
and `stop_human_review`. Add an explicit `request_parameter_retry` decision for
reviewers who want another AutoHiC attempt.

That decision persists a review-failure artifact containing reviewer notes
and refs to the AutoHiC report, contact map, scaffold FASTA, and assembly file.
It seals the current attempt and routes to the canonical
`retry_after_review_failure` task.
The review decision does not invoke AutoHiC directly.

### Status and recovery

The stage router must distinguish:

- pending parameter approval;
- approved parameters ready for freeze;
- frozen and executed recovery markers owned by the shared lifecycle;
- retry requested after execution failure;
- retry requested after review feedback;
- retry budget exhausted;
- accepted, prior assembly retained, or terminally stopped.

Resume from a parameter gate must verify request ID, request hash,
recommendation hash, attempt ID, task, and predecessor binding. Resume from
freeze or run follows the shared lifecycle and must not rebuild or re-execute.

## Implementation Surface

- `app/stages/hic_scaffolding/parameters/workflow.py`
- `app/stages/hic_scaffolding/builder.py`
- `app/stages/hic_scaffolding/routing.py`
- `app/stages/hic_scaffolding/nodes.py`
- `app/llm/parameters/hic/request.py`
- `app/llm/parameters/hic/prompt.py`
- `app/llm/parameters/hic/recommender.py`
- `app/llm/parameters/hic/response.py`
- `app/schemas/artifacts.py`
- `app/entrypoints/review_interactive.py`
- parameter-attempt schemas/helpers already shared with Assembly

Reuse the Assembly attempt-chain and sealing concepts where their contracts are
stage-neutral. Do not share Assembly biological DTOs, prompt profiles, or
quality interpretation code with Hi-C.

Extend `HiCScaffoldDecisionArtifact.decision` and the interactive review adapter
with `request_parameter_retry`. Do not add a parallel decision artifact or a
stage-local retry schema.

## Failure Behavior

- Missing or changed predecessor evidence fails closed before a retry prompt.
- Invalid model shape creates immutable invalid-response evidence and does not
  open an approval gate.
- Rejected parameters do not execute.
- A valid retry recommendation cannot bypass human approval.
- Retry exhaustion cannot fall back to defaults or a previous parameter set.
- `keep_prior_assembly` remains a valid non-retry outcome.

## Tests

- Initial recommendation still blocks at attempt `0` approval.
- Execution failure seals attempt `0` and creates retry request `1`.
- Retry prompt is hydrated from immutable failure evidence.
- Every shape-valid retry response, including unusual values, opens approval.
- No retry response field can route around approval.
- Retry recommendation blocks at a new human approval gate.
- Approved retry freezes and executes attempt `1` without changing attempt `0`.
- Review `request_parameter_retry` carries reviewer notes and AutoHiC evidence
  refs into the retry request.
- Stale predecessor seals, recommendation hashes, and approval bindings fail.
- Retry budgets are deterministic and replay-safe.
- Resume after freeze and run remains idempotent.

## Acceptance Criteria

- [ ] AutoHiC supports a validated multi-attempt chain.
- [ ] Execution failures can request a new recommendation.
- [ ] Human review can explicitly request a parameter retry.
- [ ] Every valid initial or retry recommendation enters human approval.
- [ ] Retry evidence is artifact-backed and ref-only in checkpoint state.
- [ ] No automatic fallback, automatic approval, or direct retry execution
      exists.
- [ ] Lifecycle and review-resume tests cover attempts beyond `0`.
