# Agent Handoff

This is the first file a future agent should read when taking over this
repository.

## Current Status

The project has a Phase 1 implementation commit:

```text
fad12d3 Implement Phase 1: Parent Graph and Inventory MVP
```

The Phase 1 correction pass is captured in `docs/phases/phase1/phase1.md`, with
task details under `docs/phases/phase1/`.

Completed:

- Repository scaffold exists.
- Parent graph routes through all genome domain stages and handles terminal failures.
- Shared schema contracts exist for state, assets, plans, descriptors, artifacts,
  commands, events, and manifests.
- Descriptor roots and initial ONT/CLR asset policy exist.
- Documentation now defines architecture, governance, stage contracts, descriptor
  rules, and phase plan.
- Structure tests cover stage order, initial state shape, and required document
  roots.
- **Input manifest schema** (`InputManifest`, `InputAssetSpec`) validates asset
  types, file existence, format, compression, platform, and layout.
- **Inventory stage** loads manifests, validates inputs, registers assets, writes
  the asset registry JSON artifact, and records ledger events.
- Inventory routing supports success, failure, and review-required paths.
- Ambiguous long-read vs long-RNA inputs trigger an `active_interrupt` and stop
  at inventory.
- Existing assembly inputs route to `assembly_review` and publish skip reasons.
- CLI `gene-agent run --session-id ... --manifest ...` executes the full graph.
- The misleading `--stages` CLI flag is intentionally not exposed.

Not completed:

- No real method descriptors for bioinformatics tools.
- No LocalRunner or RemoteRunner execution.
- Review interrupt resume logic is not implemented.

## Read Order

Read these files in order:

1. `CLAUDE.md`
2. `deep-research-report.md`
3. `docs/governance.md`
4. `docs/architecture.md`
5. `docs/stage_contracts.md`
6. `docs/descriptor_spec.md`
7. `docs/phases/phase1/phase1.md`
8. `docs/phases/phase1/task1.md`
9. `docs/phases/phase_completion_plan.md`
10. This file again

Use `/Users/a1/Documents/BGIwork/rna_agent` as a reference for patterns only.
Do not copy single-cell business names into this project.

## Verification Commands

Use the conda environment already used for scaffold verification:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold -v
```

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
```

Graph smoke:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -c "from app.graph.builder import build_graph; from app.schemas.state import initial_state; g=build_graph(); s=initial_state(session_id='smoke', input_manifest_ref='inputs/manifest.yaml'); r=g.invoke(s, config={'configurable': {'thread_id': 'smoke'}}); print(r['current_stage']); print(r['route_trace'])"
```

Expected graph smoke output:

```text
done
['inventory_stage', 'read_qc_stage', 'genome_survey_stage', 'assembly_stage', 'assembly_review_stage', 'hic_scaffolding_stage', 'repeat_annotation_stage', 'gene_annotation_stage', 'function_annotation_stage']
```

The `PYTHONPYCACHEPREFIX` is important in this sandbox because default Python
bytecode cache writes may try to use `~/Library/Caches`, which is not writable.

## Next Best Task

Start Phase 2 only after confirming the Phase 1 correction commit is present on
the feature branch. The next implementation slice should stay narrow: read QC
planning based on the asset registry, without running heavy bioinformatics
tools.

## Immediate Design Decisions Already Made

- Stage order is fixed in `app/stages/__init__.py`.
- `genome_survey` exists as a stage but must be skippable.
- `hic_scaffolding` exists as a stage but must be skippable.
- Assembly review is the main human-gated decision point.
- `TopLevelState` should stay small and reference-based.
- Real method execution must go through descriptors, frozen attempt specs, and
  runners.
- Documentation is part of the product, not an afterthought.

## Known Risks

- `DescriptorRegistry.load()` is scaffold-level only. It validates package/method
  references but does not yet validate asset compatibility.
- Only `inventory` has real routing; all other stages are still placeholders that
  always advance.
- `LocalRunner` and `RemoteRunner` intentionally raise `NotImplementedError`.
- Review interrupts are set but there is no resume / `ResumeInterrupt` logic yet.
- Requested-stage routing is not implemented; the CLI flag is intentionally not
  exposed.

## Files Most Likely To Change In Phase 2

- `app/schemas/plan.py`
- `app/stages/read_qc/nodes.py`
- `app/stages/read_qc/builder.py`
- `app/stages/read_qc/routing.py`
- `app/stages/genome_survey/nodes.py`
- `app/stages/genome_survey/routing.py`
- `config/defaults.yaml`
- `descriptors/packages/` and `descriptors/methods/`
- new tests under `tests/`
- docs touched when behavior changes

## Stop Conditions

Stop and ask the user before:

- changing the stage order
- removing any documented stage
- introducing a new top-level persistence backend
- adding a heavy external dependency
- making destructive filesystem or git changes
- running real bioinformatics tools on large data

## Handoff Update Rule

Before ending any substantial work session, update these sections:

- Current Status
- Next Best Task
- Known Risks
- Verification Commands, if commands changed
