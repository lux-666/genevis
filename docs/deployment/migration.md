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
| Assembly | the selected one of `flye`, `hifiasm`, `nextDenovo` |
| Assembly Dedup | `minimap2`, `pbcstat`, `calcuts`, `split_fa`, `gzip`, `purge_dups`, `get_seqs` |
| Assembly Review | `quast.py`, `busco` |
| T2T | `minimap2` |

Install the required subset with Bioconda/Conda Forge or the deployment method
appropriate to the server. Missing commands produce structured preflight
failures; the workflow does not fabricate success.

For the authoritative Flye-based five-stage contig template, a fresh shared
tool environment can be provisioned with:

```bash
conda create -n env_geno_exec -c conda-forge -c bioconda \
  seqkit jellyfish genomescope2 flye minimap2 purge_dups gzip quast busco -y
```

The package `purge_dups` must expose `pbcstat`, `calcuts`, `split_fa`,
`purge_dups`, and `get_seqs`; package installation alone is not treated as
proof. `scripts/server_check.sh` derives the selected method closure from
`docs/examples/contig_run_config.template.json`, resolves it through
`config/envs.yaml`, and verifies the actual commands. Those two files remain
the requirement authorities; this command is only a provisioning convenience.

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

## Conditional LLM Configuration

Assembly parameter recommendation requires a configured, reachable LLM; explicit
Flye/route/thread settings do not replace the biological parameter recommendation.
Hi-C scaffolding and T2T parameter recommendation also use the mandatory LLM
profile. Before freeze, `run`/TUI performs an authenticated endpoint probe for
workflows that contain one of those stages. Pure existing-assembly review-only
runs, `inspect`, storage audit, and other workflows without an LLM stage do not
require LLM configuration.

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

- `contig_manifest.template.yaml` paired with
  `contig_run_config.template.json` for the authoritative raw-read five-stage
  contig workflow;
- `existing_assembly_manifest.template.yaml` paired with
  `assembly_review_run_config.template.json`.

Copy a template to a server-local directory and replace placeholder paths. Do
not commit private paths or real sample metadata.

For the raw-read assembly example, the operator-provided genome-size estimate
lives in the paired manifest while measured survey results remain stage output.
The existing-assembly review example does not require a genome-size estimate.

## Start an Operator Run

Copy a manifest and run-config template to a server-local directory, replace
the placeholders, then run the deterministic structured-input boundary:

```bash
gene-agent run \
  --manifest /absolute/path/to/manifest.yaml \
  --run-config /absolute/path/to/run_config.json \
  --data-dir /absolute/path/to/data
```

The manifest owns `session_id`; output is written to
`<data-dir>/runs/<session_id>/`. The command validates and canonicalizes both
files, verifies the selected locally executable method closure and any mandatory
LLM endpoint, and only then freezes them and their dependency snapshot. The
readiness check is read-only and does not create a run session on failure. It
does not probe unselected assemblers. The run config requires one
`requested_stages` list of `{stage, allow_local_execution}` objects. Freeze
derives each occurrence's method roster from stage configuration and the method
catalog and stores it in the canonical frozen workflow step. The boolean
authorizes local process execution only for that occurrence.
`requested_stages: null` is rejected, while `[]` is valid. Inventory registers
an `assembly_fasta_registered` input as the central assembly resource;
`assembly_review` is the recommended review-only entry, but the executor does
not reorder the occurrence sequence by registry order. `assembly_review` is
optional and reusable: pre-freeze analysis binds an explicitly requested Review
to its nearest earlier assembly handoff, but never inserts a Review or rejects
an otherwise valid literal sequence because Review is absent. A caller may run
an authored RunConfig directly without using the planner; see
[Runtime orchestration](../architecture/topics/runtime-orchestration.md).
New runs require real stdin and stdout TTYs and reject noninteractive automation
before freeze. If an attached process later stops at a human gate, the durable
`blocked_human` checkpoint remains recoverable through `inspect`, `review`, and
`resume`. The TUI is an optional conversational intake/planning alternative
that uses the same frozen executor.

## Active Sessions During Code Upgrades

The current pre-release runtime has no checkpoint migration. Finish active
sessions before replacing a server checkout, or preserve the old checkout and
its environment until their SQLite checkpoints have been resumed or
terminalized. Do not assume upgraded code can read an older checkpoint.

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
