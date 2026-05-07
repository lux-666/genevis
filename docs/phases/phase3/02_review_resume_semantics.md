# 02 Review Resume Semantics

Task status: planned.

## Goal

Define and implement the minimal control-plane semantics for resuming existing
human review gates after `supervision_state: blocked_human`.

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
    notes: list[str] = []
```

The exact fields must be approved before implementation. Do not infer missing
candidate IDs, method names, or final assembly refs from free text.

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

### YAML Boundary

Do not store resume payload schemas, review decisions, or per-session approvals
in YAML. These are runtime facts and belong in Pydantic request models,
artifacts, checkpoints, and ledger events. YAML remains for config defaults and
descriptor capabilities.

## Tasks

- [ ] Decide the minimal `ResumeReviewRequest` schema with human approval before
  coding.
- [ ] Add validation that the requested resume stage matches `active_interrupt`.
- [ ] Add assembly-review resume logic that commits a validated decision and
  advances to `hic_scaffolding`.
- [ ] Add Hi-C resume logic that records selected scaffold/skip decision without
  losing the pre-Hi-C annotation fallback.
- [ ] Add review decision artifacts for Hi-C resume if missing.
- [ ] Add ledger events for resume request accepted/rejected if existing
  `review_decision` events are insufficient.
- [ ] Add a CLI lifecycle command only after the structured resume helper is
  tested.
- [ ] Add tests for invalid stage, invalid decision, missing candidate,
  accepted assembly candidate, Hi-C method selection, and Hi-C skip.

## Acceptance

- A blocked `assembly_review` state can resume from a validated decision.
- A blocked `hic_scaffolding` state can resume from a validated decision.
- Resume clears `active_interrupt` only after writing auditable artifacts and
  ledger events.
- Invalid resume payloads do not mutate stage outputs.
- Resume does not invent thresholds, method defaults, or hidden fallbacks.
- Durable cross-process resume requirements are explicit if memory checkpointer
  limits the first implementation.

## Non-Goals

- Do not implement a scheduler, queue, or daemon.
- Do not implement generic pause/retry/abort in this task.
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
