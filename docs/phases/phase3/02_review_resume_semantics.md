# 02 Review Inspect And Resume Interaction

Task status: implemented.

## Goal

Define how users inspect a paused workflow, make a human-gate decision, and
resume the same workflow after `supervision_state: blocked_human`.

Initial targets:

- `assembly_review`
- `hic_scaffolding`

## Project Context

Phase 2 already writes blocked states:

- `assembly_review` writes `active_interrupt`, review request artifacts, and
  `review_requested` ledger events.
- `hic_scaffolding` blocks when Hi-C reads exist and chromosome-level assembly
  is requested, but it has only a compact `active_interrupt` payload today.

There is no approved resume payload schema and `app/control/lifecycle.py` is
still placeholder-level. The next task should make resume explicit before more
method execution is added.

Task 02 is the second half of the Phase 3 interaction loop:

```text
run graph
  -> blocked_human
  -> inspect review request
  -> human decides
  -> resume
  -> continue graph
```

It is not a second planner and not a restart path.

## Files

- Modify: `app/control/lifecycle.py`
- Modify: `app/entrypoints/run.py`
- Modify: `app/review/base.py`
- Modify: `app/review/human_gate.py`
- Modify: `app/stages/assembly_review/nodes.py`
- Modify: `app/stages/hic_scaffolding/nodes.py`
- Modify: `app/control/ledger.py` if resume-specific events are missing
- Test: `tests/test_review_resume.py`
- Test: `tests/test_assembly_review.py`
- Test: `tests/test_hic_scaffolding.py`
- Documentation: update `docs/handoff.md` only when behavior changes

## Design

### Status And Inspect

Users need to answer two different questions:

```text
看一下 s1 进度
看一下 s1 为什么卡住
```

The system should resolve both through `session_id=s1`:

```text
load latest state/checkpoint or session runtime snapshot
-> read current_stage
-> read supervision_state
-> read active_interrupt
-> summarize stage statuses
-> summarize latest artifacts and ledger events
```

The response should distinguish:

- `running`
- `completed`
- `failed_terminal`
- `blocked_human`
- memory-only checkpoint limitations, when durable resume is unavailable across
  processes

For `blocked_human`, inspect should show the review request artifact and a
compact decision schema. It should not ask the LLM to decide.

### Identity Boundaries

Use these identifiers with separate meanings:

- `session_id`: workflow run primary key.
- `current_stage`: where the graph is.
- `active_interrupt.stage`: where human input is required.
- `attempt_id`: one method/stage execution attempt.
- `checkpoint` / `state_version`: recoverable state snapshot.
- `artifact_ref` / `asset_ref`: durable result reference.
- `ledger`: append-only audit history.

`session_id` answers "which run". It does not answer "which stage should rerun"
or "which old output should be replaced".

### Resume Request

Create a narrow structured resume request, for example:

```python
class ResumeReviewRequest(BaseModel):
    session_id: str
    stage: Literal["assembly_review", "hic_scaffolding"]
    decision: str
    candidate_id: str | None = None
    method_name: str | None = None
    final_assembly_ref: str | None = None
    rejected_candidates: list[str] = []
    notes: list[str] = []
    metadata: dict[str, Any] = {}
```

The exact fields must be approved before implementation. Do not infer missing
candidate IDs, method names, or final assembly refs from free text.

A natural-language resume helper may draft this request later, but the accepted
operation is the validated structure, not the raw user sentence.

### Resume Boundary

Resume should be a lifecycle operation over a blocked checkpoint, not a new
stage planner:

```text
load blocked state/checkpoint
-> validate active_interrupt matches requested stage
-> validate decision against allowed decisions / candidate methods
-> write decision artifact
-> record ledger event
-> clear active_interrupt
-> invoke graph from the same structured state
```

If checkpoint persistence is still memory-only in the local scaffold, the first
implementation may expose a test helper that accepts the blocked state directly.
The CLI shape can be added with the same payload contract, but durable
cross-process resume may wait for a later checkpointer task.

If the user says "继续 s1", the implementation must load the latest state for
`session_id=s1` and continue from the blocked/current checkpoint. It must not
call `initial_state(...)` again or rerun from inventory.

### Assembly Review Resume

`assembly_review` already has `ReviewPayload` and `ReviewDecisionArtifact`.
Resume should validate that:

- the blocked stage is `assembly_review`;
- `decision` is one of the allowed review decisions;
- `accept`, `purge`, and `continue_low_confidence` include enough candidate /
  final assembly information to publish downstream refs;
- rejected candidates remain traceable;
- thresholds are not invented during resume.

### Hi-C Resume

`hic_scaffolding` needs a comparable decision artifact before resume is useful.
Resume should validate that:

- the blocked stage is `hic_scaffolding`;
- selected method is one of the planned candidate methods;
- contact-map/manual-review policy is explicit in notes or decision metadata;
- the pre-Hi-C assembly remains available as annotation fallback;
- if the user chooses skip, the existing skip artifact path remains auditable.

### LLM Use

Natural language may help draft a resume request later, but task 02 should first
make the structured resume payload deterministic. An LLM must not directly
write checkpoint state or bypass validation.

### Resume, Retry, Rerun, Rebuild-From

Keep lifecycle verbs distinct:

