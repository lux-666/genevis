# 03 Session Observability And Run Trace

Task status: not started.

## Goal

Give operators a structured, readable trace of what happened in a completed or
stopped session — which stages ran, which methods executed, which were skipped,
what blocked, and what requires attention — without reading raw ledger JSONL.

This directly satisfies Phase 6 exit criterion #2:

> User-facing reports explain what was run, what was skipped, and what requires
> human attention.

The existing `final_report/review_decision_report.json` covers review decisions.
The existing `operator_report` covers blocked/failed states. Neither provides a
chronological stage-by-stage execution trace or a "what happened in my session"
summary an operator can read cold.

## Scope

- Read ledger `events.jsonl` and artifact records for a session, reconstruct a
  chronological run trace.
- Output a structured `session_trace.json` artifact and a human-readable
  `session_trace.md` text artifact.
- Expose through: `gene-agent inspect --session-id <S> --data-dir <D> --trace`
  (prints the markdown trace to stdout and writes artifacts).
- The trace should contain, per stage:
  - stage name
  - status: `executed`, `planned_only`, `skipped`, `blocked_human`, `failed`
  - methods attempted (with attempt_id, status, artifact refs)
  - human gates encountered and decisions made
  - skip reason (if skipped)
  - duration (wall clock from first `attempt_started` to last
    `attempt_succeeded`/`attempt_failed` in that stage, if available)
- Session-level summary:
  - session id, manifest ref, final `current_stage`, `supervision_state`
  - total stages traversed
  - total methods executed vs planned-only
  - total artifacts registered
  - human decisions count and origins
  - total elapsed time (from `session_created` to last event timestamp)

## Non-Goals

- Do not build a web UI or interactive dashboard.
- Do not add real-time streaming trace (this is post-hoc reconstruction).
- Do not change the ledger event schema — work with existing events.
- Do not add OpenTelemetry, Jaeger, or external tracing backends.
- Do not duplicate the review decision report or operator report content; link
  to their artifact refs instead.

## Design

### Data Source

The trace is purely derived from:
1. `<data_dir>/ledger/<session_id>/events.jsonl` — all lifecycle events
2. `<data_dir>/artifacts/<session_id>/artifacts.jsonl` — registered artifact
   index
3. `<data_dir>/ledger/<session_id>/session.json` — session metadata

No new state fields, no new ledger event types. If the existing ledger does not
record enough information for a trace field, that field should show `null` or
`"unavailable"` rather than fabricating data.

### Output Artifacts

Written to `<data_dir>/artifacts/session_trace/<session_id>/`:

- `session_trace.json` — structured machine-readable trace
- `session_trace.md` — human-readable markdown summary

Registered as artifact type `session_trace.summary` in the artifact index and
ledger.

### JSON Schema (sketch)

```json
{
  "session_id": "phase6-e2e-20260526-a",
  "manifest_ref": "data/phase6_e2e/manifest.yaml",
  "final_state": {
    "current_stage": "done",
    "supervision_state": "completed"
  },
  "elapsed_seconds": 142.5,
  "summary": {
    "stages_traversed": 9,
    "methods_executed": 4,
    "methods_planned_only": 3,
    "artifacts_registered": 18,
    "human_decisions": 1,
    "decisions_by_origin": {"human": 1}
  },
  "stages": [
    {
      "stage": "inventory",
      "status": "executed",
      "methods": [],
      "skip_reason": null,
      "human_gate": null,
      "artifacts": ["asset_registry/..."],
      "elapsed_seconds": 0.8
    },
    {
      "stage": "read_qc",
      "status": "executed",
      "methods": [
        {
          "method_name": "read_qc.stats.seqkit_stats",
          "attempt_id": "...",
          "status": "succeeded",
          "artifact_refs": ["read_qc/.../seqkit_stats.tsv"]
        }
      ],
      "skip_reason": null,
      "human_gate": null,
      "artifacts": ["read_qc/.../read_qc_summary.json", "read_qc/.../long_reads.fofn"],
      "elapsed_seconds": 2.1
    },
    {
      "stage": "assembly_review",
      "status": "executed",
      "methods": [
        {
          "method_name": "assembly_review.eval.quast",
          "attempt_id": "...",
          "status": "succeeded",
          "artifact_refs": ["assembly_review/.../quast_summary.json"]
        }
      ],
      "skip_reason": null,
      "human_gate": {
        "reason": "assembly_review_human_decision_required",
        "decision": "accept",
        "decided_by": "human",
        "candidate_id": "flye_01"
      },
      "artifacts": ["assembly_review/.../assembly_review_request.json"],
      "elapsed_seconds": 45.2
    }
  ]
}
```

