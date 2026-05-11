# 01 Stage-Specific Conda Env And Local Executor

Task status: planned.

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

- [ ] Add `config/envs.yaml` with stage and method environment mappings.
- [ ] Implement `app/execution/env_resolver.py` to load `config/envs.yaml`.
- [ ] Add tests for method-level override, stage-level fallback, and default
  fallback environment behavior.
- [ ] Add a preflight helper that runs `conda run -n <env> command -v <tool>`
  or equivalent without relying on shell activation.
- [ ] Update `LocalRunner` or add a small wrapper so frozen attempts can execute
  through the resolved conda environment.
- [ ] Preserve existing fake-binary tests by allowing test-specific env overrides
  or explicit bypass only in tests.
- [ ] Update read QC, Flye, QUAST, YaHS, and RepeatMasker frozen attempts to
  carry enough method/stage identity for env resolution.
- [ ] Ensure missing env or missing tool produces blocked state and ledger
  evidence instead of fake outputs.
- [ ] Keep stdout/stderr/return code artifacts compatible with existing ledger
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

