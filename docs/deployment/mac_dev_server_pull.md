# Mac Development And Server Pull Workflow

## Purpose

This project uses Mac local development and server-local execution.

The Mac checkout is for editing, review, lightweight tests, and commits. The
server checkout is for full environment validation and real bioinformatics
runs.

There is no supported mode where the Mac agent controls the server through
`RemoteRunner`, SSH ControlMaster, gateway, or remote command dispatch.

## Source Of Truth

Git is the only code transfer boundary.

Tracked in git:

- application code
- descriptors
- configs
- tests
- docs
- manifest templates
- run-config templates
- server helper scripts

Not tracked in git:

- real FASTQ/FASTA/BAM data
- conda environments
- generated artifacts
- ledger output
- server-local run configs with private paths
- large bioinformatics databases

## Mac Workflow

```bash
git status --short
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
git add ...
git commit -m "..."
git push
```

Mac smoke runs may use small committed fixtures. They are not a replacement for
server real-data acceptance.

## Server Workflow

```bash
git pull
bash scripts/server_check.sh
bash scripts/server_test.sh
bash scripts/server_phase4_smoke.sh \
  --manifest data/phase4_real_smoke/manifest.yaml \
  --run-config data/phase4_real_smoke/run_config.json \
  --session-id phase4-server-smoke-YYYYMMDD-a \
  --data-dir data/phase4_real_smoke/out
```

The server owns real data paths and generated output.

## Environment Expectations

The Python orchestrator runs in:

```text
gene_agent
```

Bioinformatics tools run through:

```text
env_geno_exec
```

Phase 4 server acceptance requires:

```text
seqkit
flye
quast.py
```

Missing tools should block with structured state rather than fake success.

## Manifest And Run Config

Use templates from:

```text
docs/examples/phase4_real_manifest.template.yaml
docs/examples/phase4_run_config.template.json
```

Copy them to a server-local data directory and edit paths there.

Example server-local files:

```text
data/phase4_real_smoke/manifest.yaml
data/phase4_real_smoke/run_config.json
```

Do not commit those server-local files if they contain private paths or real
data locations.

## Acceptance Record

After a successful server smoke, record in `docs/handoff.md`:

- commit hash
- server host/architecture
- session id
- manifest path
- run-config path
- conda env/tool availability
- final `current_stage`
- final `supervision_state`
- artifact refs for:
  - `seqkit_stats.tsv`
  - `read_qc_summary.json`
  - `long_reads.fofn`
  - `draft_assembly.fasta`
  - QUAST `report.tsv`
  - `quast_summary.json`
  - `assembly_review_request.json`

## Abandoned Remote-Control Model

Do not reintroduce:

- `RemoteRunner`
- SSH ControlMaster runner
- gateway-driven execution
- Mac-side orchestration of server commands
- local agent copying artifacts from the server

If automation is needed later, prefer server-side CI or a server-local worker
that pulls git and runs commands from inside the server environment.