| Operation | Meaning |
|---|---|
| `resume` | Continue from a human gate after a validated decision. |
| `retry` | Try a failed retryable stage/method attempt again. |
| `rerun` | Create a new attempt for a completed or failed stage while preserving old outputs. |
| `rebuild-from` | Recompute from one stage and mark downstream dependent outputs stale or needing rerun. |

Task 02 implements only `resume` plus enough `status` / `inspect` behavior to
make resume usable.

If a user says "重新做 s1 的 stage5", that is not resume. It needs an explicit
future operation:

- diagnostic `rerun`: create a new `attempt_id`, preserve old artifacts, and do
  not change downstream refs automatically;
- replacement `rerun`: create new artifact refs, write ledger events, promote
  selected outputs, and mark downstream stages stale or `needs_rerun`;
- `rebuild-from`: recompute the selected stage and all dependent downstream
  stages with an explicit invalidation plan.

Do not implement these in task 02 without a separate approved spec.

### YAML Boundary

Do not store resume payload schemas, review decisions, or per-session approvals
in YAML. These are runtime facts and belong in Pydantic request models,
artifacts, checkpoints, and ledger events. YAML remains for config defaults and
descriptor capabilities.

## Tasks

- [x] Decide the minimal `ResumeReviewRequest` schema with human approval before
  coding.
- [x] Add a status/inspect helper that resolves `session_id` to latest state,
  `current_stage`, `supervision_state`, `active_interrupt`, stage statuses,
  latest artifacts, and relevant ledger events.
- [x] Add tests that inspecting a blocked session surfaces the review request
  artifact and allowed decision shape.
- [x] Add validation that the requested resume stage matches `active_interrupt`.
- [x] Add assembly-review resume logic that commits a validated decision and
  advances to `hic_scaffolding`.
- [x] Add Hi-C resume logic that records selected scaffold/skip decision without
  losing the pre-Hi-C annotation fallback.
- [x] Add review decision artifacts for Hi-C resume if missing.
- [x] Add ledger fields for resume decisions using the existing
  `review_decision` event.
- [x] Add a CLI lifecycle command only after the structured resume helper is
  tested.
- [x] Reject retry/rerun/rebuild-from requests with explicit "not this
  operation" errors until those semantics are separately specified.
- [x] Add tests for invalid stage, invalid decision, missing candidate,
  accepted assembly candidate, Hi-C method selection, and Hi-C skip.

## Implementation Notes

改动要点：

- Added `ResumeReviewRequest` in `app/review/base.py` with the approved runtime
  fields: `session_id`, `stage`, `decision`, `candidate_id`, `method_name`,
  `final_assembly_ref`, `rejected_candidates`, `notes`, and `metadata`.
- Implemented `app/control/lifecycle.py` with non-mutating `inspect_state(...)`,
  durable-record `inspect_session(...)`, in-process `resume_review_state(...)`,
  synchronous `blocked_timeout_seconds` checks, and explicit unsupported
  lifecycle-operation errors.
- Added `blocked_at` timestamps to assembly-review and Hi-C active interrupts.
  Timeout behavior is checked only when lifecycle helpers are called; there is no
  scheduler, daemon, YAML policy, or background timer.
- Added assembly-review resume application for `accept`, `purge`,
  `continue_low_confidence`, and `stop_human_review`. Continuing decisions must
  provide explicit `candidate_id` and `final_assembly_ref`; `rerun` remains out
  of scope and is rejected as a lifecycle operation.
- Added `HiCScaffoldDecisionArtifact` and Hi-C resume application for
  `select_method`, `skip`, and `stop_human_review`. Method selection records the
  chosen method and contact-map policy but does not fabricate scaffolded outputs;
  annotation continues on the pre-Hi-C fallback assembly.
- Extended ledger review-decision events with optional `method_name`,
  `artifact_ref`, and `metadata`, plus read helpers for session inspection.
- Added artifact-index read helpers and `gene-agent inspect --session-id ...` for
  durable ledger/artifact inspection. CLI inspect reports
  `checkpoint_persistence: memory_only` and `durable_resume_available: false`.
- Did not implement CLI cross-process `resume`, because the scaffold still uses a
  memory-only LangGraph checkpointer. The implemented resume path is the tested
  in-process state helper and does not call `initial_state(...)`.

Verified focused command:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_assembly_review tests.test_hic_scaffolding tests.test_entrypoints -v
```

Result: 49 tests passed.

## Acceptance

- A blocked `assembly_review` state can resume from a validated decision.
- A blocked `hic_scaffolding` state can resume from a validated decision.
- A user can inspect `session_id=s1` and see whether it is running, completed,
  failed, or blocked for human review.
- Inspecting a blocked run exposes the review request artifact and decision
  requirements without mutating state.
- Resume clears `active_interrupt` only after writing auditable artifacts and
  ledger events.
- Invalid resume payloads do not mutate stage outputs.
- Resume does not invent thresholds, method defaults, or hidden fallbacks.
- Resume does not restart the workflow from `initial_state(...)`.
- Retry, rerun, and rebuild-from remain explicitly out of scope.
- Durable cross-process resume requirements are explicit if memory checkpointer
  limits the first implementation.

## Non-Goals

- Do not implement a scheduler, queue, or daemon.
- Do not implement generic pause/retry/rerun/rebuild-from/abort in this task.
- Do not implement requested-stage routing.
- Do not implement real Hi-C scaffolding tool execution.
- Do not let natural-language resume bypass the structured payload.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_assembly_review tests.test_hic_scaffolding -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
