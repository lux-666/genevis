# Task 4: Reduce Manifest Payload Stored In Graph State

Status: done.

## Goal

Keep checkpointed graph state small and reference-oriented.

## Current Problem

`validate_inputs` stores the full manifest dict under
`stage_outputs.inventory.manifest`. This is convenient but points in the wrong
direction for long-running workflows.

## Reuse From `rna_agent`

Reusable:

- Small `TopLevelState` discipline.
- Artifact metadata and registry reference patterns.
- Ledger event style for normalized facts.

Do not copy:

- h5ad-specific summary fields

## Required Behavior

- State stores `input_manifest_ref`, validation summary, warnings, errors, and
  compact counts.
- If normalized manifest content must be persisted, write it as an artifact
  under `data/artifacts/manifests/<session_id>/`.
- `register_assets` reloads from `input_manifest_ref` or a normalized manifest
  artifact reference.

## Files

- `app/stages/inventory/nodes.py`
- `app/schemas/state.py` only if adding a normalized manifest ref
- `tests/test_inventory.py`

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_inventory -v
```

## Human Help Trigger

If normalized manifest storage starts to require a broader artifact registry
redesign, the current framework has not specified that storage layer.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
