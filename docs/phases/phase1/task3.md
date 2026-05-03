# Task 3: Clarify Requested-Stage Semantics With Human Decision

Status: done for Phase 1 by removing the misleading `--stages` flag.

## Goal

Resolve the mismatch between CLI `--stages` and graph routing, but do not let a
coding assistant choose the routing product behavior independently.

## Current Problem

`app/entrypoints/run.py` parses `--stages`, and `TopLevelState` stores
`requested_stages`, but routing ignores it. Users can request a subset and still
run the placeholder route.

## Reuse From `rna_agent`

Reusable:

- Structured CLI entrypoint pattern.
- `requested_stages` style from the reference project.
- Route-controller tests for direct stage entry.

Do not copy:

- reference stage names
- single-cell dependency assumptions

## Human Decision Required

The framework currently does not specify which requested-stage semantics are
desired. Present these options to the human and implement only the chosen one:

1. [x] Remove `--stages` until routing semantics are implemented.
2. [ ] Restrict `--stages` to inventory-only smoke mode.
3. [ ] Implement dependency-aware requested-stage routing later, after inventory
   route intent and skip reasons are corrected.

Recommended Phase 1 direction to present: option 1 or option 2. Do not implement
option 3 in this task.

## Files

- `app/entrypoints/run.py`
- `app/schemas/state.py`
- `app/graph/routing.py` if routing is implemented now
- tests for CLI or routing

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_inventory tests.test_project_scaffold -v
```

## Human Help Trigger

If the human has not explicitly chosen option 1 or option 2, the current
framework is not detailed enough for implementation.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
