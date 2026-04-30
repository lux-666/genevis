# Agent Handoff

This is the first file a future agent should read when taking over this
repository.

## Current Status

The project is in Phase 0.

Completed:

- Repository scaffold exists.
- Parent graph placeholder can route through all genome domain stages.
- Shared schema contracts exist for state, assets, plans, descriptors, artifacts,
  commands, and events.
- Descriptor roots and initial ONT/CLR asset policy exist.
- Documentation now defines architecture, governance, stage contracts, descriptor
  rules, and phase plan.
- Structure tests cover stage order, initial state shape, and required document
  roots.

Not completed:

- No real input manifest parser.
- No real asset registry writer.
- No real method descriptors for bioinformatics tools.
- No LocalRunner or RemoteRunner execution.
- No review interrupt implementation.
- No real CLI beyond a placeholder message.

## Read Order

Read these files in order:

1. `CLAUDE.md`
2. `deep-research-report.md`
3. `docs/governance.md`
4. `docs/architecture.md`
5. `docs/stage_contracts.md`
6. `docs/descriptor_spec.md`
7. `docs/phases/phase_completion_plan.md`
8. This file again

Use `/Users/a1/Documents/BGIwork/rna_agent` as a reference for patterns only.
Do not copy single-cell business names into this project.

## Verification Commands

Use the conda environment already used for scaffold verification:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_project_scaffold -v
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

Start Phase 1: Parent Graph and Inventory MVP.

Recommended first slice:

1. Add tests for an input manifest schema.
2. Implement `InputManifest` and `InputAssetSpec` in `app/schemas/assets.py` or a
   new `app/schemas/manifest.py`.
3. Implement lightweight manifest loading in `app/stages/inventory/nodes.py`.
4. Register assets into a JSON artifact under `data/artifacts/asset_registry/`.
5. Write a ledger event through `app/control/ledger.py`.
6. Keep the parent graph shape unchanged.

Do not start with Flye, NextDenovo, or runner execution. Inventory is the
foundation for every later planner decision.

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
- Placeholder stages always advance. Phase 1 must replace this behavior for
  `inventory`.
- The parent graph currently reaches `done` even with a fake manifest. That is
  acceptable for scaffold smoke only.
- `LocalRunner` and `RemoteRunner` intentionally raise `NotImplementedError`.
- Review gates are documented but not implemented.
- No `.gitignore` exists yet; avoid committing cache or data outputs.

## Files Most Likely To Change In Phase 1

- `app/schemas/assets.py`
- `app/schemas/state.py`
- `app/stages/inventory/nodes.py`
- `app/stages/inventory/builder.py`
- `app/stages/inventory/routing.py`
- `app/control/ledger.py`
- `app/entrypoints/run.py`
- `tests/test_project_scaffold.py`
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
