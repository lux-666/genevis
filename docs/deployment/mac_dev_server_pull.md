# Mac Development and Server Execution

This page owns the Git handoff and server-update procedure. It does not repeat
the run/freeze/recovery contract; operators use
[Run and Recovery](../user_guide/run-and-recovery.md) after the checkout is ready.

The bootstrap, contig-readiness, and code-test boundaries are indexed in
[Deployment Entry Points](README.md).

The Mac checkout owns editing, review, lightweight verification, commits, and
pushes. The Linux/server checkout owns environment validation and real
bioinformatics execution. Git is the supported code-transfer boundary; Gene
Agent has no in-agent SSH runner, remote scheduler backend, or Mac-side artifact
collector.

## Source boundary

Tracked project material includes code, descriptors, dependency declarations,
tests, documentation, examples, and helper scripts.

Server-local material includes:

- real FASTQ/FASTA/BAM inputs and sample metadata;
- `.env`, API keys, and other credentials;
- active `config/envs.yaml`; only `docs/examples/envs.yaml.example` is committed;
- Conda environments and external package installations;
- databases, BUSCO lineages, model files, runtime artifacts, Ledger, reports,
  and checkpoints;
- copied run configs containing private run inputs or executor knobs.

Acquire and install NCBI snapshots only through their
[snapshot authority](../../data/reference/ncbi/README.md). The installer
resolves its root destination from `databases.ncbi` in the active host
mapping; use `--config PATH` when that mapping is not the default. Assembly
Review reports a version mismatch instead of consuming an unverified snapshot.

## First pull after the host-config split

`config/envs.yaml` is now ignored machine-local state. On an existing server
checkout that still has this path tracked, the first pull containing that Git
transition can delete the working-tree copy. Before that pull, save the active
mapping outside the repository:

```bash
mkdir -p "$HOME/gene-agent-host-config-backups"
host_config_backup="$HOME/gene-agent-host-config-backups/envs.yaml.$(date +%Y%m%d%H%M%S)"
cp -p config/envs.yaml "$host_config_backup"

# Make the tracked path clean only after the external backup exists.
git restore --source=HEAD --worktree -- config/envs.yaml
git pull --ff-only

cp -p "$host_config_backup" config/envs.yaml
git status --short --ignored -- config/envs.yaml
```

The `git restore` above discards only the tracked working copy after its
exact host mapping has been backed up outside the checkout. After the pull, the
copy command restores that mapping at the now-ignored path. Git should report
`!! config/envs.yaml`; then run the normal bootstrap preview and server checks.
A fresh clone has only `docs/examples/envs.yaml.example`; the operator may
create the active file manually or use interactive `gene-agent bootstrap`.
Bootstrap may skip database or AutoHiC setup until those resources are
installed, and creates the selected plan only after displaying it and receiving
confirmation. Normal execution does not
silently use the example when the active mapping is missing.

## Mac workflow

```bash
git status --short
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache \
  python -m pytest -q
git diff --check
git add ...
git commit -m "..."
git push
```

Only stage intended files. Do not add `.env`, real input paths, runtime data,
checkpoints, reports, credentials, or caches. Small committed fixtures support
local regression tests but do not replace a server real-data run.

## Server update workflow

Before pulling, inspect whether the checkout has active sessions. Sessions
resume only with a compatible checkpoint representation; finish them before an
incompatible update or keep the matching checkout and environment available.

The ordinary update sequence below applies only after this host has completed
the `config/envs.yaml` tracking split above. If
`git check-ignore config/envs.yaml` does not identify the repository ignore
rule, use the backup and first-pull sequence before any normal pull.

Then update and verify:

```bash
git status --short
git pull --ff-only
bash scripts/server_test.sh
bash scripts/server_check.sh
```

The two scripts may be called by absolute path from another directory because
they locate the repository root themselves.

Do not blur their scope:

- `server_check.sh` validates orchestrator prerequisites and the fixed-stage
  tool/package closure plus the BUSCO/NCBI database registry used by the
  authoritative five-stage contig template.
  Assembly method selection and candidate readiness are deferred to stage
  entry. The script deliberately does not probe the real LLM endpoint or
  private inputs.
- `server_test.sh` runs code regression/compile/diff checks. It does not execute
  Flye, Jellyfish, purge_dups, QUAST, BUSCO, or a real-data pipeline.
- the actual `gene-agent run` preflight validates run-specific inputs and
  services before freeze.

After both scripts exit zero, copy/fill server-local templates and follow
[Run and Recovery](../user_guide/run-and-recovery.md).

New-host provisioning is documented separately in
[New Host Setup](migration.md).
