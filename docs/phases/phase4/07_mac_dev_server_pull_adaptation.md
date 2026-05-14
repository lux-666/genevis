# 07 Mac Dev And Server Pull Adaptation

Task status: implemented (2026-05-14).

## Goal

Replace the abandoned "local agent controls remote server" model with a
simpler Mac-development plus server-side git-pull validation workflow.

The server is an independent execution environment. Code moves through git.
Real data, conda environments, artifacts, and ledger output stay on the server.

## Background

Current operator workflow:

```text
Mac local development
-> git commit / push
-> server git pull
-> server-side tests and real bioinformatics smoke
```

The previous plan to deploy locally and drive the server through remote control
is abandoned. Do not implement or preserve `RemoteRunner`, SSH ControlMaster,
gateway, or local-agent-to-server execution paths.

## Scope

- Remove active RemoteRunner / remote-control references from code and docs.
- Add documentation for the Mac-dev / server-pull workflow.
- Add server-local helper scripts for environment checks, tests, and Phase 4
  smoke execution.
- Add manifest and run-config templates for server-local real-data smoke.
- Update handoff and Phase 4 docs so future agents know the next validation
  should happen on the server after `git pull`.

## Non-Goals

- Do not implement `RemoteRunner`.
- Do not add SSH orchestration.
- Do not add a daemon, gateway, API server, or local-to-server command bridge.
- Do not move real FASTQ/FASTA/BAM data into git.
- Do not make Mac artifact paths portable to the server.
- Do not implement durable cross-process resume.
- Do not start Phase 5 method expansion.
- Do not install conda packages automatically from helper scripts.

## Expected File Changes

Delete:

- `remote/README.md`
- `app/execution/remote_runner.py`

Update:

- `app/execution/__init__.py`
- `docs/architecture.md`
- `README.md`
- `docs/handoff.md`
- `docs/phases/phase4/README.md`
- `docs/phases/phase_completion_plan.md`
- active Phase 3/4 docs that still describe `RemoteRunner` as planned work

Add:

- `docs/deployment/mac_dev_server_pull.md`
- `docs/examples/phase4_real_manifest.template.yaml`
- `docs/examples/phase4_run_config.template.json`
- `scripts/server_check.sh`
- `scripts/server_test.sh`
- `scripts/server_phase4_smoke.sh`

## Implementation Notes

Remote execution wording should change from:

```text
RemoteRunner planned/deferred
local/remote runner boundary
SSH ControlMaster
```

to:

```text
in-agent remote execution is intentionally unsupported
server-side execution happens after git pull
LocalRunner is used on whichever machine invokes gene-agent
```

The execution module should describe the current real boundary:

```text
execution/ owns local subprocess execution for frozen attempts.
Server execution is achieved by running the same CLI on the server checkout.
```

`docs/archive/` should be left alone unless the human explicitly asks for
historical archive cleanup.

## Server Workflow Contract

The server-side workflow should be:

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

The scripts must not install packages automatically. They should report missing
conda envs or tools clearly and exit non-zero when required Phase 4 tools are
missing.

Required tools for Phase 4 server acceptance:

- `conda`
- `gene_agent` env for the Python orchestrator
- `env_geno_exec`
- `seqkit`
- `flye`
- `quast.py`

## Helper Script Requirements

`scripts/server_check.sh` should:

- print the current commit hash and branch;
- check `conda` availability;
- check `gene_agent` can run Python;
- check `env_geno_exec` exists;
- check `seqkit`, `flye`, and `quast.py` inside `env_geno_exec`;
- print actionable missing-tool messages;
- avoid package installation and destructive filesystem actions.

`scripts/server_test.sh` should run:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

`scripts/server_phase4_smoke.sh` should:

- accept `--manifest`, `--run-config`, `--session-id`, and `--data-dir`;
- call `gene-agent run` from the server checkout;
- print final `session_id`, `current_stage`, `supervision_state`, and relevant
  artifact paths;
- not copy artifacts back to the Mac;
- not assume real data paths are tracked in git.

## Manifest And Run-Config Templates

Add templates only. Real server-local files should be copied from templates and
edited under `data/` or another ignored server-local directory.

Template manifest should show:

- one `raw_long_reads` asset;
- absolute or server-local path placeholder;
- ONT/CycloneSEQ or PacBio metadata placeholder;
- no real private path.

Template run config should show:

```json
{
  "local_exec": ["read_qc", "assembly", "assembly_review"],
  "assembly": {
    "flye_parameters": {
      "read_type": "nano-raw",
      "estimated_genome_size": 2000000,
      "threads": 4
    }
  }
}
```

## Documentation Updates

Update active docs so future agents understand:

- Mac fixture tests are useful but do not replace server real-data acceptance.
- The preferred Phase 4 QUAST acceptance host is the server when Mac arm64 lacks
  `quast.py`.
