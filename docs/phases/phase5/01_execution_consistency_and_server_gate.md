# 01 Execution Consistency And Server Gate

Task status: planned.

## Goal

Stabilize the execution boundary before adding more methods.

Every realized method slice should follow the same path:

```text
descriptor
-> frozen attempt
-> EnvResolver / preflight
-> LocalRunner on the invoking machine
-> artifacts
-> ledger
-> compact state refs
```

The server/HPC should be the real validation environment after `git pull`; the
Mac remains the development and lightweight-test environment.

## Scope

- Confirm Phase 4 server-side smoke is runnable after the Mac-dev / server-pull
  adaptation.
- Ensure active docs no longer present `RemoteRunner` or local-to-server control
  as a supported future direction.
- Wire `genome_survey` Jellyfish + GenomeScope2 local execution through
  `env_dispatch` instead of trusting the operator shell environment.
- Audit realized local execution slices for consistent env preflight and blocked
  status vocabulary:
  - `read_qc`
  - `assembly`
  - `assembly_review`
  - `hic_scaffolding`
  - `repeat_annotation`
  - `genome_survey`
- Confirm server helper scripts and templates produce repeatable commands.
- Update docs and handoff to reflect the server/HPC validation path.

## Non-Goals

- Do not add BUSCO, Merqury, minimap2 coverage, or purge tools.
- Do not add new assembler execution paths.
- Do not add gene or function annotation execution.
- Do not implement durable resume in this task.
- Do not implement or restore `RemoteRunner`.
- Do not add biological accept/reject thresholds.
- Do not download databases automatically.

## Expected Outputs

- `genome_survey` local Jellyfish + GenomeScope2 attempts use
  `EnvResolver` / `env_dispatch`.
- Missing `jellyfish` or `genomescope2` in the configured execution env produces
  `blocked_env` or `missing_tool` before command execution.
- All realized local methods have consistent ledger evidence for preflight
  blocks and attempt lifecycle events.
- Server-side smoke instructions are current and do not require Mac-side remote
  control.
- Handoff identifies the next task based on actual server-side validation
  outcome.

## Tasks

- [ ] Review all active references to `RemoteRunner`, remote runner, SSH
  ControlMaster, gateway, and local-to-server command dispatch.
- [ ] Remove or reword active docs so server execution means "run the same CLI
  on the server checkout after git pull."
- [ ] Add env config entries for `genome_survey` methods if they are missing or
  incomplete.
- [ ] Wire `genome_survey` attempt freezing through the shared env dispatcher.
- [ ] Add tests for missing `jellyfish` / missing `genomescope2` preflight
  blocks.
- [ ] Confirm existing tests still cover successful fake-binary genome survey
  execution.
- [ ] Run server helper scripts on the server/HPC after `git pull`.
- [ ] Record the server-side validation result in `docs/handoff.md`.

## Acceptance

- A future worker can add new method slices by following one execution pattern.
- `genome_survey` no longer depends on whichever shell PATH the operator had
  active.
- Mac fixture tests and server real-data validation have distinct, documented
  roles.
- No active plan tells agents to implement local-agent-controlled remote
  execution.
- All verification commands pass.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_genome_survey tests.test_env_resolver -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

Server-side:

```bash
bash scripts/server_check.sh
bash scripts/server_test.sh
```
