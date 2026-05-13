# 01 Stage-Specific Conda Env And Local Executor

Task status: implemented.

## Goal

Make real method execution environment-aware so each stage or method can run in
its configured conda environment.

Different bioinformatics packages must not be assumed to exist in the current
shell or in the `gene_agent` Python environment. The runner should be able to
execute a frozen attempt through `conda run -n <env_name> ...` and report
structured blocked states when the environment or required tool is missing.

## Scope

Add a small environment configuration layer that maps stages and methods to
conda environments and required command-line tools.

Proposed config source:

```text
config/envs.yaml
```

Suggested starting shape:

```yaml
default:
  conda_bin: conda
  fallback_env: gene_agent

stages:
  read_qc:
    conda_env: gene_agent
  genome_survey:
    conda_env: env_genome_survey
  assembly:
    conda_env: env_assembly_flye
  assembly_review:
    conda_env: env_assembly_review
  hic_scaffolding:
    conda_env: env_hic_yahs
  repeat_annotation:
    conda_env: env_repeatmasker

methods:
  read_qc.long_read_stats.seqkit_stats:
    conda_env: gene_agent
    required_tools:
      - seqkit
  read_qc.length_filter.seqkit_seq:
    conda_env: gene_agent
    required_tools:
      - seqkit
  assembly.candidate.flye:
    conda_env: env_assembly_flye
    required_tools:
      - flye
  assembly_review.eval.quast:
    conda_env: env_assembly_review
    required_tools:
      - quast.py
  hic_scaffolding.candidate.yahs:
    conda_env: env_hic_yahs
    required_tools:
      - yahs
  repeat_annotation.masking.repeatmasker:
    conda_env: env_repeatmasker
    required_tools:
      - RepeatMasker
```

Method-level config should override stage-level config. Stage-level config should
override the default fallback environment.

## Boundaries

- Do not require users to run `conda activate`.
- Do not hard-code Flye, QUAST, YaHS, or RepeatMasker into the `gene_agent`
  environment.
- Do not silently fall back to the current PATH when a method declares a conda
  environment.
- Do not implement RemoteRunner in this task.
- Do not install conda environments in this task.
- Do not execute real genome-scale data in this task.
- Do not change biological method selection policy.

## Expected Outputs

- `config/envs.yaml`
- environment resolver behavior for stage and method env lookup
- preflight checks for conda environment existence
- preflight checks for required tools inside the selected environment
- runner command execution through `conda run -n <env_name>`
- stdout, stderr, return code, and output path recording through existing result
  structures
- structured status values for:
  - `blocked_env`
  - `missing_tool`
  - `failed_retryable`
  - `failed_fatal`

## Tasks

- [x] Add `config/envs.yaml` with stage and method environment mappings.
- [x] Implement `app/execution/env_resolver.py` to load `config/envs.yaml`.
- [x] Add tests for method-level override, stage-level fallback, and default
  fallback environment behavior.
- [x] Add a preflight helper that runs `conda run -n <env> command -v <tool>`
  or equivalent without relying on shell activation.
- [x] Update `LocalRunner` or add a small wrapper so frozen attempts can execute
  through the resolved conda environment.
- [x] Preserve existing fake-binary tests by allowing test-specific env overrides
  or explicit bypass only in tests.
- [x] Update read QC, Flye, QUAST, YaHS, and RepeatMasker frozen attempts to
  carry enough method/stage identity for env resolution.
- [x] Ensure missing env or missing tool produces blocked state and ledger
  evidence instead of fake outputs.
- [x] Keep stdout/stderr/return code artifacts compatible with existing ledger
  and artifact collection.

## Acceptance

- `read_qc.long_read_stats.seqkit_stats` resolves to `gene_agent` by default.
- `assembly.candidate.flye` resolves to `env_assembly_flye`.
- `assembly_review.eval.quast` resolves to `env_assembly_review`.
- A nonexistent environment blocks before the biological command runs.
- A missing required tool blocks before the biological command runs.
- A real command uses `conda run -n <env_name>` without requiring prior shell
  activation.
- Existing unit tests remain green.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_read_qc tests.test_assembly tests.test_assembly_review -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```

## Human Decisions Before Coding

- Final `config/envs.yaml` shape if the proposed schema is not acceptable.
- Whether to use env names only or allow absolute conda prefix paths.
- Whether missing env should be represented as stage `blocked_env` or terminal
  failure for each method family.

## Implemented State

- `config/envs.yaml` ships with `default.conda_bin`, `default.fallback_env`,
  and stage / method entries for the real local execution mainline. Every
  stage and every declared method resolves to the single shared
  `env_geno_exec` conda env (SeqKit stats / SeqKit seq, Flye, QUAST, YaHS,
  RepeatMasker). The `gene_agent` env is the orchestrator only; bioinformatics
  binaries are not installed into it.
- `app/execution/env_resolver.py` exposes `EnvResolver`, `EnvSpec`,
  `PreflightResult`, and `preflight(...)`. Resolution precedence is
  method > stage > default. Both `conda_env` (named env) and `conda_prefix`
  (absolute prefix) are supported; setting both on one entry is rejected.
- Preflight uses `conda run --live-stream --no-capture-output -n|-p TARGET`
  to probe env usability and per-tool presence without shell activation. It
  returns one of `ok`, `blocked_env`, `missing_tool` with a human-readable
  `detail` and a `missing_tools` list.
- `FrozenAttemptSpec` carries optional `conda_bin`, `conda_env`, and
  `conda_prefix`. `LocalRunner` dispatches through `conda run` when either
  env field is set, otherwise falls back to the legacy shell-based execution
  so existing fake-binary stage tests stay green.
- `ExecutionResult` has a structured `status` field (`ok` / `failed` today;
  `blocked_env` / `missing_tool` reserved for future stage wiring) plus a
  `status_detail` string so callers can emit the right ledger/state vocabulary.
- A shared dispatcher `app/execution/env_dispatch.py` centralises the resolve
  -> preflight -> blocked-payload-or-spec flow. It also honours a PATH-based
  test escape hatch (`stage_outputs.<stage>.env.PATH`) so existing fake-binary
  suites keep running without conda. The `attach_conda_fields` helper merges
  the resolved `EnvSpec`'s conda bin/env/prefix into each stage's
  `FrozenAttemptSpec` kwargs before construction.
- Every stage that freezes a real attempt (`read_qc.freeze_seqkit_attempt`,
  `read_qc.maybe_run_length_filter`, `assembly.freeze_flye_attempt`,
  `assembly_review.freeze_quast_attempts`, `hic_scaffolding.freeze_yahs_attempt`,
  `repeat_annotation.freeze_repeatmasker_attempt`) now calls the shared
  dispatcher before materialising `FrozenAttemptSpec`. Preflight failures
  short-circuit into `status = blocked_env | missing_tool`, record an
  `env_preflight_blocked` ledger event with `env_target` / `missing_tools` /
  `detail`, and route to the stage's terminal failure edge.
- Each stage's routing module now recognises `blocked_env` and `missing_tool`
  alongside `failed` so preflight blocks reach `fail_<stage>` without running
  the biological command, and each `fail_<stage>` translates those statuses
  into a human-readable `last_error`.

