# 01 Inventory CycloneSEQ FASTQ

Task status: implemented.

## Goal

Make CycloneSEQ downsampled or production FASTQ / FASTQ.gz a first-class
inventory input that becomes `raw_long_reads`.

This task starts the real Phase 2 line. It does not run QC tools yet.

## Files

- Modify: `app/schemas/manifest.py`
- Modify: `app/stages/inventory/nodes.py`
- Modify: `app/stages/inventory/routing.py` only if route intent needs a small
  CycloneSEQ-specific reason
- Test: `tests/test_manifest.py`
- Test: `tests/test_inventory.py`
- Fixtures: add tiny FASTQ / FASTQ.gz files only if current fixtures do not cover
  CycloneSEQ platform metadata

## Project Context

Inventory is the only real Phase 1 business stage today. The current
`app/stages/inventory/nodes.py` already has:

- `_load_manifest()`
- `validate_inputs()`
- `register_assets()`
- `_build_asset_records()`
- `_entry_mode()`
- `commit_inventory()`

This task should extend that path instead of replacing it. The important local
contract is that `register_assets()` writes the asset registry JSON artifact and
stores only `asset_registry_ref` in graph state.

## Node Plan

Keep the existing inventory subgraph shape:

```text
enter_inventory
-> validate_inputs
-> review_gate or register_assets
-> commit_inventory
```

Implementation work should be local to:

- `validate_manifest()` and schema validation for CycloneSEQ platform/metadata;
- `_build_asset_records()` so CycloneSEQ FASTQ becomes `raw_long_reads`;
- inventory review routing only if ambiguous platform metadata needs a new
  warning/reason.

Do not add a separate CycloneSEQ inventory node unless validation logic becomes
too large for `validate_manifest()`.

## Reuse From `rna_agent`

Reusable engineering patterns:

- small state plus artifact refs;
- append-only ledger event for registered assets;
- tests that prove schema behavior before graph routing behavior.

Do not copy:

- h5ad input resolution;
- single-cell dataset fields;
- RNA-specific intent parsing.

## Optimization Notes

- Keep platform inference conservative: metadata beats filename heuristics.
- If CycloneSEQ metadata is missing but FASTQ is otherwise valid, prefer a
  review warning over silent platform invention.
- Keep expensive file checks out of inventory; large FASTQ checksum work belongs
  in a later artifact/runner layer.

## Input Contract

User input:

- FASTQ / FASTQ.gz paths in a YAML/JSON manifest
- platform or metadata indicating BGI CycloneSEQ when available

Accepted suffixes:

- `.fastq`
- `.fq`
- `.fastq.gz`
- `.fq.gz`

Inventory output:

- registered asset type: `raw_long_reads`
- `asset_registry_ref` in graph state
- inventory ledger events

## Tasks

- [x] Add or confirm manifest schema accepts CycloneSEQ as a long-read genomic
  platform value.
- [x] Add a manifest fixture with CycloneSEQ FASTQ / FASTQ.gz input.
- [x] Add a test proving CycloneSEQ FASTQ registers as `raw_long_reads`.
- [x] Add a test proving ambiguous genomic long reads versus long RNA reads still
  stop at inventory review.
- [x] Add a test proving existing assembly input still routes to
  `assembly_review` and is not broken by CycloneSEQ metadata.
- [x] Keep graph state reference-oriented: `input_manifest_ref`,
  `asset_registry_ref`, summaries, warnings, and errors only.

## Implemented State

- `app/schemas/manifest.py` accepts `platform: cycloneseq`.
- `_detect_platform()` infers CycloneSEQ from filenames containing
  `cycloneseq`, `cyclone_seq`, or `cyclone-seq`.
- `_infer_asset_properties()` also infers CycloneSEQ from explicit metadata such
  as `technology: BGI CycloneSEQ`.
- Inventory registers CycloneSEQ FASTQ / FASTQ.gz as `raw_long_reads` and keeps
  registry/state behavior unchanged.
- Existing ambiguous read review behavior and existing assembly routing remain
  covered by inventory tests.

## Non-Goals

- Do not run SeqKit, NanoPlot, pyfastx, Jellyfish, or assembly tools here.
- Do not compute expensive checksums synchronously for large FASTQ files.
- Do not decide whether CycloneSEQ is ONT-like, CLR-like, or its own descriptor
  profile beyond inventory recognition.
- Do not reintroduce `--stages`.

## Acceptance Criteria

- A CycloneSEQ FASTQ / FASTQ.gz manifest produces `raw_long_reads`.
- Ambiguous long-read input still blocks for human review.
- Existing assembly routing remains intact.
- Phase 1 inventory tests still pass.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

明确要求：coding助手读到了应寻求人类帮助，而不是自己去构建具体方案。
