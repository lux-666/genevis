# 05 Operator Usability Pack

Task status: implemented.

## Goal

Make the existing workflow understandable and operable without reading code,
hand-authoring JSON from scratch, or reverse-engineering artifact paths.

This task is product hardening, not new bioinformatics execution. The user
should be able to answer three questions from the CLI and generated reports:

```text
Where is my run?
What does it need from me?
What exact file or command should I use next?
```

## Scope

- Improve `gene-agent inspect` output for blocked and failed sessions.
- Add a machine-readable `--json` inspect mode if it is not already present.
- Surface a concise next-action block for:
  - missing input / missing tool / blocked env
  - human review gates
  - function-annotation database provenance gates
  - completed runs
- Generate review decision-file templates from the latest review request.
- Write readable terminal reports for `blocked_human` and `failed_terminal`
  sessions, not only successful `done` sessions.
- Refresh top-level status docs so they match the implemented Phase 5 durable
  resume and review-policy behavior.
- Keep examples copy-pasteable for the server/HPC path with absolute
  `--data-dir` guidance.

## Non-Goals

- Do not add a web UI.
- Do not add new lifecycle operations such as retry, abort, or rebuild.
- Do not add new biological tools.
- Do not change review decision semantics.
- Do not add automatic thresholds or automatic database downloads.

## Expected Outputs

- `gene-agent inspect --session-id ...` prints current state, durable resume
  availability, decision requirements, latest relevant artifacts, and a clear
  next action.
- `gene-agent inspect --json` emits the same information as a stable JSON
  object suitable for wrappers and notebooks.
- A command or flag exists to write a structured decision-file template for the
  active review gate.
- Blocked and failed-terminal sessions write an operator report artifact with
  the reason, required action, and relevant refs.
- `README.md`, `docs/handoff.md`, and `docs/phases/phase_completion_plan.md`
  no longer contradict the implemented sqlite durable resume path.

## Tasks

- [x] Audit current CLI output for `run`, `inspect`, and `resume` against real
  Phase 5 smoke states.
- [x] Add inspect JSON output while preserving the existing text output.
- [x] Add next-action projection for review gates, env/tool blocks, provenance
  blocks, and terminal success.
- [x] Add decision-file template generation for `assembly_review` and
  `hic_scaffolding`.
- [x] Write blocked/failed operator report artifacts through the existing
  artifact registry boundary.
- [x] Add tests for text output, JSON output, template generation, and failed
  report generation.
- [x] Update top-level status docs and examples.

## Implementation Notes

- `app/entrypoints/run.py` now exposes `inspect --json` and
  `inspect --write-decision-template PATH`; the text renderer preserves the
  existing summary fields and appends `next_action.*` lines.
- `app/control/lifecycle.py` projects durable ledger/artifact records into a
  shared operator summary with latest report/final artifacts, decision
  requirements, and next-action guidance for review gates, missing-input blocks,
  env/tool blocks, provenance failures, completed runs, and missing sessions.
- `app/graph/builder.py` writes blocked/failed operator reports at the parent
  graph route boundary, so direct `build_graph(...).invoke(...)` callers and
  notebooks get the same report artifacts as the CLI.
- `app/reports/review_report.py` writes lightweight
  `operator_report.blocked_human` and `operator_report.failed_terminal`
  artifacts through `ArtifactMeta` and the session artifact index; reports keep
  only compact refs, active-interrupt summaries, route-tail, and action hints.
- `tests/test_entrypoints.py` covers parser flags, inspect text/JSON output,
  decision-template generation, failed provenance report artifacts, and missing
  RepeatMasker tool report artifacts. `tests/test_read_qc.py` covers missing
  input next-action and direct parent graph report generation.
  `tests/test_review_report.py` covers blocked/failed operator report
  registration.
- `README.md`, `docs/handoff.md`, and
  `docs/phases/phase_completion_plan.md` now describe the implemented sqlite
  resume path and operator inspect/resume commands with absolute `--data-dir`
  examples.

## Acceptance

- A user can inspect a blocked session and create the required decision file
  without opening source code.
- A failed provenance gate tells the user exactly which provenance fields or
  refs are missing.
- The docs match the actual Phase 5 implementation status.
- All new reports are artifacts and do not put large outputs in graph state.

## Verification

Planned final verification:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_entrypoints tests.test_review_report tests.test_durable_resume -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Final validation run:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_entrypoints tests.test_review_report -v
# Ran 74 tests in 3.943s — OK
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_entrypoints tests.test_review_report tests.test_durable_resume -v
# Ran 78 tests in 4.506s — OK
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
# Ran 336 tests in 15.041s — OK
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
# OK
git diff --check
# OK
```
