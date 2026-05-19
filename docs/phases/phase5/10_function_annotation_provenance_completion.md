# 10 Function Annotation Provenance Completion

Task status: planned.

## Goal

Turn the current expected `function_annotation` provenance gate into an honest
completion path when the user provides real predicted proteins and explicit
database provenance.

This task should allow a narrow downstream workflow to finish:

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

- [ ] Decide whether operator-facing provenance should be supplied through
  manifest, run-config, or both.
- [ ] Document the provenance JSON shape with a copy-pasteable example.
- [ ] Tighten validation for real protein refs and provenance field completeness.
- [ ] Ensure the function annotation bundle records provenance and honest
  planning status without fake tool outputs.
- [ ] Add tests for success, missing provenance, empty provenance, mismatched
  proteins ref, and missing protein artifact.
- [ ] Add a server/HPC completion smoke and record it in `docs/handoff.md`.

## Acceptance

- A user can complete the workflow when they provide proteins and database
  provenance.
- Missing or inconsistent provenance fails before terminal success.
- The final report and ledger make the basis for completion auditable.
- No function annotation tool execution or database download is implied.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_function_annotation tests.test_run_config tests.test_entrypoints tests.test_review_report -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
