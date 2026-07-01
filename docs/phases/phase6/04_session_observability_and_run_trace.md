# 04 Session Observability And Run Trace

Task status: code-complete; local-validated. HPC validation pending.

## Goal

Give operators a concise, durable explanation of what happened in a session:
which stages ran, which methods executed, which stages were skipped, where human
or policy decisions occurred, what failed or blocked, and where the important
artifacts are.

The output should be useful after a long HPC run, when the operator should not
need to manually read ledger JSONL, artifact indexes, checkpoint snapshots, and
stage-specific reports to answer "what happened?"

## Relationship To Tasks 02 And 03

Task 02 has produced a real HPC session that reaches terminal completion.
Task 03 is code-complete for the interactive human-gate and resume flow, with
HPC validation still pending. This task turns durable evidence from those
sessions into a user-facing trace. It should be validated against the Phase 6
task 02 acceptance run and against a blocked/resumed session that exercises the
Phase 6 task 03 operator flow.

## Current Baseline

The current project already writes several durable records that the trace should
reuse rather than duplicate:

- session metadata and business events under `<data_dir>/ledger/<session>/`;
- artifact metadata under `<data_dir>/artifacts/<session>/artifacts.jsonl`;
- blocked/failed operator reports;
- final review decision reports on completed terminal paths;
- sqlite checkpoints when the run used the durable checkpoint backend.

The existing `inspect` workflow infers session state from these records. A trace
implementation should build on that durable surface, but it must also expose
where current records are incomplete. For example, some completed sessions may
not have a fully persisted route trace outside checkpoint or operator-report
payloads, and current review records are mostly stage-scoped rather than
per-request scoped.

## Scope

- Add an operator-facing trace view for a session.
- The trace must work for completed, blocked, and failed sessions.
- The trace must be derived from durable records already written by the workflow.
- The trace must include enough artifact refs for the operator to inspect the
  underlying evidence.
- The trace must be available from the existing inspect workflow.
- The trace must have both machine-readable and human-readable output.
- The trace must surface durable-record gaps explicitly instead of pretending
  that unavailable details are known.

## Non-Goals

- Do not build a web UI.
- Do not add real-time streaming.
- Do not introduce an external tracing backend.
- Do not require the original Python process to still be alive.
- Do not require a checkpoint to reconstruct facts that are already present in
  ledger or artifact records.
- Do not invent biological conclusions that are not present in existing
  artifacts, ledger events, or review records.
- Do not retrofit broad runtime telemetry or streaming logs into this task.
- Do not prescribe exact internal module names or test file names in this task
  document.

## Required Trace Content

The trace must answer these session-level questions:

- What session was inspected?
- What manifest and data dir were used?
- What commit/run context is known, if recorded?
- What is the final or current stage, if it can be reconstructed?
- What is the final or current supervision state?
- Which stages were traversed?
- Which stages executed real methods?
- Which stages were planned-only or skipped?
- Which review decisions occurred, and who/what made them?
- Which artifacts should the operator open first?
- Were there failures, blocked gates, or missing follow-up actions?

For each stage, the trace must show:

- stage name;
- status using a small stable vocabulary such as `executed`, `candidate_only`,
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

Idempotent generation matters. Re-running trace generation for unchanged durable
records should either reuse the existing trace artifact paths or update them in
place without adding duplicate artifact registrations or misleading ledger
events.

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

- [x] Define the stable trace fields and status vocabulary.
- [x] Define a durable-record gap vocabulary for unavailable, checkpoint-only,
  and unsupported details.
- [x] Add trace generation from durable session records.
- [x] Add machine-readable and human-readable trace outputs.
- [x] Register trace outputs as artifacts.
- [x] Expose trace generation through inspect.
- [x] Ensure completed, blocked, and failed sessions all produce useful output.
- [x] Ensure repeated trace generation is idempotent, including artifact and
  ledger registration.
- [x] Add verification coverage for completed, blocked, skipped, failed, and
  decision-containing sessions.
- [ ] Validate the trace against the Phase 6 task 02 HPC acceptance run.
- [ ] Validate the trace against a Phase 6 task 03 interactive-review
  blocked/resumed session once HPC validation exists.
