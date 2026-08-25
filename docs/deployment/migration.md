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
conda run -n gene_agent pip install -e '.[dev]'
```

The quotes around `'.[dev]'` prevent shell glob expansion. Python runtime and
development dependencies are declared only in [`../../pyproject.toml`](../../pyproject.toml).

## Configure the active host mapping

Execution reads the active environment mapping: `GENE_AGENT_ENVS_CONFIG` when
set, otherwise `config/envs.yaml`. Use an absolute override when invoking tools
from different current directories; relative overrides resolve from the caller's
current directory, while the default stays checkout-anchored.
`docs/examples/envs.yaml.example` is the generic
bootstrap seed; it is not a second active resolver input. Inspect the proposed
changes first and write only after review:

```bash
conda run -n gene_agent gene-agent bootstrap
conda run -n gene_agent gene-agent bootstrap --apply
```

Fill the single `databases:` block in that active file once: register available
BUSCO lineage aliases (including one default) and the NCBI Metazoa snapshot.
Per-run RunConfig files do not repeat database paths, versions, or provenance.
For Assembly Review, a RunConfig may only select a registered BUSCO alias with
`assembly_review.busco.lineage` and set executor knobs such as `threads`; host
database identity and location remain in this active mapping and other fields
are rejected.
Running bootstrap interactively against a pre-database `envs.yaml` now proposes
this migration instead of treating the missing block as an intentionally empty
catalog.

Bootstrap does not install arbitrary packages or Conda environments. It
initializes/validates host mappings, verifies declared package canaries, performs
the bounded Juicer conformance action when configured, and probes every method
entry declared in the active mapping. That declared-catalog scope can be broader
than the fixed-stage contig check below.

The precise bootstrap boundary is maintained in
[Project Bootstrap](../architecture/topics/bootstrap.md). Method identity and
capability come from [`../../descriptors/registry.yaml`](../../descriptors/registry.yaml)
and its referenced descriptors; installation prose must not become another
method registry.

## Provision the contig tool set

The checked-in five-stage contig example deliberately does not bind an
assembler. A convenient CycloneSeq-first shared environment is:

```bash
conda create -n env_geno_exec -c conda-forge -c bioconda \
  seqkit jellyfish genomescope2 flye minimap2 purge_dups gzip quast busco -y
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
Assembly Review requires the target species in the manifest and at least one
matching BUSCO lineage under `databases.busco`; selection is derived from the
verified `databases.ncbi_metazoa` snapshot and fails if it is absent or
ambiguous. RunConfig does not repeat either database path. AutoHiC additionally
needs its declared external package roots,
Juicer/3D-DNA/JAR layout, and model files.

Bootstrap catalog validation is not a substitute for the contig-specific
database and tool checks below.

## Verify the checkout

The scripts resolve the repository root from their own file location, so they
may be invoked from any current directory:

```bash
bash /path/to/gene_agent/scripts/server_check.sh
bash /path/to/gene_agent/scripts/server_test.sh
```

Scope:

- `server_check.sh`: orchestrator prerequisites plus the fixed contig template
  tool/package closure; deferred Assembly-candidate readiness is stage-owned;
- `server_test.sh`: Python regression, compile, and diff checks only;
- real-data smoke: an explicit operator run using server-local inputs, described
  in [Run and Recovery](../user_guide/run-and-recovery.md).

Do not call either script successful unless it exits zero and prints its expected
completion evidence.
