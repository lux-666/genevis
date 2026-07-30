# Mac Development and Server Execution

The Mac checkout owns editing, review, lightweight verification, commits, and
pushes. The server checkout owns environment validation and real
bioinformatics execution.

Git is the only supported code-transfer boundary. Gene Agent does not provide
an in-agent remote runner, SSH ControlMaster execution, gateway dispatch, or
Mac-side artifact collection from the server.

## Source Boundary

Tracked in Git:

- application code, descriptors, and configuration defaults;
- tests and current documentation;
- manifest/run-config templates and server helper scripts;
- the pinned NCBI Metazoa reference manifest and alias table.

Server-local and untracked:

- real FASTQ/FASTA/BAM inputs;
- Conda environments and external software installations;
- generated artifacts, ledger, reports, and checkpoints;
- databases, model files, and private run configurations.

The NCBI Metazoa reference export is provisioned without downloading through
the application:

```bash
python scripts/install_ncbi_metazoa_reference.py \
  --source-dir /shared/reference/ncbi_metazoa

python scripts/install_ncbi_metazoa_reference.py --verify-only
```

The installer verifies the tracked manifest hashes. Assembly review reports a
version mismatch rather than consuming an unverified snapshot.

## Mac Workflow

```bash
git status --short
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent \
  python -m unittest discover -s tests -t . -p 'test*.py' -v
git diff --check
git add ...
git commit -m "..."
git push
```

Small committed fixtures may be used locally. They do not replace real-data
verification on the server.

## Server Workflow

```bash
git pull
bash scripts/server_check.sh
bash scripts/server_test.sh
```

Start the TUI inside the server checkout and provide server-owned input paths
during intake:

```bash
gene-agent tui --data-dir data/server_run/out
```

The TUI freezes the manifest and run config before execution. The server owns
all real paths and generated output.

## Block and Resume

When a session reaches a human gate, the SQLite checkpointer preserves the
parent graph under the run directory. Inspect the exact gate and candidate:

```bash
gene-agent inspect \
  --session-id server-run-YYYYMMDD-a \
  --data-dir data/server_run/out \
  --write-decision-template data/server_run/decision.json
```

Edit the generated template, then resume:

```bash
gene-agent resume \
  --session-id server-run-YYYYMMDD-a \
  --decision-file data/server_run/decision.json \
  --data-dir data/server_run/out \
  --checkpointer sqlite
```

Resume validates session identity, review-request bindings, the decision body,
checkpoint availability, and durable refs before changing workflow state.

## Environment Expectations

- the orchestrator runs in the configured Python environment (default:
  `gene_agent`);
- bioinformatics commands run through the method mapping in
  `config/envs.yaml`;
- AutoHiC external package roots and models pass bootstrap/preflight;
- missing commands or package files produce structured blocked/failure state,
  never fake success.

New-host provisioning is documented in [`migration.md`](migration.md).
