# Gene Agent

Gene Agent is a LangGraph-based workflow scaffold for ONT/CLR long-read genome
assembly and annotation projects.

The repository is currently in **Phase 0: scaffold and contracts**. It is not
yet a production bioinformatics runner. The current value is the project shape,
state contracts, stage contracts, descriptor roots, and handoff documentation
needed to implement the pipeline phase by phase.

## Biological Backbone

```text
inventory -> read_qc -> genome_survey -> assembly -> assembly_review
-> hic_scaffolding -> repeat_annotation -> gene_annotation -> function_annotation
```

`genome_survey` runs only when low-error short DNA reads exist. `hic_scaffolding`
runs only when Hi-C assets exist and chromosome-level assembly is requested.

## Read First

Start here if you are taking over the project:

1. [CLAUDE.md](CLAUDE.md)
2. [deep-research-report.md](deep-research-report.md)
3. [docs/handoff.md](docs/handoff.md)
4. [docs/governance.md](docs/governance.md)
5. [docs/architecture.md](docs/architecture.md)
6. [docs/stage_contracts.md](docs/stage_contracts.md)
7. [docs/phases/phase_completion_plan.md](docs/phases/phase_completion_plan.md)

## Current Implementation

Implemented:

- thin parent graph skeleton
- nine placeholder domain stage subgraphs
- small `TopLevelState`
- asset, plan, descriptor, artifact, command, and event schema roots
- descriptor/config roots
- phase plan and handoff documentation

Not implemented:

- real manifest parsing
- real asset registry writing
- real bioinformatics method descriptors
- LocalRunner or RemoteRunner execution
- review interrupts
- production CLI

## Verification

Use the same environment and pycache prefix used during scaffold validation:

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

## Reference Project

`/Users/a1/Documents/BGIwork/rna_agent` is the reference for engineering
patterns: thin parent graph, stage subgraphs, descriptor contracts, forge,
runner, ledger, review, and reporting boundaries. It is not the biological
source of truth for this project.