- Phase 5 is still gated on a server-side real-data artifact trail.
- In-agent remote execution is intentionally unsupported.
- Durable cross-process resume remains a Phase 6 lifecycle concern.

## Acceptance Criteria

- No active docs tell future agents to implement local-to-remote server control.
- `RemoteRunner` placeholder code is removed.
- Server-side validation docs explain exactly what to run after `git pull`.
- Helper scripts can be run directly on the server.
- Manifest/run-config templates exist and do not contain real private data paths.
- Phase 4 docs identify the server as the preferred host for QUAST acceptance.
- `git diff --check` passes.
- Full unit tests pass if the implementation touches imports or code paths.

## Verification

Documentation-only minimum:

```bash
git diff --check
```

If code or scripts are changed:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

On the server after implementation:

```bash
bash scripts/server_check.sh
bash scripts/server_test.sh
bash scripts/server_phase4_smoke.sh \
  --manifest data/phase4_real_smoke/manifest.yaml \
  --run-config data/phase4_real_smoke/run_config.json \
  --session-id phase4-server-smoke-YYYYMMDD-a \
  --data-dir data/phase4_real_smoke/out
```

## Implemented State

Deleted:

- `app/execution/remote_runner.py` (placeholder; no callers).
- `remote/README.md` and the now-empty `remote/` directory.

Added:

- `scripts/server_check.sh` — prints commit/branch, probes `conda`, the
  `gene_agent` orchestrator env, the `env_geno_exec` exec env, and `seqkit`
  / `flye` / `quast.py` inside `env_geno_exec`. Exits non-zero on any
  missing piece. Performs no installation or destructive filesystem
  actions.
- `scripts/server_test.sh` — runs unit tests and `compileall` under
  `gene_agent` plus `git diff --check`, mirroring the Mac developer test
  loop on the server checkout.
- `scripts/server_phase4_smoke.sh` — accepts `--manifest`, `--run-config`,
  `--session-id`, `--data-dir`, validates inputs, calls
  `conda run -n gene_agent gene-agent run ...`, prints the expected Phase 4
  artifact paths for the operator to record in `docs/handoff.md`, and
  forwards the CLI exit code.
- `docs/examples/phase4_real_manifest.template.yaml` — Phase 4 server-side
  smoke manifest template (one `raw_long_reads` asset, placeholder
  absolute path, ONT/CycloneSEQ metadata stub, no private path).
- `docs/examples/phase4_run_config.template.json` — matches task-07 spec
  with `local_exec = ["read_qc", "assembly", "assembly_review"]` and
  `flye_parameters = { read_type: "nano-raw", estimated_genome_size:
  2_000_000, threads: 4 }`. Validates against `app/schemas/run_config.py`.

Updated:

- `app/execution/__init__.py` — docstring now states "local subprocess
  runners; in-agent remote execution is intentionally unsupported".
- `README.md` — replaces the `RemoteRunner execution` not-implemented
  bullet with a pointer to `docs/deployment/mac_dev_server_pull.md`.
- `docs/architecture.md` — module-list line, `execution/` ownership row,
  and a new `Server Execution` subsection explaining the Mac dev / server
  `git pull` boundary.
- `docs/handoff.md` — three sentence-level edits flipping
  "RemoteRunner deferred / unimplemented" wording to "in-agent remote
  execution intentionally unsupported".
- `docs/phases/phase4/README.md` — non-goal bullet, status-table row, and
  arm64 acceptance note now point operators at the server `git pull`
  workflow.
- `docs/phases/phase_completion_plan.md` — Phase 3 task list no longer
  treats `RemoteRunner` as a future decision.
- `docs/beginner_mainline_guide.md` — closing capability table flipped
  from `RemoteRunner execution \| Not implemented` to `In-agent remote
  execution \| Intentionally unsupported`.
- Phase 3 docs (`README.md`, `03`, `04`, `05`, `06`, `07`, `09`) — all
  forward-looking "RemoteRunner deferred" / "Decide RemoteRunner
  semantics separately" wording rewritten to the unsupported-and-handled
  framing. Phase 4 task docs `01`, `04`, `06` task-scoping guards left
  intact as historical task scope.

Untouched (intentionally):

- `app/entrypoints/run.py` — already exposes the right `--manifest`,
  `--session-id`, `--data-dir`, `--local-exec`, `--run-config` surface for
  the smoke script to call.
- `data/phase4_real_smoke/manifest.yaml` and `run_config.json` —
  server-local files; templates live under `docs/examples/`.
- `docs/archive/*` — explicit out of scope.

Verification commands run after the change:

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
bash -n scripts/server_check.sh
bash -n scripts/server_test.sh
bash -n scripts/server_phase4_smoke.sh
```
