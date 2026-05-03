# Task 2: Add Existing-Assembly Route Intent And Skip Reasons

Status: done.

## Goal

When the manifest provides an existing assembly and no raw reads, inventory
should publish route intent that skips read QC, genome survey, and assembly.

## Current Problem

`assembly_fasta_registered` is registered as an asset, but the graph still moves
to `read_qc`. That conflicts with the stage contract.

## Reuse From `rna_agent`

Reusable:

- Compact route hints and stage bundles from stage commit nodes.
- Artifact/ledger references rather than large graph-state payloads.
- Tests that prove direct stage entry and skipped work.

Do not copy:

- h5ad dataset fields
- integration/annotation/benchmark route names

## Required Behavior

- [x] Existing assembly only should produce an `entry_mode` such as
  `existing_assembly`.
- [x] `skip_reasons` should include read QC, genome survey, and assembly.
- [x] Default next stage should be `assembly_review`, because an existing assembly
  still needs evaluation before annotation.
- [x] Stage order should not be changed.

## Files

- `app/stages/inventory/nodes.py`
- `app/stages/inventory/routing.py`
- `app/graph/routing.py` if skip routing is centralized
- `tests/test_inventory.py`
- `docs/phases/phase1/phase1.md`

## Steps

1. [x] Update the existing-assembly test to expect `current_stage ==
   "assembly_review"`.
2. [x] Add assertions for `skip_reasons`.
3. [x] Add compact route intent in inventory output or state.
4. [x] Route existing-assembly inventory commits to `assembly_review`.
5. [x] Run inventory tests.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_inventory -v
```

## Human Help Trigger

If the desired destination for existing assemblies is not clearly
`assembly_review`, the current framework has not specified the route policy.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
