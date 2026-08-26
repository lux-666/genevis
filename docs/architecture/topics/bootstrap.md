# Project Bootstrap

> **Status:** implemented as an auxiliary machine-configuration command
>
> **Read when:** initializing or repairing `config/envs.yaml` and `.env`, or
> diagnosing package and Conda readiness before freezing a run.
>
> **Comes from:** operator-supplied machine paths and environment choices,
> the host method catalog, and package/database declarations.
>
> **Feeds into:** the dependency snapshot used by
> [TUI and planning](tui-and-planning.md), [method execution](execution.md), and
> the runtime requirements of [AutoHiC](autohic.md).
>
> **Contract dependencies:** [descriptors](../reference/descriptors.md),
> [governance](../reference/governance.md), and
> [run data](../reference/data-contract.md).
>
> **Code evidence:** `app/entrypoints/run.py::_run_bootstrap`,
> `app/execution/project_bootstrap.py`,
> `app/execution/package_bootstrap.py`,
> `app/execution/method_preflight.py`, `app/execution/env_resolver.py`, and
> `tests/test_project_bootstrap.py`.

## Boundary

Bootstrap prepares mutable machine/project configuration. It is not a workflow
run and it does not own biological inputs.

`docs/examples/envs.yaml.example` is the committed generic seed. The default active
`config/envs.yaml` is ignored, machine-local state created or updated by
bootstrap; execution never falls back to the example when the active file is
missing. Active-config selection is shared with execution: explicit
`bootstrap --config PATH`, then `GENE_AGENT_ENVS_CONFIG`, then the repository
default. An override should be absolute when callers may use different current
directories: relative overrides follow the caller's current directory, whereas
the default `config/envs.yaml` is checkout-anchored.

| Bootstrap owns | Bootstrap does not own |
|---|---|
| Missing or explicitly selected fields in the active environment/package/database mapping | Run manifests, run configuration, `resource_refs`, artifacts, ledger events, checkpoints, or reports |
| Selected `GENE_AGENT_*` overrides in project `.env`, with secrets redacted and protected | Descriptor creation or method registration |
| Validation of package paths and canaries declared in the active config | Installing arbitrary tools, Conda environments, reference databases, or workflow packages |
| Probing configured method environments with execution-time precedence | Freezing a session or rebuilding the dependency snapshot of an existing session |
| The declared Juicer layout-conformance action | A general package repair framework |

Per-run facts such as reads, an assembly, species metadata, restriction enzyme,
stage choice, and review decisions belong to TUI/run inputs and durable run
evidence. Putting them in `.env` or `config/envs.yaml` would cross the boundary.

## Modes and write behavior

The default command builds and validates one plan, prints every configuration
change and declared package-conformance action it will perform, and waits for
an empty Enter confirmation before applying that same in-memory plan. It does
not exit and reconstruct a second plan in a later invocation. `--update` is the
interactive repair path for existing bootstrap-owned fields: the operator
selects fields, sees `old -> new`, and uses the same final confirmation rule.
There is no separate `--apply` mode.

Existing valid values are preserved unless explicitly selected for update.
Candidate configuration is loaded through the same `EnvResolver` used by
execution; stale-plan and unrecorded-mutation checks prevent a concurrent edit
from being silently overwritten. Secret values are never printed, and a new or
updated `.env` is written with user-only permissions.

A newly generated configuration includes the AutoHiC, Juicer, 3D-DNA,
juicer-tools, and model locations consumed by the current AutoHiC adapter.
Bootstrap asks for concrete locations instead of leaving placeholder package
configuration.

## Validation and conformance

After a persisted candidate is available, bootstrap:

1. validates environment and package references;
2. performs the implemented Juicer symlink conformance when the package opts in
   and the chosen mode authorizes writes;
3. verifies every declared package canary;
4. groups configured method entries by Conda target and probes their required
   executables;
5. verifies every declared BUSCO lineage and the NCBI snapshot manifest; and
6. reports every package, method, database, target, missing executable, and repair hint
   before returning non-zero.

This is readiness validation, not installation. The only implemented
package-layout mutation is the bounded Juicer link repair declared by the
package config. A general environment solver, downloader, scheduler setup, or
package installer is not implemented and must not be inferred from the word
“bootstrap.”

## Relationship to the two freezes

`config/envs.yaml` is mutable machine state. When the operator later confirms a
TUI plan, the whole-run freeze captures the selected environment/package
closure and typed database registry in `control/dependency_manifest.json`. The
frozen executor resolves from that snapshot, not from later bootstrap edits.
Each executable method then creates its narrower method-attempt freeze from the
approved inputs and frozen dependency selection.

The only implicit dotenv source is `.env` beside the project selected by the
active host mapping: for `config/envs.yaml` it is the repository-root `.env`;
for an explicit/`GENE_AGENT_ENVS_CONFIG` path under another `config/` directory
it is that project's root `.env`. A caller's current-directory `.env` is never
an alternate source. Already exported variables still win through setdefault
loading.

Bootstrap probes every package and method entry declared in the active machine
mapping. Separately, after a
run's final confirmation and TTY check, both structured run and TUI perform a
read-only readiness probe for only the canonical effective method closure (and
the mandatory LLM endpoint when the chosen workflow reaches one). The exact
same closure is then frozen into the dependency snapshot. A failed run-specific
probe creates no session. A first-run bootstrap validates its candidate before
confirmation, then performs persisted validation and preflight immediately
after publishing that same plan.

Every locally executable method in that selected closure must resolve through
the active config to a non-empty `required_tools` and/or `package_tools`
declaration. Method-level entries may override a stage-level declaration, while
stage-level requirements remain valid user-authored configuration. A bare
default Conda target or an empty resolved requirement set fails closed, so a
configuration omission cannot become a zero-tool pass.
