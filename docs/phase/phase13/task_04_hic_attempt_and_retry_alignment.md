# Task 04: Complete the Hi-C Parameter Attempt and Retry Loop

**Phase**: 13
**Status**: Planned
**Depends on**: Task 03

## Goal

Bring AutoHiC attempt sequencing and retry behavior into the same durable shape
as Assembly while using the sparse recommendation contract from Task 03.

Each Hi-C attempt owns its own sparse parameter mapping. A retry recommendation
uses the previous attempt only as evidence and never as a mapping to update or
complete.

## Current State

AutoHiC already has:

- an immutable recommendation manifest;
- a hash-bound parameter approval gate;
- a compact `ParameterAttempt` after approval;
- `ParameterAttemptOwner` for shared `freeze -> run -> collect` execution;
- a separate adoption review after collect.

The current flow supports only recommendation attempt `0`. Execution failure
terminates the stage, adoption review cannot request a parameter retry, and the
attempt array is managed as a one-item special case.

## Attempt Tasks

Use the existing canonical task names:

- `recommend_initial`;
- `retry_after_execution_failure`;
- `retry_after_review_failure`.

Every task returns the same response shape:

```json
{
  "parameters": {
    "only_the_current_attempt_override": "value"
  },
  "summary": "Optional reviewer-facing explanation"
}
```

`parameters` may be empty. There is no `retry_disposition`, parameter patch,
full-catalog response, or absent-key list.

## Attempt Chain

Use sequential integer attempt IDs. Each retry recommendation records:

- `previous_attempt_id`;
- previous sealed-attempt manifest ref and seal hash;
- retry task;
- immutable failure/review-evidence refs;
- the new sparse recommendation and its approval decision.

Older attempts are immutable. Before creating attempt `N + 1`, seal and verify
attempt `N`.

The previous parameter mapping is prompt evidence only. The recommendation for
attempt `N + 1` is the exact mapping returned for that attempt:

```text
attempt N parameters       -> retry context only
attempt N+1 parameters     -> attempt N+1 L2 source
```

No merge operation exists between those mappings.

## Execution-Failure Retry

After the shared lifecycle persists a failed execution result, the Hi-C retry
gate writes a ref-only retry request containing:

- previous attempt ID and seal hash;
- process return code and bounded failure classification;
- stdout/stderr refs and a bounded log tail;
- previous approved sparse parameters;
- constructed config and execution refs.

The recommender receives a bounded projection of that evidence and returns a
new sparse mapping. Every shape-valid mapping, including `{}`, enters a new
human parameter-approval gate before freeze.

Retry budgets are operator configuration. Exhaustion stops with an explicit
reason and does not copy a prior mapping or synthesize defaults.

## Review-Failure Retry

Keep the final decisions:

- `accept_candidate`;
- `keep_prior_assembly`;
- `stop_human_review`.

Add `request_parameter_retry` for a reviewer who wants another AutoHiC attempt.
That decision persists reviewer notes plus refs to the AutoHiC report, contact
map, scaffold FASTA, and assembly file. It seals the current attempt and routes
to `retry_after_review_failure`.

The review decision does not execute AutoHiC and does not prefill the next
mapping. The next mapping comes only from the new recommendation response and
again requires human parameter approval.

## Status and Recovery

Routing must distinguish:

- pending parameter approval;
- approved sparse parameters ready for freeze;
- lifecycle-owned frozen and executed recovery markers;
- retry requested after execution failure;
- retry requested after review failure;
- retry budget exhausted;
- accepted, prior assembly retained, or terminally stopped.

Resume from a parameter gate verifies request ID/hash, recommendation hash,
attempt ID, task, and predecessor binding. Resume from freeze or run follows the
shared lifecycle and does not rebuild or re-execute.

Checkpoint state stores compact cursors and refs. Logs, model bodies, reports,
effective config values, and default-source details remain in artifacts.

## Human Approval Rule

Every shape-valid initial or retry recommendation enters `parameter_approval`.
The model cannot stop, approve, reject, or divert the retry through a response
field.

Human approval binds the exact current sparse mapping. It does not approve a
merged mapping and does not ask separately about catalog keys that are absent
from the mapping.

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
- shared parameter-attempt sealing/timeline helpers

Extend `HiCScaffoldDecisionArtifact.decision` with
`request_parameter_retry`. Do not add a stage-local attempt schema or parallel
decision artifact.

## Failure Behavior

- Missing or changed predecessor evidence fails before model invocation.
- Invalid response structure persists invalid-response evidence and does not
  open approval.
- Every structurally valid sparse mapping opens approval.
- Rejected parameters do not execute.
- Retry exhaustion does not reuse a prior mapping.
- Post-approval executability failure leaves the recommendation unchanged.
- `keep_prior_assembly` remains a valid non-retry outcome.

## Tests

- Attempt `0` initial recommendation blocks at parameter approval.
- Execution failure seals attempt `0` and creates retry request `1`.
- Retry context contains attempt `0` parameters as evidence only.
- Attempt `1` sparse mapping is not merged with attempt `0`.
- `parameters: {}` on retry still opens approval.
- No retry response field can route around approval.
- Approved attempt `1` freezes and executes without changing attempt `0`.
- `request_parameter_retry` binds reviewer notes and AutoHiC evidence refs.
- Stale seals, hashes, and approval bindings fail closed.
- Retry budgets and resume after freeze/run are deterministic.

## Acceptance Criteria

- [ ] AutoHiC supports a validated multi-attempt chain.
- [ ] Every attempt owns only its own sparse mapping.
- [ ] Previous mappings are retry evidence and are never merge inputs.
- [ ] Execution and review failures can request a new recommendation.
- [ ] Every shape-valid initial or retry mapping enters human approval.
- [ ] No model-controlled retry disposition or automatic fallback exists.
- [ ] Retry evidence is artifact-backed and ref-only in checkpoint state.
- [ ] Lifecycle and review-resume tests cover attempts beyond `0`.
