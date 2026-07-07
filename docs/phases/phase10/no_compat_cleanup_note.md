# Phase 10 No-Compat Cleanup Note

This note describes the cleanup contract and the current Phase 10 status.

## Target Contract

The supported architecture should have one source of truth for each concern:

- method availability and planner metadata come from `descriptors/registry.yaml`, method descriptors, and method catalogs;
- assembly route selection and parameter recommendation go through the shared assembly decision/advisor boundary;
- parameter-loop state is typed and compact through `parameter_attempts`;
- runtime artifacts use session/attempt path refs with hashes as integrity fields;
- knowledge-pack cases are evidence, rules are hard boundaries, and `SKILL.md` is heuristic guidance.

## Unsupported As Extension Points

Do not preserve or document these as supported APIs:

- duplicate hardcoded planner method registries;
- stage-local assembly model calls outside the shared decision boundary;
- loose ad hoc parameter-loop state as a parallel contract to `parameter_attempts`;
- hash-only refs as the running graph's runtime artifact scheme;
- compatibility wrappers whose only purpose is to keep old Phase 10 migration paths alive.

## Current Status

Completed:

- route selection and parameter recommendation both use the shared assembly decision boundary;
- hifiasm/hifiasm_ont freeze paths consume typed `parameter_attempts`;
- main-flow parameter profile and prompt refs use `data/artifacts/assembly/<session>/parameter_attempt_<N>/`;
- hashes are recorded for integrity.
- planner method metadata is derived from descriptors/catalogs through `app/entrypoints/planner_catalog.py`;
- `ask_planner.py` no longer maintains `_STAGE_METHODS`;
- `ask_interactive.py` consumes the same descriptor-derived catalog;
- architecture-regression tests fail if the duplicate planner registry, old planner constants, stage-local assembly model calls, or hash-only runtime refs return;
- command metadata and review failure context are written as standalone attempt-scoped artifact files.
