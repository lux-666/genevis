# Task 5: Fix Provenance And Empty-Manifest Policy

Status: done.

## Goal

Make inventory output trustworthy for downstream planners.

## Current Problem

Asset provenance currently writes the asset file path under `manifest_ref`, and
empty manifests can validate successfully.

## Reuse From `rna_agent`

Reusable:

- Append-only ledger facts.
- Artifact metadata structure.
- Tests that enforce schema behavior before stage behavior.

Do not copy:

- single-cell dataset assumptions

## Required Behavior

- Asset provenance includes:
  - actual `manifest_ref`
  - `declared_path` or `source_path`
  - optional registry event metadata
- Empty manifests fail by default.
- If dry-run mode is desired later, it must be explicit.

## Files

- `app/schemas/manifest.py`
- `app/stages/inventory/nodes.py`
- `tests/test_manifest.py`
- `tests/test_inventory.py`

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory -v
```

## Human Help Trigger

If empty manifests are needed for a planned UI or dry-run workflow, the coding
framework has not specified that workflow.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
