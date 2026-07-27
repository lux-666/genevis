# Method-Level Bootstrap Preflight Design

## Context

`gene-agent bootstrap` currently initializes or updates `config/envs.yaml`,
checks the filesystem layout of configured external packages, and can conform
Juicer. The stage execution boundary later runs `conda run ... command -v` for
the selected method, but bootstrap does not yet tell an operator whether all
configured tool environments are ready before expensive data processing starts.

The real execution model is host-local: the operator runs the CLI from the HPC
checkout and the same conda/module/container environment that will run the
workflow. This feature must not introduce remote submission or TUI-time conda
probing.

## Goals

- After a valid `config/envs.yaml` is available, check every configured method,
  not only a selected workflow or T2T subset.
- Probe each resolved environment on the current host using the existing safe
  `conda run ... command -v "$1"` contract.
- Avoid repeatedly checking the same environment/tool combination while
  retaining method-level ownership in the report.
- Keep external package checks for AutoHiC, Juicer, 3D-DNA, and juicer-tools,
  including model and canary validation, in the bootstrap result.
- Print actionable, object-specific diagnostics: method name, target
  environment, missing tool(s) or package path(s), and a remediation hint.
- Return a failing exit status when any check fails, while never collapsing the
  cause into only a generic “bootstrap incomplete” message.
- Run automatically after `bootstrap --apply` has published a generated config
  and after Juicer conformance; also run on later bootstrap validations of an
  existing config.

## Non-goals

- No remote/HPC job submission from the local TUI.
- No conda probing during TUI dependency freeze.
- No execution of the bioinformatics tools themselves; probes only verify the
  environment and executable discovery.
- No change to stage-time preflight behavior.

## Design

### Method discovery and grouping

Add a public resolver view for configured method entries. Each method resolves
through the existing precedence rules (method entry, then stage entry, then
default fallback), so bootstrap checks exactly the environment that stage
execution would use.

The preflight helper groups methods by the resolved conda identity:
`conda_bin` plus `conda_env` or `conda_prefix`. For each group it unions and
deduplicates all `required_tools`, performs one environment usability probe,
and performs one `command -v` probe per unique tool. The grouped result is then
projected back to each method, preserving method-specific missing tool lists.
Methods without required tools still receive the environment usability check.

External `package_tools` references remain associated with the method in the
report, while the existing package-level filesystem preflight remains the
authoritative check for package roots, canaries, entrypoints, and configured
AutoHiC models. This keeps package checks from being duplicated and ensures
Juicer conformance is reflected before the final package result is evaluated.

### Bootstrap flow

The CLI flow remains:

1. Collect and validate a bootstrap plan.
2. On `--apply` (or confirmed `--update`), atomically publish `config/envs.yaml`.
3. Validate the persisted config.
4. Run requested Juicer conformance.
5. Revalidate package configuration and run package preflight.
6. Run method-level environment preflight for all configured methods.
7. Print a detailed per-package/per-method report and return `0` only when all
   checks passed.

When a new config is only previewed without `--apply`, bootstrap continues to
stop after showing the candidate plan because there is no persisted config to
probe. Once applied, the same invocation performs the complete preflight.

### Diagnostics and failure handling

Each failed package line identifies the package and missing relative paths.
Each failed method line identifies the method, resolved environment, and either
the missing executable names or the environment probe error. The output includes
a short remediation hint such as installing the named executable into the
reported conda environment, correcting the method/stage/default mapping, or
fixing the declared package `install_root`/canary. A summary may count failures,
but it must not replace these detailed lines.

Probe-level `OSError` failures are converted to a structured blocked-environment
result so bootstrap can report the conda binary/target rather than crash with a
traceback. Existing stage-time `preflight()` semantics remain unchanged.

## Testing

- Unit-test method discovery/resolution and grouping by conda identity.
- Verify duplicate tools are probed once per environment and mapped back to all
  owning methods.
- Verify blocked conda environments and missing tools produce specific method
  diagnostics; verify probe errors are structured rather than uncaught.
- Extend bootstrap CLI tests to cover a generated config that passes, a missing
  method executable, and a missing external package canary. Assert the exit
  code and the concrete method/package error text.
- Keep existing package and stage-time preflight tests passing.

## Acceptance criteria

Running `gene-agent bootstrap --apply` from the actual HPC checkout produces a
single report covering every configured method and every declared external
package. Before any expensive workflow data is processed, an operator can see
which exact executable, conda target, package path, or model path needs fixing;
the command exits successfully only after all listed checks pass.
