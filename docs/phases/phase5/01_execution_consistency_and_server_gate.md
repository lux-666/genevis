# 01 Execution Consistency And Server Gate

Task status: implemented in code (2026-05-14). Server-side validation
(`scripts/server_check.sh` + `scripts/server_test.sh` after `git pull`) is
still pending and tracked in `docs/handoff.md`.

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

- [x] Review all active references to `RemoteRunner`, remote runner, SSH
  ControlMaster, gateway, and local-to-server command dispatch.
- [x] Remove or reword active docs so server execution means "run the same CLI
  on the server checkout after git pull."
- [x] Add env config entries for `genome_survey` methods if they are missing or
  incomplete.
- [x] Wire `genome_survey` attempt freezing through the shared env dispatcher.
- [x] Add tests for missing `jellyfish` / missing `genomescope2` preflight
  blocks.
- [x] Confirm existing tests still cover successful fake-binary genome survey
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

## Implementation Notes

- `config/envs.yaml` gains two method-level entries:
  `genome_survey.kmer_count.jellyfish` (`required_tools: [jellyfish]`)
  and `genome_survey.model.genomescope2`
  (`required_tools: [genomescope.R]`). The probed binary name matches
  what the freeze script invokes, so preflight rejects an environment
  where the script alias does not exist.
- `app/stages/genome_survey/nodes.py` rewires `freeze_jellyfish_attempt`
  and `freeze_genomescope2_attempt` to call
  `app.execution.env_dispatch.resolve_method_env(...)` and merge the
  resolved spec into the `FrozenAttemptSpec` kwargs via
  `attach_conda_fields(...)`. Both freezes use
  `app.stages.base.absolute_data_dir(state)` for path composition (same
  fix already applied to `read_qc` / `assembly`). The bypass branch
  (`stage_outputs.genome_survey.env.PATH` set) survives because
  `env_dispatch.has_explicit_path_override` returns `(None, None)` and
  the existing fake-binary tests keep passing.
- `app/stages/genome_survey/routing.py` introduces
  `route_after_freeze` and a shared `FAIL_STATUSES = {"failed",
  "blocked_env", "missing_tool"}` (mirrors the assembly stage). The
  builder uses it to short-circuit a preflight block to `fail` before
  any biological command runs.
- `commit_genome_survey` retains `env_target` / `missing_tools` /
  `method_name` / `reason` on the committed payload, and
  `fail_genome_survey` defaults to a structured reason on
  `blocked_env` / `missing_tool` (parallels read_qc /
  repeat_annotation).
- New tests in
  `tests.test_genome_survey.GenomeSurveyEnvPreflightTests`:
  `test_missing_conda_env_blocks_jellyfish_with_blocked_env_status`,
  `test_missing_jellyfish_in_env_blocks_with_missing_tool_status`,
  `test_missing_genomescope_after_jellyfish_succeeds_blocks_with_missing_tool`.
  All three assert no biological binary runs before preflight blocks,
  and the jellyfish-then-genomescope test additionally proves the
  per-method preflight order is honored. Existing fixture-PATH tests
  (`test_local_full_mode_runs_jellyfish_and_genomescope2` and friends)
  still run because the bypass path is preserved.

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
