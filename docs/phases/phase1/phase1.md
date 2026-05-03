# Phase 1: Inventory MVP Correction Record

## 1. Phase Goal

Phase 1 is the inventory entry point for Gene Agent.

It should make the system able to:

- [x] load a user input manifest
- [x] validate lightweight file metadata
- [x] classify standard genome-workflow assets
- [x] write an asset registry artifact
- [x] write ledger events
- [x] route forward, stop for review, or fail terminally with clear state

It must not run QC tools, assemblers, annotation tools, heavyweight checksums,
or remote jobs.

## 2. Current Code State

Phase 1 started from this feature commit:

```text
fad12d3 Implement Phase 1: Parent Graph and Inventory MVP
```

Implemented files:

- [x] `app/schemas/manifest.py`
- [x] `app/stages/inventory/builder.py`
- [x] `app/stages/inventory/nodes.py`
- [x] `app/stages/inventory/routing.py`
- [x] `app/control/ledger.py`
- [x] `app/entrypoints/run.py`
- [x] `tests/test_manifest.py`
- [x] `tests/test_inventory.py`

Current capabilities:

- [x] YAML/JSON manifest loading
- [x] simple manifest schema
- [x] file existence, size, format, compression, platform, and layout inference
- [x] asset registry JSON writing
- [x] ledger event writing
- [x] CLI `gene-agent run`
- [x] manifest and inventory tests
- [x] review-required inputs stop at inventory
- [x] existing assembly inputs route to `assembly_review`
- [x] the unimplemented `--stages` CLI flag is not exposed

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

1. [x] Review gate advanced to `read_qc` instead of stopping.

   Evidence: `app/stages/inventory/builder.py:66-68` routes
   `review_gate -> commit`; `app/stages/inventory/nodes.py:165-175` always
   advances to `next_stage_name("inventory")`. `tests/test_inventory.py:107-126`
   The correction makes review-required inventory end with
   `supervision_state="blocked_human"` and `current_stage="inventory"`.

2. [x] Existing assembly did not alter routing.

   Evidence: `tests/test_inventory.py:128-148` expected
   `assembly_fasta_registered` to advance to `read_qc`. The correction routes
   existing assembly inputs to `assembly_review` and writes skip reasons for
   read QC, genome survey, and assembly.

3. [x] CLI `--stages` was parsed but not honored.

   The correction removes the flag until requested-stage semantics are specified.

4. [x] Manifest payload was stored in graph state.

   The correction stores validation summary fields and reloads the manifest from
   `input_manifest_ref` during registration.

5. [x] Asset provenance was misleading.

   The correction records the actual `manifest_ref` and a separate
   `declared_path`.

6. [x] Empty manifests could validate.

   The correction makes validation fail when no assets are declared.

7. [x] One test helper was broken but unused.

   The correction removes the unused helper.

## 5. Optimization Directions

These are directions, not implementation plans.

- [ ] Make review gates resume from a real human decision.
- [x] Make existing assembly entry a first-class route mode for Phase 1.
- [x] Keep graph state reference-oriented.
- [ ] Move reusable ledger/artifact/descriptor code toward the proven `rna_agent`
  contract shape.
- [x] Delay full requested-stage routing until dependency and skip semantics are
  explicit.

If a coding assistant reads an optimization direction and cannot map it to an
existing task file, the framework is not detailed enough for autonomous
implementation.

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。

## 6. Task Order

Follow these in order:

1. [x] [task1.md](task1.md) - Fix inventory review-gate routing.
2. [x] [task2.md](task2.md) - Add existing-assembly route intent and skip reasons.
3. [x] [task3.md](task3.md) - Decide and enforce requested-stage semantics for Phase 1.
4. [x] [task4.md](task4.md) - Reduce manifest payload stored in graph state.
5. [x] [task5.md](task5.md) - Fix provenance and empty-manifest policy.

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
