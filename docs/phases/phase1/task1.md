# Task 1: Fix Inventory Review-Gate Routing

Status: done.

## Goal

Ambiguous inventory inputs must stop at inventory review instead of advancing to
`read_qc`.

## Current Problem

`app/stages/inventory/builder.py` routes `review_gate` to `commit`, and
`commit_inventory` advances to the next stage. This means a blocked human review
state can still move biological routing forward.

The original test `test_inventory_triggers_review_for_ambiguous_reads` asserted
`current_stage == "read_qc"`, so the test encoded the bug. It now expects the
stage to remain at `inventory`.

## Reuse From `rna_agent`

Reusable:

- The idea that human gates live in explicit review nodes.
- The separation between stage execution nodes and review/control nodes.
- The route-controller discipline: parent graph should stop on blocked or
  terminal supervision state.

Do not copy:

- single-cell review semantics
- Harmony retry logic

## Required Behavior

- [x] Ambiguous long-read vs long-RNA inputs set `active_interrupt`.
- [x] `supervision_state` becomes `blocked_human`.
- [x] `current_stage` remains `inventory`.
- [x] No downstream biological stage runs.
- [x] Asset registry is not written before human approval unless a human-approved
  provisional-registration rule is explicitly added later.

## Files

- `app/stages/inventory/builder.py`
- `app/stages/inventory/nodes.py`
- `app/graph/routing.py`
- `tests/test_inventory.py`

## Steps

1. [x] Change the test first so the ambiguous-input case expects `current_stage` to
   stay `inventory`.
2. [x] Run the focused test and confirm it fails.
3. [x] Change inventory graph routing so `review_gate` ends the subgraph without
   commit.
4. [x] Make parent routing stop when `supervision_state == "blocked_human"`.
5. [x] Run the focused test again.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_inventory -v
```

## Human Help Trigger

If implementation requires real LangGraph `interrupt()` resume semantics, the
current framework has not specified the resume protocol.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
