# Repository-Wide Quality Gates Design

## Goal

Make the repository-wide commands below reliable merge gates without weakening
their current configuration:

```shell
ruff format --check .
ruff check .
mypy app
pytest -q
```

The work proceeds in two deliberately separate tracks. Ruff is cleaned up
mechanically first. Strict mypy is then repaired from the persisted-state
boundary outward, rather than silenced with ignores or a broader `Any` surface.

## Current Baseline

The 2026-07-25 baseline is:

- `ruff check .`: 1,380 findings:
  - 1,298 E501;
  - 69 I001;
  - 8 F401;
  - 5 F541.
- `mypy app`: 796 errors in 90 files:
  - 245 `type-arg`;
  - 200 `arg-type`;
  - 97 `union-attr`;
  - 61 `no-untyped-def`;
  - 45 `no-untyped-call`;
  - 39 `assignment`;
  - 27 `dict-item`;
  - smaller groups of return, operator, overload, and attribute errors.

A dry run on a temporary copy shows that Ruff formatting followed by safe
autofix reduces the lint backlog to roughly 210 manual E501 findings in 51
files. Implementation records the exact remaining baseline after running those
mechanical commands in the real worktree; the dry-run number is planning
guidance only.

## Ruff Remediation

Ruff cleanup is behavior-neutral and remains separate from type changes:

1. run `ruff format .`;
2. run `ruff check . --fix`;
3. manually wrap the remaining E501 strings, comments, embedded scripts, and
   notebook-source strings;
4. run `ruff format --check .`, `ruff check .`, and the full test suite.

The configured rule set and default line length remain unchanged. No file,
directory, notebook, or rule is excluded to make the result pass.

## Strict State Typing

The persisted checkpoint contract remains JSON-only:

```python
JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
StageCursor = dict[str, JsonValue]
```

This representation is correct for serialization but too broad for business
logic. Reading a cursor key currently produces the full recursive union, which
causes most `arg-type`, `union-attr`, and `dict-item` errors.

The repair introduces two layers:

1. the generic checkpoint layer continues to validate bounded JSON through
   `validate_stage_outputs`;
2. each stage declares a stage-local `TypedDict` cursor contract for the keys it
   reads and writes.

JSON validation alone is not sufficient to narrow a cursor. Each stage therefore
owns a Pydantic `TypeAdapter` for its `TypedDict` cursor. The stage read boundary
first obtains the generic JSON cursor and then calls
`TypeAdapter.validate_python`; only the successfully validated result is exposed
to node bodies as the stage-local cursor. The cursor contracts use
`total=False` for lifecycle-dependent fields but precisely type every declared
value. Unsupported keys remain allowed only when the stage explicitly declares
an extension mapping; they are not silently discarded.

Writes follow the reverse path. A stage-local update is type-checked against the
same cursor contract, merged with the current cursor, validated by the
stage-specific adapter, serialized to ordinary JSON values, and finally passed
through `validate_stage_outputs`. This keeps checkpoint serialization generic
while making business logic precise.

Existing Pydantic artifact models are transient validated views for structured
nested records. Nodes call `model_validate` when reading such records and write
`model_dump(mode="json")` results to checkpoints. A `BaseModel` instance is
never stored directly in `TopLevelState`.

`StateUpdate` is an explicit `TypedDict(total=False)` containing the writable
top-level channels with the same value types as `TopLevelState`, including
control/supervision state, current stage, interrupt, compact references,
shared parameters, errors/notes/trace, stage outputs, resource refs, and skip
reasons. Node return annotations use this partial update contract. It is not a
plain dictionary alias and does not make required `TopLevelState` construction
fields optional. A schema test ensures its keys remain a subset of
`TopLevelState`.

## Mypy Remediation Batches

Strict mypy is fixed in dependency order:

1. **Foundations**
   - add `StateUpdate` and precise collection aliases;
   - annotate graph builders, routing functions, and leaf utilities;
   - fix bare generic collections and local variable inference.
2. **High-volume stage boundaries**
   - Assembly Review;
   - Assembly;
   - T2T;
   - Gene Annotation;
   - Hi-C scaffolding.
3. **Remaining stages and control plane**
   - Inventory, Read QC, Genome Survey, Repeat/Function Annotation, dedup;
   - graph, lifecycle, review, and entrypoint boundaries.
4. **Independent local errors**
   - optional matplotlib/nbformat dependency loading;
   - Literal narrowing and `None` guards;
   - LangGraph/checkpointer generics;
   - real attribute, overload, and variable-shadowing errors exposed by mypy.

Each batch must reduce the error count without adding `type: ignore`,
`ignore_errors`, per-module relaxations, or untyped compatibility paths. A
targeted `cast` is allowed only immediately after a runtime-validated boundary
and must narrow to a concrete domain type.

## Verification and Gate Activation

After every batch:

- run strict mypy on the touched package;
- run the affected tests;
- run changed-file Ruff and `git diff --check`.

At completion:

```shell
ruff format --check .
ruff check .
mypy app
pytest -q
git diff --check
```

All commands must pass from a clean environment using the declared development
dependencies. In this repository, “gate” means these exact repository-wide
commands are the documented required pre-merge checks and return zero. There is
currently no repository-owned CI workflow, so creating or configuring a remote
CI provider and required status checks is outside this change. The work does not
claim that a hosting platform enforces the checks automatically.

## Non-Goals

- changing runtime behavior or workflow contracts merely to satisfy typing;
- replacing the JSON checkpoint contract with a large serialized domain model;
- hiding existing failures through exclusions, baselines, or weaker mypy
  options;
- mixing unrelated product refactors into the mechanical Ruff cleanup.
