# Gene Agent

Gene Agent is a LangGraph-based workflow scaffold for ONT/CLR long-read genome
assembly and annotation projects.

The repository has a corrected Phase 1 inventory implementation and the first
Phase 2 executable slice for CycloneSEQ read QC. The current value is the
project shape, state contracts, stage contracts, descriptor roots, handoff
documentation, inventory, SeqKit stats execution boundary, genome survey
contracts, assembly candidate planning, and the assembly review human gate.

## Biological Backbone

```text
inventory -> read_qc -> genome_survey -> assembly -> assembly_review
-> hic_scaffolding -> repeat_annotation -> gene_annotation -> function_annotation
```

`genome_survey` plans a full k-mer survey only when low-error short DNA reads
exist; CycloneSEQ-only projects get an explicit low-confidence fallback record.
`hic_scaffolding` runs only when Hi-C assets exist and chromosome-level assembly
is requested.

## Read First

Start here if you are taking over the project:

1. [CLAUDE.md](CLAUDE.md)
2. [deep-research-report.md](deep-research-report.md)
3. [docs/handoff.md](docs/handoff.md)
4. [docs/governance.md](docs/governance.md)
5. [docs/architecture.md](docs/architecture.md)
6. [docs/stage_contracts.md](docs/stage_contracts.md)
7. [docs/phases/phase1/phase1.md](docs/phases/phase1/phase1.md)
8. [docs/phases/phase2/phase2.md](docs/phases/phase2/phase2.md)
9. [docs/phases/phase2/01_*.md through 11_*.md](docs/phases/phase2/)
10. [docs/phases/phase_completion_plan.md](docs/phases/phase_completion_plan.md)

## Current Implementation

Implemented:

- thin parent graph skeleton
- real inventory stage and a real `read_qc.long_read_stats` SeqKit stats slice
- real `genome_survey` full/fallback/skip contract stage
- real `assembly` candidate planning stage with Flye, hifiasm, NextDenovo, and
  Canu planning descriptors
- real `assembly_review` human gate with evaluation/review artifacts and ledger
  events
- placeholder downstream domain stage subgraphs after assembly review
- small `TopLevelState`
- asset, plan, descriptor, artifact, command, and event schema roots
- descriptor/config roots, including SeqKit, genome survey, assembly planning,
  and assembly review descriptors
- phase plan and handoff documentation
- Phase 1 inventory code for manifest loading, asset registry writing, ledger
  events, and CLI invocation
- minimal local subprocess runner for frozen attempts
- SeqKit stats artifact registration and ledger attempt events

Not implemented:

- executable bioinformatics method catalog beyond SeqKit stats
- RemoteRunner execution
- genome survey tool execution, assembly tool execution, scaffolding, repeat,
  gene, or function annotation execution
- complete review interrupt blocking/resume semantics
- production-grade requested-stage routing; the placeholder `--stages` flag is
  intentionally not exposed yet

Phase 1 correction notes are documented in
[docs/phases/phase1/phase1.md](docs/phases/phase1/phase1.md).

Phase 2 CycloneSEQ planning is documented in
[docs/phases/phase2/phase2.md](docs/phases/phase2/phase2.md). The first
executable slice, `read_qc.long_read_stats` with SeqKit stats, is implemented;
the same Phase 2 plan also defines the single-line contracts through function
annotation.

## Verification

Use the same environment and pycache prefix used during scaffold validation:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_manifest tests.test_inventory tests.test_entrypoints tests.test_project_scaffold tests.test_read_qc tests.test_genome_survey tests.test_assembly tests.test_assembly_review -v
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
