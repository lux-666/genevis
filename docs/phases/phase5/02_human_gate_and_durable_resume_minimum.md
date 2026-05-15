# 02 Human Gate And Durable Resume Minimum

Task status: implemented (2026-05-14).

## Goal

Make human review gates operational across process boundaries before Phase 5
adds more long-running tools.

The minimum target is:

```text
blocked_human checkpoint
-> inspect durable state
-> submit structured decision file
-> resume same workflow
-> continue downstream
```

This task should make `assembly_review` and `hic_scaffolding` review gates
usable for real server/HPC runs without relying on in-memory state.

## Scope

- Add the smallest durable checkpoint backend required for local/server runs,
  preferably sqlite.
- Add or complete CLI lifecycle commands for:
  - `inspect`
  - `resume`
- Use structured decision files rather than free-form command flags for review
  decisions.
- Support durable resume for existing review gates:
  - `assembly_review`
  - `hic_scaffolding`
- Preserve current in-process resume validation semantics.
- Write review decisions as artifacts and ledger events.
- Continue graph execution after a valid decision.

## Non-Goals

- Do not implement full pause/retry/rerun/rebuild semantics.
- Do not implement postgres.
- Do not build a web UI.
- Do not add automatic assembly acceptance thresholds.
- Do not make purge/polish/contact-map decisions automatically.
- Do not add new bioinformatics method execution.
- Do not store large metric tables or command logs in checkpointed state.

## Decision File Contract

Use a structured JSON decision file. The exact schema can be refined during
implementation, but it should include:

```json
{
  "session_id": "phase5-review-smoke",
  "stage": "assembly_review",
  "decision": "pass",
  "candidate_id": "phase5-review-smoke:assembly.candidate.flye",
  "final_assembly_ref": "data/.../draft_assembly.fasta",
  "notes": ["human reviewed QUAST report"],
  "metadata": {
    "reviewer": "operator",
    "basis": "server-side QUAST smoke"
  }
}
```

The CLI should validate the decision file before mutating checkpoint, artifacts,
or ledger.

## Expected Outputs

- Durable checkpoint configuration for normal CLI runs.
- `gene-agent inspect --session-id ...` reports:
  - current stage
  - supervision state
  - active interrupt
  - review request artifact
  - allowed decisions
  - latest artifact refs
- `gene-agent resume --decision-file ...` can resume a blocked
  `assembly_review` or `hic_scaffolding` session.
- Review decision artifacts are written and registered.
- Ledger records review decision and lifecycle continuation.
- The workflow can continue from `assembly_review` to downstream stages after
  `pass` / `accept` / approved action decisions.

## Tasks

- [x] Decide the durable checkpointer backend for Phase 5 minimum, with sqlite
  preferred for local/server use.
- [x] Add checkpointer config without changing the small `TopLevelState`
  contract.
- [x] Extend CLI parsing for `resume --decision-file PATH`.
- [x] Add a decision file schema under `app/schemas/` or the existing review
  schema boundary.
- [x] Load latest checkpoint by `session_id` / thread id.
- [x] Validate active interrupt and decision compatibility before mutation.
- [x] Apply existing `assembly_review` resume semantics through the durable
  path.
- [x] Apply existing `hic_scaffolding` resume semantics through the durable
  path.
- [x] Write decision artifacts and ledger events.
- [x] Continue graph execution after resume and report final state.
- [x] Add tests for valid resume, invalid decision, stage mismatch, missing
  checkpoint, and no duplicate side effects.
- [x] Add a server-side review/resume smoke documented in handoff.

## Future Work (Out Of Task 02 Scope)

- `--max-blocked-seconds` CLI flag on `resume` that forwards into
  `resume_review_state(blocked_timeout_seconds=...)`. The lifecycle helper
  already supports the timeout path; the CLI just needs an opt-in surface.
  Deferred because task 02 acceptance does not require it.

## Acceptance

- A server/HPC run can stop at `blocked_human`, exit the process, and later
  resume from a decision file.
- `resume` does not restart from inventory or create a fresh initial state.
- Invalid decisions fail before mutating checkpoint, artifacts, or ledger.
- Review decisions remain auditable through artifacts and ledger.
- Generated assemblies and Hi-C scaffold candidates still require explicit human
  decisions.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_entrypoints -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

## Implemented State

Added:

- `langgraph-checkpoint-sqlite>=2.0` dependency in `pyproject.toml`
  (resolved 3.1.0 in the local conda env).
- `app/graph/checkpointer.py` — `get_checkpointer(backend, *, data_dir)`
  factory with `sqlite` (default) and `memory` backends, plus
  `checkpoint_db_path(data_dir)` helper. Sqlite db is opened with
  `sqlite3.connect(check_same_thread=False)` and lives at
  `<data_dir>/checkpoints/checkpoints.sqlite`.
