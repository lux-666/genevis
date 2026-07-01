# 02 HPC End-To-End Acceptance

Task status: completed.

## Goal

Prove on the HPC host that one real session can start from raw long reads and
finish at:

```text
current_stage=done
supervision_state=completed
```

The run must exercise the implemented raw-read mainline, durable checkpoint
resume, review decision recording, and terminal reporting in one session. This
is an acceptance task, not an implementation design task: record what was run,
what happened, and what had to be fixed.

## Current Baseline

The codebase already has these pieces implemented:

- sqlite durable checkpointing for `run`, `inspect`, and `resume`;
- structured decision-file resume for `assembly_review` and `hic_scaffolding`;
- strict/auto review policy with `review_policy` at the top level of
  run-config;
- real local execution surfaces for `read_qc`, `genome_survey`, `assembly`,
  `assembly_review`, `hic_scaffolding`, and `repeat_annotation` when their
  inputs and tool environments are available;
- function annotation terminal completion when explicit database provenance is
  supplied;
- final review decision report generation on the successful terminal path.

Earlier server/HPC smokes validated these pieces in fragments. This task
validates that they work together in one raw-read-to-terminal session.

## Scope

- Run on the HPC checkout under the normal project environment.
- Use one `session_id` for the entire acceptance run.
- Start from a manifest that includes raw long reads.
- Include enough downstream inputs or provenance for the graph to reach `done`.
- Use the existing sqlite checkpoint backend.
- Use real local execution where the current project already supports it and the
  HPC tools/data are available.
- Use candidate-only or explicit skip behavior where a stage lacks required data.
- Exercise at least the `assembly_review` decision path with durable
  inspect/resume unless a documented auto policy path is intentionally used.
- Exercise `hic_scaffolding` review/resume if Hi-C inputs are included; otherwise
  document the deterministic skip reason.
- Record the full run in `docs/handoff.md` after the HPC pass.

## Non-Goals

- Do not add new method execution just for this task.
- Do not add biological acceptance thresholds.
- Do not require a particular shell script layout.
- Do not prescribe the exact test-code implementation.
- Do not commit private HPC data paths, secrets, or large generated artifacts.
- Do not mark checklist items complete until the HPC run has produced evidence.

## Required Inputs

The acceptance run needs operator-prepared inputs, not committed data files:

- raw long reads accepted by the manifest schema;
- a genome-size prior through manifest or `shared_parameters`;
- a normalized shared read type such as `ont_raw` or `pacbio_hifi`;
- function annotation database provenance sufficient for terminal completion;
- repeat annotation inputs if `repeat_annotation` is expected to execute;
- BUSCO lineage provenance if BUSCO execution is expected;
- Hi-C inputs only if the run is intended to exercise real scaffolding.

The run-config must match the current schema. In particular:

- `review_policy` is a top-level run-config block;
- `shared_parameters` is a top-level run-config block;
- `assembly.flye_parameters` may contain `threads` only when shared or manifest
  values supply read type and genome size;
- `function_annotation.database_provenance` must include the provenance required
  by the schema.

Minimal shape:

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review", "repeat_annotation"],
  "shared_parameters": {
    "read_type": "ont_raw",
    "estimated_genome_size": 5000000
  },
  "assembly": {
    "flye_parameters": {
      "threads": 4
    }
  },
  "assembly_review": {
    "quast": {
      "threads": 4
    }
  },
  "review_policy": {
    "mode": "strict"
  },
  "function_annotation": {
    "database_provenance": [
      {
        "method_name": "function_annotation.candidate.diamond",
        "database_name": "InterPro",
        "database_version": "operator_supplied",
        "snapshot_date": "2026-05-26",
        "database_source_ref": "/path/on/hpc",
        "descriptor_version": "operator_supplied",
        "input_proteins_ref": "/path/on/hpc/proteins.faa",
        "command": "operator supplied command or provenance note"
      }
    ]
  }
}
```

Adjust tools, provenance, local execution stages, and review policy to the
actual HPC environment before running.

## Required Evidence

The completed task must leave evidence for all of the following:

- the terminal inspect output shows `current_stage=done` and
  `supervision_state=completed`;
- the route trace or equivalent durable records show the expected stage path from
  inventory through terminal completion;
- every stage that did not execute has an explicit reason;
- every locally executed method has attempt and artifact records;
- durable resume is demonstrated for any strict human gate that blocks;
- review decisions are recorded with `decided_by` and are visible through
  inspect/final-report outputs;
- final report exists at
  `artifacts/final_report/<session>/review_decision_report.json`;
- function annotation terminal bundle exists;
- repeat annotation output exists when the run claims repeat annotation executed;
- the ledger contains no duplicate method attempts caused by resume;
- any bug found during the HPC run is either fixed in code or recorded as a
  follow-up blocker before checking off the task.

## Tasks

- [x] Prepare the HPC manifest and run-config for one raw-read-to-terminal
  session.
- [x] Validate that the run-config loads under the current schema before starting
  the long run.
- [x] Start the session on HPC with sqlite checkpointing enabled.
- [x] Confirm the run reaches the first expected review gate or documented auto
  decision path.
- [x] Use durable `inspect` output to determine the required operator action.
- [x] Resume from a structured decision file for each strict human gate that
  blocks.
- [x] Confirm downstream execution continues after resume without restarting
  earlier attempts.
- [x] Confirm final terminal state is `done` / `completed`.
- [x] Confirm final report, function annotation bundle, and expected stage
  artifacts exist.
- [x] Check ledger/artifact records for duplicate side effects after resume.
- [x] Record the run, artifact refs, decisions, and any fixes in
  `docs/handoff.md`.

## Acceptance

This task is complete only when the HPC evidence shows:

- one session reached `done` / `completed`;
- at least one review decision path was exercised and audited;
- sqlite resume did not duplicate prior attempts;
- all terminal reports and required provenance artifacts are present;
- the final handoff record is detailed enough for another operator to audit the
  run without re-running it.

## Handoff Record Requirements

After the run, add a Phase 6 end-to-end acceptance section to `docs/handoff.md`
with:

- host and repo checkout context;
- git commit tested;
- session id;
- manifest path and run-config path;
- data dir;
- selected local execution stages;
- review policy used;
- final inspect summary;
- route trace or equivalent stage sequence;
- review gates encountered and decisions made;
- key artifact refs;
- ledger event count;
- duplicate-attempt check result;
- issues found and how they were resolved or deferred.
