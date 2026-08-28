# New Host Setup

This guide provisions a fresh Gene Agent checkout. It owns installation and
host readiness only. Starting, inspecting, and resuming sessions belongs to
[Run and Recovery](../user_guide/run-and-recovery.md); the ongoing Mac/server
Git workflow belongs to [Mac Development and Server Execution](mac_dev_server_pull.md).

The three deployment entry points and their non-overlapping scopes are indexed
in [Deployment Entry Points](README.md).

## Prerequisites

- Conda or Mamba on `PATH`;
- Python 3.11 or newer;
- enough CPU, memory, storage, and wall time for the selected methods;
- server-local bioinformatics databases, BUSCO lineages, packages, and models.

GPU is not a blanket contig requirement. It is required only when a selected
method or external package explicitly needs it.

## What Git carries

Git carries application code, `pyproject.toml`, descriptors, tests, current
documentation, schema-valid examples, and helper scripts.

The following remain machine-local:

- `.env` and credentials;
- active `config/envs.yaml` (the committed seed is `docs/examples/envs.yaml.example`);
- Conda environments and external package installations;
- BUSCO lineages, databases, and model weights;
- real FASTQ/FASTA/BAM inputs and private run configs;
- runtime data, checkpoints, Ledger, reports, and artifacts.

## Install the orchestrator

```bash
conda create -n gene_agent python=3.11 -y
conda activate gene_agent
python -m pip install -e '.[dev]'
```

The quotes around `'.[dev]'` prevent shell glob expansion. Python runtime and
development dependencies are declared only in [`../../pyproject.toml`](../../pyproject.toml).
`gene_agent` is the documented orchestrator-environment convention; it is not
read from `config/envs.yaml`. All commands below assume it remains activated.

## Verify the code checkout

Run the code-only gate before installing biological tools or editing host paths:

```bash
bash /path/to/gene_agent/scripts/server_test.sh
```

It uses the currently activated Python and never selects another Conda
environment, invokes Node/npm, or downloads packages.

## Provision the contig tool set

The checked-in five-stage contig example deliberately does not bind an
assembler. The following shared environment covers every method declared by
the generic host seed except the optional AutoHiC candidate:

```bash
# Package name matters: conda-forge::jellyfish is an unrelated Python string-
# matching library and does not provide the required executable.
conda create -n env_geno_exec \
  --channel conda-forge \
  --channel bioconda \
  --strict-channel-priority \
  seqkit \
  kmer-jellyfish \
  genomescope2 \
  flye \
  nextdenovo \
  hifiasm \
  minimap2 \
  purge_dups \
  gzip \
  quast \
  busco \
  -y
```

This command is a provisioning convenience, not the requirement authority.
`scripts/server_check.sh` derives the fixed-stage method closure from
`docs/examples/contig_run_config.template.json`, resolves it through the active
host mapping, and checks every command/package requirement. In particular,
`purge_dups` must expose all of its helper commands; package installation alone
does not prove readiness.

Assembly chooses its method after upstream evidence exists. Declare and install
every Assembly candidate that a run may select on this host; `bootstrap` checks
that declared catalog. The selected candidate receives a second,
stage-time preflight from the frozen dependency snapshot before execution.
AutoHiC, Juicer, 3D-DNA, their models, and biological databases are external
resources and remain separate from this Conda environment.

## Conditional services and data

Create a private `.env` for configured LLM stages, for example:

```dotenv
GENE_AGENT_LLM_PROVIDER=openai_compatible
GENE_AGENT_LLM_BASE_URL=...
GENE_AGENT_LLM_API_KEY=...
GENE_AGENT_LLM_MODEL=...
GENE_AGENT_LLM_TEMPERATURE=0
GENE_AGENT_LLM_MAX_TOKENS=16000
GENE_AGENT_LLM_TIMEOUT_SECONDS=180
```

Do not commit it. Assembly method and parameter recommendation requires a
reachable LLM.
Assembly Review additionally requires `user_metadata.target_species`, the NCBI
snapshot root, and a compatible installed BUSCO lineage; runtime selection is
defined by the
[Assembly Review stage contract](../architecture/reference/stage-contracts.md#6-assembly-review).
AutoHiC additionally needs its declared external package roots,
Juicer/3D-DNA/JAR layout, and model files.

Install the NCBI snapshot through its single
[snapshot authority](../../data/reference/ncbi/README.md). Install
AutoHiC/Juicer/3D-DNA/JAR/model resources only when this host will expose that
capability. A skipped optional capability can be added to the host mapping
later.

## Configure the active host mapping

Execution reads `GENE_AGENT_ENVS_CONFIG` when set, otherwise
`config/envs.yaml`. Use an absolute override when commands may start in
different directories. `docs/examples/envs.yaml.example` is only the generic
bootstrap seed, never a second runtime input.

The active mapping may be written manually or with the optional assistant:

```bash
gene-agent bootstrap
```

On a fresh host, bootstrap can create the core Conda mapping before databases or
AutoHiC are installed: answer no for either optional group. After installing a
skipped resource, either add its complete YAML group manually or rerun ordinary
`gene-agent bootstrap` and choose to configure it. `bootstrap --update` replaces
selected values that are already managed; it is not required for adding an
absent optional group. Every assisted change prints one exact plan and waits for
an empty Enter confirmation before writing it.

Manual edits are equally valid. When copying the generic seed by hand, replace
all retained placeholders and delete any optional group that is not yet
configured; do not leave a half-filled database or package group. Skipping
AutoHiC means removing its four package entries and
`methods.hic_scaffolding.candidate.autohic` together.

When the databases are installed, fill the single `databases:` block: register
installed BUSCO lineage aliases and the NCBI snapshot root. Per-run RunConfig
does not repeat database paths, versions, provenance, or lineage selection. For
Assembly Review, the manifest supplies the NCBI Latin scientific name in
`user_metadata.target_species`; the system derives lineage from the frozen NCBI
snapshot selected from that root and the installed host registry. A RunConfig
`assembly_review.busco.lineage`, when present, is consistency-only.

The NCBI installer resolves its root destination from the active mapping and
writes one named child directory. If using it before the root exists, manually
add the intended root first, run the installer, and then use bootstrap or
`--verify-only` to validate the completed installation.

Bootstrap does not install arbitrary packages or Conda environments. It
validates package canaries, performs the bounded declared Juicer conformance,
and probes every declared method entry in the active mapping. The precise
boundary is maintained in
[Project Bootstrap](../architecture/topics/bootstrap.md).

Bootstrap catalog validation is not a substitute for the contig-specific
database and tool checks below.

## Verify host readiness

The readiness script resolves the repository root from its own file location,
so it may be invoked from any current directory:

```bash
bash /path/to/gene_agent/scripts/server_check.sh
```

Scope:

- `server_check.sh`: orchestrator prerequisites plus the fixed contig template
  tool/package/database closure; deferred Assembly-candidate readiness is
  stage-owned;
- `server_test.sh`: earlier Python regression, compile, and diff gate;
- real-data smoke: an explicit operator run using server-local inputs, described
  in [Run and Recovery](../user_guide/run-and-recovery.md).

Do not call either gate successful unless it exits zero and prints its expected
completion evidence.
