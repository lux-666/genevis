# 04 Session Observability And Run Trace

Task status: not started.

## Goal

Give operators a concise, durable explanation of what happened in a session:
which stages ran, which methods executed, which stages were skipped, where human
or policy decisions occurred, what failed or blocked, and where the important
artifacts are.

The output should be useful after a long HPC run, when the operator should not
need to manually read ledger JSONL, artifact indexes, checkpoint snapshots, and
stage-specific reports to answer "what happened?"

## Relationship To Tasks 02 And 03

Task 02 proves that one real HPC session can reach terminal completion. This
Task 03 makes the human-gate and resume interaction usable without manual
command assembly. This task turns durable evidence from those sessions into a
user-facing trace. It should be validated against the Phase 6 task 02 acceptance
run once that run exists, and against a blocked/resumed session that exercises
the Phase 6 task 03 operator flow.

## Scope

- Add an operator-facing trace view for a session.
- The trace must work for completed, blocked, and failed sessions.
- The trace must be derived from durable records already written by the workflow.
- The trace must include enough artifact refs for the operator to inspect the
  underlying evidence.
- The trace must be available from the existing inspect workflow.
- The trace must have both machine-readable and human-readable output.

## Non-Goals

- Do not build a web UI.
- Do not add real-time streaming.
- Do not introduce an external tracing backend.
- Do not require the original Python process to still be alive.
- Do not invent biological conclusions that are not present in existing
  artifacts, ledger events, or review records.
- Do not prescribe exact internal module names or test file names in this task
  document.

## Required Trace Content

The trace must answer these session-level questions:

- What session was inspected?
- What manifest and data dir were used?
- What commit/run context is known, if recorded?
- What is the final or current stage?
- What is the final or current supervision state?
- Which stages were traversed?
- Which stages executed real methods?
- Which stages were planned-only or skipped?
- Which review decisions occurred, and who/what made them?
- Which artifacts should the operator open first?
- Were there failures, blocked gates, or missing follow-up actions?

For each stage, the trace must show:

- stage name;
- status using a small stable vocabulary such as `executed`, `planned_only`,
  `skipped`, `blocked_human`, `failed`, or `completed`;
- methods attempted, if any;
- attempt outcomes, if available;
- key artifact refs;
- skip or block reason, if available;
- review request and decision refs, if the stage had a gate;
- timing information when the durable records make it reliable.

For each review decision, the trace must show:

- stage;
- decision value;
- `decided_by`;
- candidate or method selected, when relevant;
- rationale artifact ref when present;
- decision artifact ref when present.

## Output Requirements

The trace must produce:

- a structured JSON artifact for downstream tooling;
- a readable Markdown or text artifact for operators;
- inspect output that can print or reference the readable trace;
- artifact and ledger registration for generated trace artifacts.

The trace output should degrade gracefully. If a field cannot be reconstructed
from durable records, it should be shown as missing or unavailable rather than
fabricated.

## Operator Experience Requirements

An operator should be able to:

- run inspect for a session and request the trace;
- read a stage-by-stage summary without opening raw ledger files;
- see the first few artifact refs needed for review or debugging;
- distinguish "not run", "planned only", "skipped", and "failed";
- understand whether a blocked session is waiting on a human action;
- use the trace after copying the data dir to another machine, as long as the
  durable ledger and artifact records are present.

## Data Consistency Requirements

- The trace must not duplicate or mutate workflow state.
- Re-generating the trace for the same durable records should be idempotent.
- Trace artifact registration should not obscure original workflow artifacts.
- The trace must not treat missing artifacts as successful outputs.
- A blocked or failed session must still produce a partial trace.
- Multiple review requests in one session must be represented distinctly when
  the durable records make that possible; if current records are too coarse, the
  trace must surface that limitation.

## Tasks

- [ ] Define the stable trace fields and status vocabulary.
- [ ] Add trace generation from durable session records.
- [ ] Add machine-readable and human-readable trace outputs.
- [ ] Register trace outputs as artifacts.
- [ ] Expose trace generation through inspect.
- [ ] Ensure completed, blocked, and failed sessions all produce useful output.
- [ ] Ensure repeated trace generation is idempotent.
- [ ] Add verification coverage for completed, blocked, skipped, failed, and
  decision-containing sessions.
- [ ] Validate the trace against the Phase 6 task 02 HPC acceptance run.
- [ ] Record an example trace artifact path in `docs/handoff.md`.

## Acceptance

This task is complete only when:

- inspect can produce a readable trace for an existing durable session;
- a completed session trace explains the full stage path and terminal reports;
- a blocked session trace names the pending human action and relevant refs;
- a failed session trace names the failure and prior successful stages;
- trace artifacts are registered and reproducible;
- the Phase 6 task 02 HPC run can be explained from the trace without manually
  reading raw ledger JSONL.

## Handoff Record Requirements

After validation, update `docs/handoff.md` with:

- session id used for trace validation;
- command or operator action used to generate the trace;
- trace artifact refs;
- summary of what the trace reported;
- any durable-record gaps discovered during trace generation;
- follow-up tasks if the trace cannot represent a needed fact reliably.