### Markdown Rendering

```markdown
# Session Trace: phase6-e2e-20260526-a

**Status:** done / completed
**Elapsed:** 2m 22s
**Stages:** 9 traversed, 4 executed, 3 planned-only, 1 skipped

## Stage Summary

| # | Stage | Status | Methods | Gate |
|---|-------|--------|---------|------|
| 1 | inventory | executed | — | — |
| 2 | read_qc | executed | seqkit_stats | — |
| 3 | genome_survey | skipped | — | no_low_error_short_reads |
| 4 | assembly | executed | flye (succeeded) | — |
| 5 | assembly_review | executed | quast (succeeded) | accept (human) |
| 6 | hic_scaffolding | skipped | — | no_hic_reads |
| 7 | repeat_annotation | executed | repeatmasker (succeeded) | — |
| 8 | gene_annotation | planned_only | — | — |
| 9 | function_annotation | planned_only | — | — |

## Human Decisions

- **assembly_review**: accept candidate `flye_01`, decided by human

## Artifacts (18 registered)

See: `artifacts/<session>/artifacts.jsonl`

## Reports

- Final report: `final_report/<session>/review_decision_report.json`
- Operator report: n/a (completed)
```

## Implementation Location

- `app/reports/session_trace.py` — new module with `build_session_trace()`
  and `write_session_trace_artifacts()`.
- `app/entrypoints/run.py` — add `--trace` flag to `_run_inspect()`.
- Reuse existing `Ledger.read_events()`, `read_artifact_records()`, and
  `decision_records()` from `app/review/records.py`.

## Tasks

- [ ] Add `app/reports/session_trace.py` with `build_session_trace(data_dir,
  session_id)` that reconstructs the trace from ledger + artifacts.
- [ ] Add `write_session_trace_artifacts(data_dir, session_id)` that writes
  JSON and markdown artifacts and registers them.
- [ ] Add `--trace` flag to `gene-agent inspect` that prints the markdown trace
  to stdout.
- [ ] Handle incomplete sessions gracefully: a session stopped at
  `blocked_human` should still produce a partial trace showing which stages
  completed before the block.
- [ ] Add tests:
  - trace from a completed session ledger;
  - trace from a blocked session ledger (partial);
  - stage elapsed time calculation from event timestamps;
  - human gate decisions reflected in trace;
  - skipped stages shown with skip reason;
  - idempotent re-generation (running trace twice produces same content).
- [ ] Validate against Phase 6 task 02 HPC run data: the trace should match
  the manually-recorded observations in `docs/handoff.md`.

## Acceptance

- `gene-agent inspect --session-id <S> --data-dir <D> --trace` prints a
  readable stage-by-stage summary for a completed session.
- The JSON trace artifact contains per-stage status, method attempts with
  outcomes, human gate decisions, and timing where available.
- A blocked session produces a partial trace (not an error).
- The trace does not re-read checkpoint state — it works entirely from ledger
  and artifact files, so it can be generated even if the sqlite db is missing
  or the process that ran the session is gone.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_session_trace -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check

# After Phase 6 task 02 acceptance, verify against real data:
conda run -n gene_agent gene-agent inspect \
  --session-id <PHASE6_SESSION> \
  --data-dir   /data/work/genevis/data/phase6_e2e/out \
  --trace
```
