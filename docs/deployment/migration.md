# Migration Checklist (New Machine)

First-time setup checklist for moving Gene Agent to a new host. This file
does not change behavior; it consolidates the install surface already
implied by `pyproject.toml`, `config/envs.yaml`, and the package
descriptors, so you can stand the project up without reading them all.

For the ongoing Mac-dev / server `git pull` workflow once both hosts are
already provisioned, see [`mac_dev_server_pull.md`](mac_dev_server_pull.md).

## Scope

After `git clone` on a new host you need:

- `conda` on PATH (Miniconda / Mambaforge), Python 3.11+ available through
  the orchestrator env.
- Two conda envs: `gene_agent` (orchestrator) and `env_geno_exec` (tools).
- Optional `.env` for `gene-agent ask` (natural-language entry).

## What Git Carries vs Does Not Carry

Tracked: application code, descriptors, configs, tests, docs, manifest /
run-config templates, server helper scripts.

Not tracked (must be (re)created or supplied locally; see `.gitignore`):

- `.env` (LLM credentials)
- `data/` (manifests, ledgers, artifacts, checkpoints, smoke configs)
- conda envs themselves
- bioinformatics databases (RepeatMasker libraries, InterProScan, ...)
- `app/entrypoints/workflow.ipynb` and `docs/beginner_mainline_guide.md`

## Two Conda Envs

`config/envs.yaml` decides which env each method runs in. Defaults:

| Env name | Role | When required |
|---|---|---|
| `gene_agent` | Python orchestrator (LangGraph, CLI) | always |
| `env_geno_exec` | All bioinformatics tools (default + every stage / method) | when running real local execution |

Both names are config knobs. If you need different names on the new host,
edit `config/envs.yaml` (named env) or switch its `conda_env:` entries to
`conda_prefix:` absolute paths. `scripts/server_*.sh` also hardcode these
two names — search and replace if you change them.

### `gene_agent` env (orchestrator)

```bash
conda create -n gene_agent python=3.11 -y
conda run -n gene_agent pip install -e .[dev]
```

`pyproject.toml` declares the runtime deps (`langgraph`,
`langgraph-checkpoint`, `langgraph-checkpoint-sqlite`, `langchain-core`,
`langchain-openai`, `pydantic`, `pyyaml`) and the dev extras (`pytest`,
`ruff`, `mypy`). The optional `proxy` extra (`fastapi`, `httpx`,
`uvicorn`) is unused by the current mainline; install only if you need it.

Phase 5/02 onward, `langgraph-checkpoint-sqlite` is required even for a
local-only run because `gene-agent run` defaults to the durable sqlite
checkpoint backend. `scripts/server_check.sh` verifies the import is
available and fails fast with an actionable message if `pip install -e .`
was skipped.

### `env_geno_exec` env (tools)

This env hosts every binary the workflow can actually invoke today, plus
the descriptors for tools whose stages are still planning-only. Channels:
`bioconda` + `conda-forge`.

#### Wired into the executable mainline — install these for Phase 4 / 5 acceptance

| Tool | Binary used by attempt scripts | Stages |
|---|---|---|
| seqkit ≥ 2.0 | `seqkit` | `read_qc` |
| jellyfish ≥ 2.3 | `jellyfish` | `genome_survey` |
| genomescope2 ≥ 2.0 | `genomescope.R` | `genome_survey` |
| flye ≥ 2.9 | `flye` | `assembly` |
| quast ≥ 5.2 | `quast.py` | `assembly_review` |
| yahs ≥ 1.2 | `yahs` | `hic_scaffolding` |
| repeatmasker ≥ 4.1 | `RepeatMasker` | `repeat_annotation` |

```bash
conda create -n env_geno_exec -y
conda install -n env_geno_exec -c bioconda -c conda-forge -y \
  seqkit jellyfish genomescope2 flye quast yahs repeatmasker
```

Notes:

- The `genomescope2` conda package installs an R wrapper exposed as
  `genomescope.R`, not a binary literally named `genomescope2`.
  `config/envs.yaml` lists `genomescope.R` as the required tool to match.
- `RepeatMasker` ships without a default repeat library on conda.
  Configure its library separately if you opt into the local
  `repeat_annotation` route; the workflow already requires explicitly
  registered libraries.
- Missing tools surface structured `blocked_env` / `missing_tool` states
  via `EnvResolver` preflight; they do not silently fail.

#### Descriptor-only — install when their stage gets a real execution path

These have package descriptors but no execution code yet:

- assembly: `nextdenovo`, `canu`, `hifiasm`
- assembly_review: `busco`, `merqury`, `meryl`, `minimap2`, `purge_dups`
- read_qc length filters: `chopper`, `fastplong`, `filtlong`
- genome_survey: `smudgeplot`
- repeat_annotation: `edta`, `repeatmodeler`, `trf`
- gene_annotation: `braker3`, `maker`, `augustus`, `miniprot`, `hisat2`,
  `stringtie`, `isoquant`, `gemoma`
- function_annotation: `interproscan`, `eggnog_mapper`, `diamond`,
  `kofamscan`

`docs/handoff.md` "Not implemented" tracks this catalogue authoritatively.

#### Not on conda

Marked `install_channel: container` in their descriptors and need
container / upstream installs (not a Phase 4 / 5 mainline concern):

- `juicer`, `juicebox`, `3ddna`

## Absolute Paths That Reference The Original Mac

Search-and-replace targets when standing the new host up:

- `README.md`, `docs/handoff.md`, `docs/governance.md`,
  `docs/architecture.md`, and several `docs/phases/phase*/...` files
  reference `/Users/a1/Documents/BGIwork/rna_agent` (a sibling reference
  project, not a runtime dependency). Update or drop these mentions on the
  new host as needed.
- `data/phase4_real_smoke/manifest.yaml` carries an absolute path to the
  Phase 4 smoke FASTA. `data/` is gitignored, so this file does not
  migrate with git; recreate it from
  `docs/examples/phase4_real_manifest.template.yaml`.
- `.claude/settings.local.json` carries Mac-specific permission entries;
  it is local to the original Claude Code session and does not need to
  migrate.

The `/opt/conda/envs/...` strings inside `config/envs.yaml` are inline
comments showing the alternative `conda_prefix:` form, not active paths.

## Optional: `.env` for `gene-agent ask`

`gene-agent ask` reads LLM credentials from `GENE_AGENT_LLM_*` env vars
and from a project-root `.env` (gitignored). `gene-agent run` and
`gene-agent inspect` do not need these. Create `.env` only when you
intend to use `ask`:

```dotenv
GENE_AGENT_LLM_PROVIDER=openai_compatible
GENE_AGENT_LLM_BASE_URL=...
GENE_AGENT_LLM_API_KEY=...
GENE_AGENT_LLM_MODEL=...
GENE_AGENT_LLM_TEMPERATURE=0
GENE_AGENT_LLM_MAX_TOKENS=16000
GENE_AGENT_LLM_TIMEOUT_SECONDS=180
```

## Verification After Setup

```bash
bash scripts/server_check.sh
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m compileall app tests
```

A passing `server_check.sh` confirms both envs exist and seqkit / flye /
quast.py are present; a passing test suite confirms Python-side wiring.
The Phase 4 real-data smoke (`scripts/server_phase4_smoke.sh`) is the
final acceptance step; see `mac_dev_server_pull.md` for inputs.
