# 11 Function Annotation Provenance Completion

Task status: implemented; server/HPC smoke script added, server execution pending.

## Goal

Turn the current expected `function_annotation` provenance gate into an honest
completion path when the user provides real predicted proteins and explicit
database provenance.

This task allows a narrow downstream workflow to finish:

```text
provided/adopted proteins
-> database provenance supplied by user
-> function annotation planning/provenance bundle
-> final report
-> done
```

## Scope

- Treat database provenance as a first-class user-supplied contract, not a
  smoke-only fixture detail.
- Validate that `database_provenance.input_proteins_ref` matches the real
  proteins artifact/ref consumed by `function_annotation`.
- Validate required provenance fields such as database name, version or
  snapshot, source/ref, and method intent.
- Support run-config and/or manifest surfaces that are practical for operators.
- Produce a terminal function annotation bundle and final review report when
  provenance is complete.
- Add server/HPC smoke coverage for a true `done` run.

## Provenance Surface Decision

Use run-config for database provenance and keep manifest focused on physical
input assets.

Why: manifest already owns provided `predicted_proteins`, `gene_models`, and
optional `transcripts_fasta` assets. Putting database provenance there would
mix operator execution/provenance contracts into the asset inventory and would
duplicate the existing `function_annotation.database_provenance` run-config
surface.

## Run-config JSON Shape

`function_annotation.database_provenance` is a non-empty list. Each record must
match the materialized proteins FASTA consumed by `function_annotation`:

```json
{
  "function_annotation": {
    "database_provenance": [
      {
        "method_name": "function_annotation.candidate.interproscan",
        "database_name": "InterPro",
        "database_version": "99.0",
        "snapshot_date": "2026-05-25",
        "database_source_ref": "/data/db/interpro/99.0",
        "descriptor_version": "interproscan.yaml",
        "input_proteins_ref": "/data/work/sample/predicted_proteins.fasta"
      }
    ]
  }
}
```

Required fields:

- `method_name`: descriptor/method intent for the provenance record.
- `database_name`: database family/name.
- `database_version`: explicit database version.
- `snapshot_date`: date or snapshot label for the database state.
- `database_source_ref`: local path, catalog ref, or other operator-supplied
  source reference for the database snapshot.
- `input_proteins_ref`: exact predicted-proteins FASTA path consumed by
  `function_annotation`.
- one of `command` or `descriptor_version`: how the database/method provenance
  was obtained or anchored.

## Non-Goals

- Do not run InterProScan, eggNOG-mapper, DIAMOND, or KofamScan.
- Do not download or install annotation databases.
- Do not accept provenance that cannot be traced to the proteins input.
- Do not fabricate annotation tables.

## Expected Outputs

- Clear provenance schema documentation and examples.
- CLI validation errors that identify the missing or mismatched provenance
  field.
- A server/HPC smoke where existing assembly + provided/adopted annotation +
  database provenance reaches `current_stage=done` and
  `supervision_state=completed`.
- Final report artifact written on the successful terminal path.

## Tasks

- [x] Decide whether operator-facing provenance should be supplied through
  manifest, run-config, or both.
- [x] Document the provenance JSON shape with a copy-pasteable example.
- [x] Tighten validation for real protein refs and provenance field completeness.
- [x] Ensure the function annotation bundle records provenance and honest
  planning status without fake tool outputs.
- [x] Add tests for success, missing provenance, empty provenance, mismatched
  proteins ref, and missing protein artifact.
- [x] Add a server/HPC completion smoke and record it in `docs/handoff.md`.

## Acceptance

- A user can complete the workflow when they provide proteins and database
  provenance.
- Missing or inconsistent provenance fails before terminal success.
- The final report and ledger make the basis for completion auditable.
- No function annotation tool execution or database download is implied.

## Implemented State

- `app/schemas/artifacts.py:324` defines `FunctionDatabaseProvenance` with
  required `database_source_ref`, non-empty text validation, and the existing
  `command` or `descriptor_version` anchoring rule.
- `app/stages/function_annotation/nodes.py:109` validates provenance before
  commit, checks that `input_proteins_ref` exactly matches `proteins_fasta_ref`,
  and now requires the matched proteins ref to be a materialized local file
  before writing plan/bundle artifacts.
- `app/stages/function_annotation/nodes.py:160` and `:219` append both artifact
  index records and ledger `artifact_registered` events for the function plan
  and bundle.
- `app/reports/review_report.py:56` now records the final report in the ledger
  as `final_report.review_decisions`, so the successful terminal path is
  auditable through both artifact index and ledger.
- `app/schemas/run_config.py:117` keeps provenance operator-facing through
  `function_annotation.database_provenance`; no manifest provenance field was
  added.
- `app/entrypoints/run.py:49` documents the run-config provenance surface in
  CLI help, and `_run_session` continues seeding only approved
  `function_annotation.database_provenance` fields into state.
- `tests/test_function_annotation.py` covers missing protein ref, missing
  provenance, empty provenance, incomplete provenance, missing command/
  descriptor anchor, mismatched protein refs, missing materialized protein
  artifact, successful completion, and alias resolution.
- `tests/test_entrypoints.py` adds an existing-assembly + provided annotation +
  run-config provenance E2E that reaches `current_stage=done`,
  `supervision_state=completed`, writes the function bundle/final report, and
  verifies artifact-index plus ledger audit records.
- `scripts/server_phase5_function_provenance_smoke.sh` is the server/HPC smoke
  entrypoint for the true completion path. It extracts the manifest
  `predicted_proteins` asset using manifest-relative path resolution, patches
  the run-config placeholder, requires the protein FASTA to exist, verifies
  terminal success, and checks that planned function tables were not fabricated.
- `scripts/server_phase5_auto_review_smoke.sh` was updated to use
  manifest-provided proteins instead of a non-existent planned proteins path.
  Keep any ignored local `data/phase5_auto_review` fixture files in sync on the
  server; those data templates are not tracked source files.

## Server/HPC Smoke

```bash
bash scripts/server_phase5_function_provenance_smoke.sh \
  --manifest    /abs/path/to/data/phase5_auto_review/manifest.yaml \
  --run-config  /abs/path/to/data/phase5_auto_review/run_config.json \
  --session-id  phase5-function-provenance-YYYYMMDD-a \
  --data-dir    /abs/path/to/data/phase5_function_provenance/out
```

Before running, provide a manifest with real server files for existing assembly,
provided gene models, provided predicted proteins, and optional transcripts.
Relative asset paths are resolved from the manifest directory, matching the
application inventory behavior. Replace `database_source_ref` in the run-config
with the actual database snapshot/catalog reference used by the operator.

Expected terminal output includes:

```text
current_stage: done
supervision_state: completed
```

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_function_annotation tests.test_run_config tests.test_entrypoints tests.test_review_report -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
