# New Host Setup

This guide provisions the current Gene Agent checkout on a new machine. For the
ongoing Mac-development/server-execution workflow after provisioning, use
[`mac_dev_server_pull.md`](mac_dev_server_pull.md).

## Prerequisites

- Conda or Mamba on `PATH`.
- Python 3.11 or newer for the orchestrator.
- A `gene_agent` orchestrator environment.
- A bioinformatics environment matching `config/envs.yaml` (default:
  `env_geno_exec`).
- Server-local databases, model files, and external package installations for
  the stages that will run.

## What Git Carries

Git contains application code, descriptors, configuration defaults, tests,
current documentation, examples, and server helper scripts.

The following remain machine-local:

- `.env` and credentials;
- runtime `data/`, checkpoints, ledger, reports, and artifacts;
- Conda environments;
- bioinformatics databases and BUSCO lineages;
- real input files and run configs containing private paths;
- AutoHiC, Juicer, 3D-DNA, Juicer Tools, and model installations.

## Orchestrator Environment

```bash
conda create -n gene_agent python=3.11 -y
conda run -n gene_agent pip install -e .[dev]
```

The runtime dependencies and development tools are declared in
`pyproject.toml`. Durable CLI runs require the SQLite checkpoint package that is
installed with the project dependencies.

## Bioinformatics Environment

`config/envs.yaml` maps stages and methods to named Conda environments or
absolute prefixes. The default shared environment must provide the tools used
by the methods you intend to execute.

| Stage | Current command requirements |
|---|---|
| Read QC | `seqkit` |
| Genome Survey | `jellyfish`, `genomescope2` |
| Assembly | `flye`, `hifiasm`, `nextDenovo` |
| Assembly Dedup | `minimap2`, `samtools`, `pbcstat`, `calcuts`, `split_fa`, `gzip`, `purge_dups`, `get_seqs` |
| Assembly Review | `quast.py`, `busco` |
| T2T | `minimap2` |
| Repeat Annotation | `RepeatMasker` |
| Gene Annotation | `augustus`, `miniprot`, `gffread`, `EVidenceModeler`, `busco` |
| Function Annotation | `diamond` |

Install the required subset with Bioconda/Conda Forge or the deployment method
appropriate to the server. Missing commands produce structured preflight
failures; the workflow does not fabricate success.

## AutoHiC Packages

AutoHiC uses its configured environment plus external package roots. Update the
`packages` entries in `config/envs.yaml` for:

- AutoHiC;
- Juicer;
- 3D-DNA;
- the directory containing `juicer_tools.jar`;
- AutoHiC error and chromosome model paths.

Inspect the plan first, then apply approved configuration/conformance changes:

```bash
conda run -n gene_agent gene-agent bootstrap
conda run -n gene_agent gene-agent bootstrap --apply
```

Bootstrap validates current configuration, conforms packages that opt in, and
checks their declared canary paths. It performs no downloads.

## Optional LLM Configuration

TUI parameter recommendation and model-assisted review read credentials from
exported `GENE_AGENT_LLM_*` variables or the project-root `.env` file.
`inspect` and storage audit do not require an LLM.

Example `.env`:

```dotenv
GENE_AGENT_LLM_PROVIDER=openai_compatible
GENE_AGENT_LLM_BASE_URL=...
GENE_AGENT_LLM_API_KEY=...
GENE_AGENT_LLM_MODEL=...
GENE_AGENT_LLM_TEMPERATURE=0
GENE_AGENT_LLM_MAX_TOKENS=16000
GENE_AGENT_LLM_TIMEOUT_SECONDS=180
```

Do not commit this file.

## Current Templates

Schema-valid templates live in `docs/examples/`:

- `real_data_manifest.template.yaml`;
- `read_qc_assembly_review_run_config.template.json`;
- `busco_run_config.template.json`;
- `existing_assembly_repeatmasker_manifest.template.yaml`;
- `existing_assembly_provided_annotation_manifest.template.yaml`;
- `repeatmasker_run_config.template.json`;
- `e2e_manifest.template.yaml`;
- `e2e_run_config.template.json`.

Copy a template to a server-local directory and replace placeholder paths. Do
not commit private paths or real sample metadata.

For the two assembly examples, the operator-provided genome-size estimate lives
in the paired manifest. The run config contains execution choices only.

## Verification

```bash
bash scripts/server_check.sh
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m compileall app tests
```

Run real-data smoke scripts only from the provisioned server checkout and with
explicit server-local inputs.
