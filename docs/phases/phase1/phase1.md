# Phase 1: Inventory MVP Correction Record

## 1. Phase Goal

Phase 1 is the inventory entry point for Gene Agent.

It should make the system able to:

- load a user input manifest
- validate lightweight file metadata
- classify standard genome-workflow assets
- write an asset registry artifact
- write ledger events
- route forward, stop for review, or fail terminally with clear state

It must not run QC tools, assemblers, annotation tools, heavyweight checksums,
or remote jobs.

## 2. Current Code State

Phase 1 started from this feature commit:

```text
fad12d3 Implement Phase 1: Parent Graph and Inventory MVP
```

Implemented files:

- `app/schemas/manifest.py`
- `app/stages/inventory/builder.py`
- `app/stages/inventory/nodes.py`
- `app/stages/inventory/routing.py`
- `app/control/ledger.py`
- `app/entrypoints/run.py`
- `tests/test_manifest.py`
- `tests/test_inventory.py`

Current capabilities:

- YAML/JSON manifest loading
- simple manifest schema
- file existence, size, format, compression, platform, and layout inference
- asset registry JSON writing
- ledger event writing
- CLI `gene-agent run`
- manifest and inventory tests
- review-required inputs stop at inventory
- existing assembly inputs route to `assembly_review`
- the unimplemented `--stages` CLI flag is not exposed

## 3. What Can Be Reused From `rna_agent`

Use `/Users/a1/Documents/BGIwork/rna_agent` as the pattern source.

Directly reusable patterns:

- Router-hub parent graph from `app/graph/builder.py` and `app/graph/routing.py`.
- Small checkpoint state discipline from `app/schemas/state.py`.
- JSONL ledger pattern from `app/control/ledger.py`.
- Artifact metadata pattern from `app/schemas/artifacts.py`.
- Descriptor registry loading pattern from `app/schemas/descriptors.py`.
- Phase documentation style from
  `/Users/a1/Documents/BGIwork/rna_agent/docs/phase1/README.md` and its
  numbered task docs.
- Test style using standard-library `unittest`.

Do not directly reuse:

- single-cell stage names (`integration`, `annotation`, `benchmark`)
- h5ad-specific schema fields
- Harmony-specific method family code
- Scanpy/Harmony prompt assets

## 4. Corrections Collected From Review

These issues were found during review and collected into the feature branch.

1. Review gate advanced to `read_qc` instead of stopping.

   Evidence: `app/stages/inventory/builder.py:66-68` routes
   `review_gate -> commit`; `app/stages/inventory/nodes.py:165-175` always
   advances to `next_stage_name("inventory")`. `tests/test_inventory.py:107-126`
   The correction makes review-required inventory end with
   `supervision_state="blocked_human"` and `current_stage="inventory"`.

2. Existing assembly did not alter routing.

   Evidence: `tests/test_inventory.py:128-148` expected
   `assembly_fasta_registered` to advance to `read_qc`. The correction routes
   existing assembly inputs to `assembly_review` and writes skip reasons for
   read QC, genome survey, and assembly.

3. CLI `--stages` was parsed but not honored.

   The correction removes the flag until requested-stage semantics are specified.

4. Manifest payload was stored in graph state.

   The correction stores validation summary fields and reloads the manifest from
   `input_manifest_ref` during registration.

5. Asset provenance was misleading.

   The correction records the actual `manifest_ref` and a separate
   `declared_path`.

6. Empty manifests could validate.

   The correction makes validation fail when no assets are declared.

7. One test helper was broken but unused.

   The correction removes the unused helper.

## 5. Optimization Directions

These are directions, not implementation plans.

- Make review gates stop routing until the human decision is resolved.
- Make existing assembly entry a first-class route mode.
- Keep graph state reference-oriented.
- Move reusable ledger/artifact/descriptor code toward the proven `rna_agent`
  contract shape.
- Delay full requested-stage routing until dependency and skip semantics are
  explicit.

If a coding assistant reads an optimization direction and cannot map it to an
existing task file, the framework is not detailed enough for autonomous
implementation.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## 6. Task Order

Follow these in order:

1. [task1.md](task1.md) - Fix inventory review-gate routing.
2. [task2.md](task2.md) - Add existing-assembly route intent and skip reasons.
3. [task3.md](task3.md) - Decide and enforce requested-stage semantics.
4. [task4.md](task4.md) - Reduce manifest payload stored in graph state.
5. [task5.md](task5.md) - Fix provenance and empty-manifest policy.

Task 1 through task 5 have been collected into the feature branch. Do not
re-open these tasks unless a regression appears.

## 7. Verification

Run after each task:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold -v
```

Run before declaring Phase 1 stable:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```