- `app/review/base.py::load_resume_review_request(path)` — JSON →
  `ResumeReviewRequest` loader with the same error-message style as
  `load_run_config`. Used by the resume CLI before any state mutation.
- `app/entrypoints/run.py::_run_resume(args)` — wires
  `gene-agent resume --session-id … --decision-file … --data-dir …
  [--checkpointer sqlite|memory]` through the existing in-process resume
  helpers. Rejects `memory` backend, mismatched session ids, missing or
  invalid decision files, missing checkpoint db, and missing thread
  before mutating state. After validation it calls
  `resume_review_state(state, decision)` →
  `graph.update_state(cfg, update)` → `graph.invoke(None, cfg)`.
- New test files:
  - `tests/test_checkpointer.py` — backend factory smokes.
  - `tests/test_durable_resume.py` — end-to-end seed-blocked-state →
    close db → reopen → resume CLI → assert ledger / state advance.
  - `LoadResumeReviewRequestTests` in `tests/test_review_resume.py`.
  - `ResumeCLITests` in `tests/test_entrypoints.py` covering
    parser shape and the five rejection branches.

Updated:

- `app/graph/builder.py::build_graph(*, checkpointer=None, backend, data_dir)`
  — explicit `checkpointer=` still wins (existing tests rely on it);
  otherwise the factory resolves the saver from `backend` + `data_dir`.
- `app/control/lifecycle.py::inspect_session(...)` — reports
  `checkpoint_persistence: sqlite` /
  `durable_resume_available: True` when
  `<data_dir>/checkpoints/checkpoints.sqlite` exists, else stays on
  `memory_only` / `false`. Also reconstructs `decision_requirements`
  (stage / decisions_allowed / candidate_ids / candidate_methods)
  from the latest review request artifact JSON so the resume CLI can
  guide an operator without their having to open the artifact
  manually. `inspect_state(...)` no longer carries persistence
  fields — those are properties of the checkpoint backend, not of
  the in-memory state.
- `app/entrypoints/run.py`:
  - `_resolve_checkpointer_backend(args)` selects backend with
    priority explicit `--checkpointer` > `GENE_AGENT_CHECKPOINTER`
    env var > `"sqlite"`. `tests/__init__.py` sets the env var to
    `memory` for the test suite, so every unittest discover
    invocation must use `-t .` to load the package init (all repo
    scripts and docs already do).
  - `main(...)` calls `_load_dotenv_without_override()` once at the
    entry point so every subcommand (run, ask, inspect, resume) sees
    the same secrets without re-implementing the loader. Server
    `.env` files now reach the resume code path too.
  - `_run_resume(...)` records `resume_started` before mutation,
    `resume_completed` / `resume_failed` afterward, with the decision
    file ref and decision summary in the payload. The resume CLI
    fails fast on `--checkpointer memory`, mismatched session_id
    (between CLI flag, decision file, and the loaded checkpoint),
    missing decision file, missing checkpoint database, and missing
    thread.
  - `_run_inspect(...)` prints `decision_requirements.stage`,
    `decisions_allowed`, `candidate_ids`, and
    `candidate_methods` when an unresolved review request exists.
- `pyproject.toml` — pinned `langgraph>=1.1,<2`,
  `langgraph-checkpoint>=4.1,<5`,
  `langgraph-checkpoint-sqlite>=3.1,<4`. The
  `tests/test_checkpointer.SqliteSaverApiContractTests` class asserts
  the SqliteSaver constructor / `setup()` / `.conn` shape so a
  dependency upgrade that changes the surface fails CI rather than
  failing on the server at resume time.
- `scripts/server_check.sh` — verifies
  `from langgraph.checkpoint.sqlite import SqliteSaver` succeeds in
  the orchestrator env so a missing `pip install -e .` does not turn
  into a runtime ImportError on the first `gene-agent run`.

Untouched (intentionally):

- `TopLevelState` schema and the existing in-process
  `apply_assembly_review_resume_decision` /
  `apply_hic_scaffolding_resume_decision` semantics — durable resume
  reuses them through the same `resume_review_state(...)` path.
- Pause / retry / rerun / rebuild lifecycle commands — explicitly out
  of scope.
- Postgres / web UI / automatic accept thresholds.

Verification commands run after the change:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_review_resume tests.test_entrypoints tests.test_durable_resume tests.test_checkpointer -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Server-side smoke (operator workflow): see
[`docs/deployment/mac_dev_server_pull.md`](../../deployment/mac_dev_server_pull.md)
under "Block-And-Resume Loop". The commands live there as the single
source of truth; this task doc only references them to avoid drift.
