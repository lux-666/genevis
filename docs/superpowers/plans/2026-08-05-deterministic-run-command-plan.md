# Deterministic Run Command Implementation Plan

**Date:** 2026-08-05
**Design:** `docs/superpowers/specs/2026-08-05-deterministic-run-command-design.md`
**Status:** approved for implementation

## Goal

Add one deterministic public start boundary:

```bash
gene-agent run \
  --manifest manifest.yaml \
  --run-config run_config.json \
  --data-dir data \
  [--non-interactive]
```

The command must validate both source files and the machine dependency closure
before publishing immutable inputs, then execute only through the existing
frozen-session boundary. It must not retain an unfrozen direct-run path.

## Task 1: Promote no-write frozen-input preflight

**Files:**

- Modify `app/storage/frozen_inputs.py`
- Modify `tests/test_frozen_inputs.py`
- Modify `tests/test_storage_paths.py` only if session-identity validation is
  centralized in `SessionPaths`

**Implementation:**

1. Introduce a typed prepared-input result that retains the canonical
   `InputManifest`, canonical `RunConfig`, reconciled declarations, and the
   run-config model's explicitly supplied field set.
2. Extract a reusable no-write preparation function from
   `freeze_session_inputs` using the existing YAML/JSON loaders,
   `canonicalize_input_declarations`, assembly input validation, local assembly
   validation, and genome-size execution-plan validation.
3. Validate `manifest.session_id` against the target `SessionPaths.session_id`
   before `ensure_run_layout` or immutable publication.
4. Keep `freeze_session_inputs` as the create-only transaction owner. Let it
   accept/reuse prepared models while retaining defensive validation for TUI
   callers.
5. Make dependency stages exactly the caller-confirmed ordered
   `requested_stages`; reject any `local_exec` stage outside that list. For the
   public path, an empty method selection must continue to trigger the existing
   conservative configured-method/helper closure in
   `app.execution.dependency_snapshot`.

**Focused verification:**

```bash
conda run -n gene_agent python -m unittest tests.test_frozen_inputs tests.test_storage_paths -v
```

Required assertions include no immutable publication on identity or biological
preflight failure, exact stage scope, conservative method closure, relative-path
canonicalization, and unchanged TUI-compatible freeze behavior.

## Task 2: Add the public `run` parser and handler

**Files:**

- Modify `app/entrypoints/run.py`
- Modify `tests/test_entrypoints.py`
- Modify `tests/test_frozen_inputs.py`

**Implementation:**

1. Add only the four approved arguments to the `run` subparser; keep all three
   paths required and expose no session/checkpointer/local-stage overrides.
2. Add a dedicated handler that:
   - loads and prepares the two structured inputs through the shared frozen
     component;
   - requires explicit JSON arrays for `requested_stages` and `local_exec` using
     Pydantic's supplied-field set, and rejects `requested_stages: null`;
   - constructs the run path from manifest-owned `session_id`;
   - freezes the exact stage scope with the existing dependency snapshot;
   - calls `execute_frozen_session(..., checkpointer="sqlite",
     interactive_gate=not args.non_interactive)`.
3. Normalize all handler failures to concise `error:` stderr output and exit
   code `1`; retain argparse exit code `2`.
4. When non-interactive execution safely stops at a human gate, print explicit
   `inspect`, `review`, and `resume` continuation commands and return `0`.

**Focused verification:**

```bash
conda run -n gene_agent python -m unittest tests.test_entrypoints tests.test_frozen_inputs -v
```

Required assertions cover help/parser shape, rejected legacy flags, explicit
field presence, rejected `requested_stages: null`, output placement,
preflight-before-freeze/execution, SQLite
dispatch, gate mode, continuation guidance, and terminal failure codes.

## Task 3: Make graph execution frozen-only and remove legacy code

**Files:**

- Modify `app/entrypoints/run.py`
- Delete `app/schemas/entry.py` after a clean reference audit
- Delete `app/storage/materialization.py` after migrating its useful tests and a
  clean reference audit
- Modify `tests/test_entrypoints.py`
- Modify `tests/test_frozen_inputs.py`
- Modify `tests/test_refactor_contracts.py`
- Modify `tests/test_genome_size.py`

**Implementation:**

1. Replace `_run_session` with a frozen-only internal graph seeding/execution
   component whose signature accepts verified frozen state rather than source
   file paths, CLI stage overrides, generated identity, or a `frozen_session`
   switch.
2. Preserve state seeding, graph invocation, checkpoint recovery, terminal
   failure persistence, interactive review, and output reporting.
3. Remove `_peek_manifest_session_id`, CLI ID adoption/alignment, CLI
   `local_exec` merging, and mutable `control/manifest.yaml` materialization.
4. Migrate relative manifest/run-config path tests to the frozen-input boundary
   and migrate graph-seeding tests to the frozen-only executor/public handler.
5. Remove `EntryRequest` and `app.storage.materialization` only after
   repository-wide `rg` proves they are unreferenced.
6. Retain `generate_session_id`, `app.entrypoints.ask_planner`,
   `execute_frozen_session`, execution locks/recovery, and
   `SessionPaths.manifest_path`.

**Focused verification:**

```bash
conda run -n gene_agent python -m unittest tests.test_entrypoints tests.test_frozen_inputs tests.test_refactor_contracts tests.test_genome_size tests.test_tui -v
rg -n "_run_session|_peek_manifest_session_id|EntryRequest|app\.storage\.materialization|materialize_run_layout" app tests
```

The reference search must be empty for removed code, excluding historical docs
that intentionally describe superseded behavior.

## Task 4: Synchronize examples and current architecture docs

**Files:**

- Modify `README.md`
- Modify `docs/architecture/topics/system-map.md`
- Modify `docs/architecture/topics/runtime-orchestration.md`
- Modify `docs/architecture/topics/tui-and-planning.md`
- Modify `docs/architecture/reference/data-contract.md`
- Modify `docs/architecture/langgraph-implementation-map.md`
- Modify relevant `docs/deployment/*.md`
- Modify all current `docs/examples/*run_config*.json`
- Modify `tests/test_configuration_examples.py`

**Implementation:**

1. Document `run` as the deterministic structured-input path and TUI as the
   optional conversational path. State that public run configs require explicit
   JSON arrays and reject `requested_stages: null` while allowing `[]`.
2. Document manifest-owned identity, required data-layout root, fixed SQLite
   durability, frozen preflight, and interactive/non-interactive continuation.
3. Remove current-architecture claims that TUI is the only new-run boundary or
   mutable `manifest.yaml` is execution authority.
4. Ensure every operator run-config example explicitly contains valid
   `requested_stages` and `local_exec` fields.

**Focused verification:**

```bash
conda run -n gene_agent python -m unittest tests.test_configuration_examples -v
rg -n "gene-agent (run|tui)|requested_stages|local_exec|manifest\.yaml" README.md docs/architecture docs/deployment docs/examples
```

## Task 5: Final acceptance

1. Review `git diff --stat`, `git diff --check`, and every changed hunk against
   the approved design.
2. Run the focused suites above to completion.
3. Run the repository-declared gates without weakening configuration:

```bash
conda run -n gene_agent ruff check app tests
conda run -n gene_agent mypy app
conda run -n gene_agent pytest
```

4. Smoke-test `gene-agent run --help` and one mocked/non-executing valid public
   run boundary.
5. Confirm no unrelated files, dependencies, migrations, or temporary artifacts
   remain.