- [x] Record an example trace artifact path in `docs/handoff.md`.

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

## Implemented State

Trace generation lives in `app/reports/trace_writer.py` (previously an empty
placeholder; no new module added):

- Status vocabulary (`app/reports/trace_writer.py:33`): `executed`,
  `candidate_only`, `blocked_human`, `failed`, `completed`,
  `no_durable_evidence`.
- Gap vocabulary (`app/reports/trace_writer.py:44`): `unavailable`,
  `checkpoint_only`, `unsupported`.
- `build_session_trace(data_dir, session_id)` reconstructs the trace purely
  from durable records: `Ledger.read_session` / `read_events`,
  `read_artifact_records`, and `inspect_session` for terminal supervision
  state, checkpoint persistence, decisions, and `next_action`. Per-stage
  status is derived from attempt events + candidate/artifact evidence + review
  events; method outcomes, timing (event timestamps), candidate methods, and
  review/decision refs are attached per stage.
- `render_trace_markdown(trace)` produces the operator-readable text; failures
  drop the conda wrapper line (`_first_error_line`) so the real tool error
  shows.
- `write_session_trace(data_dir, session_id)` writes `run_trace.json` +
  `run_trace.md` under `artifacts/run_trace/<session>/`, registers both as
  artifacts (`run_trace.session`, `run_trace.session_markdown`) and ledger
  `artifact_registered` events. Regeneration is idempotent via
  content-equality + existing-index-row checks (same pattern as
  `write_operator_report`). The trace's own `run_trace.*` artifacts are
  excluded from `artifact_count`, `stage_path_observed`, and `open_first`, so
  re-running `--trace` does not inflate the operator-facing artifact total. A
  session with no durable ledger record builds and returns the trace for
  display but writes nothing to disk (`json_ref`/`markdown_ref` are `None`) —
  there is no session to attach artifacts to, so persisting would only leave
  orphans.

Key durable-record gap (always surfaced): the authoritative ordered
`route_trace` and per-stage `skip_reasons` live only in checkpoint/in-memory
state. The trace reconstructs an evidence-based `stage_path_observed` and
marks the authoritative ordering `checkpoint_only` rather than fabricating it.
A stage emitting multiple `review_requested` events is flagged `unsupported`
(decisions bind at stage granularity), and a missing session.json yields an
`error: session_not_found` plus an `unavailable` gap.

CLI: `gene-agent inspect --session-id <s> --data-dir <d> --trace` generates
and registers the trace, prints the readable markdown (or embeds `trace`,
`trace_json_ref`, `trace_markdown_ref` in `--json`). Wired in
`app/entrypoints/run.py` `_run_inspect`.

Tests: `tests/test_run_trace.py` (11 tests) cover executed/decision,
planned-only, failed (clean error), blocked (pending action), the
checkpoint-only/unsupported/unavailable gaps, artifact registration,
idempotent regeneration, trace-artifact exclusion from the count across
repeated runs, and the missing-session no-write path.

Local example (failed-terminal session, regenerated from durable records on a
Mac dev checkout):

- session id: `gene-20260527-000002`
- command: `gene-agent inspect --session-id gene-20260527-000002
  --data-dir data --trace`
- json: `data/artifacts/run_trace/gene-20260527-000002/run_trace.json`
- markdown: `data/artifacts/run_trace/gene-20260527-000002/run_trace.md`
- reported: `supervision_state=failed_terminal`,
  `final_or_current_stage=assembly` (source: operator_report),
  `stage_path_observed=[read_qc, genome_survey, assembly]`, assembly `failed`
  on `assembly.candidate.flye`, open-first
  `operator_report.failed_terminal`, gaps `route_trace` /
  `skip_reasons` (both `checkpoint_only`).

HPC validation against the Phase 6 task 02 acceptance run
(`phase6-e2e-20260526-a`) and a task 03 blocked/resumed session is still
pending; run `gene-agent inspect --trace` against those sessions on the
server and record the refs in `docs/handoff.md`.
