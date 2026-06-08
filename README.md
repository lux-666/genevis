# Gene Agent

Gene Agent is a LangGraph-based workflow scaffold for ONT/CLR long-read genome
assembly and annotation projects.

The repository implements the Phase 1 inventory gate, the Phase 2/3 CycloneSEQ
mainline contract through function annotation, and the Phase 4 real local
execution slices for the `inventory → read_qc → assembly → assembly_review`
mainline. Every real executable method dispatches through a shared conda env
(`env_geno_exec`) via `EnvResolver` + `env_dispatch` + `LocalRunner`, is opt-in
per stage, and produces structured blocked states (`blocked_input`,
`blocked_param`, `blocked_env`, `missing_tool`) rather than faking success.
`genome_survey`'s Jellyfish + GenomeScope2 local route is wired through the
same `env_dispatch` path as of Phase 5 task 01.

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
9. [docs/phases/phase3/README.md](docs/phases/phase3/README.md)
10. [docs/phases/phase4/README.md](docs/phases/phase4/README.md)
11. [docs/phases/phase_completion_plan.md](docs/phases/phase_completion_plan.md)

If you are setting this project up on a new machine, also read
[docs/deployment/migration.md](docs/deployment/migration.md) for the conda
env / install checklist.

## Current Implementation

Implemented:

- thin parent graph skeleton with full stage order and terminal-failure routing
- real `inventory` stage with manifest loading, asset registration, registry
  JSON artifact, absolutised asset paths, and ledger events
- real `read_qc` stage with SeqKit stats, optional `seqkit seq` min-length
  filtering, stable `long_reads.fofn` / `filtered_long_reads.fofn`, and
  `read_qc_summary.json`
- real `genome_survey` stage with full Jellyfish + GenomeScope2 opt-in
  execution, CycloneSEQ-only low-confidence fallback, and explicit skip record
- real `assembly` stage with deterministic candidate planning, opt-in local
  Flye execution (strict `flye_parameters` validation → `blocked_param`), and
  materialized `draft_assembly.fasta`
- real `assembly_review` stage with opt-in local QUAST, `quast_summary.json`,
  action-provenance artifacts for pass/purge/polish, and a human gate that
  does not invent thresholds
- real `hic_scaffolding` stage with opt-in local YaHS, scaffold/skip policy,
  pre-Hi-C assembly preservation, and contact-map human review block
- real `repeat_annotation` stage with opt-in local RepeatMasker for provided
  libraries and softmasked genome handoff
- `gene_annotation` with executable Miniprot + AUGUSTUS runners, EVM merge,
  BUSCO completeness signal, and review chain gates (method planning,
  candidate evidence, merge, final gene set); BRAKER3, MAKER, GeMoMa, and
  RNA-seq helpers remain planning-only
- `function_annotation` with executable DIAMOND blastp runner and
  database-provenance gating; InterProScan, eggNOG-mapper, KofamScan
  remain planning-only
- shared `EnvResolver` + `env_dispatch`: method/stage/default precedence,
  `conda run` dispatch, and preflight that emits `blocked_env` / `missing_tool`
  before any biological command runs; wired into `read_qc`, `assembly`,
  `assembly_review`, `hic_scaffolding`, and `repeat_annotation`
- CLI `gene-agent run --manifest ... [--local-exec STAGE] [--run-config PATH]`
  for structured local execution; repeatable `--local-exec` and JSON run-config
  seed only approved `stage_outputs` fields and are validated before graph
  invocation
- CLI `gene-agent ask "..."` natural-language entry that materialises a
  manifest from source files/folders and reuses the structured `run` path
- CLI `gene-agent inspect --session-id ...` reads durable ledger/artifact
  records, supports `--json`, surfaces next actions, and can write review
  decision-file templates with `--write-decision-template PATH`
- structured review resume for `assembly_review` and `hic_scaffolding`,
  including durable sqlite cross-process resume via `gene-agent resume
  --decision-file ... --checkpointer sqlite`
- blocked and failed terminal sessions write lightweight operator report
  artifacts through the artifact registry
- minimal `LocalRunner` that dispatches through `conda run` when a frozen
  attempt carries `conda_env` / `conda_prefix`

Not implemented:

- in-agent remote execution (intentionally unsupported; see
  [`docs/deployment/mac_dev_server_pull.md`](docs/deployment/mac_dev_server_pull.md))
- executable bioinformatics methods beyond the slices above (Merqury,
  minimap2 coverage, purge_dups, contamination check; NextDenovo, Canu,
  hifiasm; Juicer/3D-DNA/Juicebox and raw Hi-C alignment; EDTA, RepeatModeler,
  TRF, de novo repeat library; BRAKER3, MAKER, HISAT2,
  StringTie, IsoQuant, GeMoMa; InterProScan, eggNOG-mapper, KofamScan)
- requested-stage routing (the `--stages` flag is intentionally not exposed)
- lifecycle operations beyond structured review resume, such as retry, abort,
  rebuild, or requested-stage reruns
- automatic annotation database downloads

## Operator Inspect / Resume

Use absolute `--data-dir` values on the server/HPC checkout so ledger,
checkpoints, and artifacts resolve to the same run directory:

```bash
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data
```

```bash
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --json
```

### Interactive TUI

The guided TUI helps assemble parameters and handles human gates automatically:

```bash
gene-agent tui --data-dir /absolute/path/to/data
```

It offers two modes: assembly from raw reads (full pipeline) or annotation from
an existing assembly (skips read_qc/genome_survey/assembly). When the graph
blocks at a human gate, the TUI prompts for the decision and resumes in-process.

When running `gene-agent run` directly in a TTY, human gates are also handled
interactively without exiting the process.

### Scripted / automation workflow

For a blocked `assembly_review` or `hic_scaffolding` gate, generate and edit a
structured decision file, then resume against the same sqlite checkpoint store:

```bash
gene-agent inspect --session-id <SESSION> --data-dir /absolute/path/to/data --write-decision-template /absolute/path/to/decision.json
gene-agent resume --session-id <SESSION> --data-dir /absolute/path/to/data --decision-file /absolute/path/to/decision.json --checkpointer sqlite
```

Or use the interactive review command to inspect, decide, and resume in one step:

```bash
gene-agent review --session-id <SESSION> --data-dir /absolute/path/to/data
```

The `review` command shows the blocked gate, allowed decisions, candidate refs,
and report refs, then prompts the operator to choose. Use `--dry-run` to
validate and write the decision artifact without resuming.

The default checkpointer is sqlite unless overridden by `--checkpointer` or
`GENE_AGENT_CHECKPOINTER`; memory checkpoints are for tests and do not support
cross-process resume.

## Verification

Use the same environment and pycache prefix used during scaffold validation:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
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
