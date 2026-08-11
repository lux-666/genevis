# Project Bootstrap

> **Status:** implemented as an auxiliary machine-configuration command
>
> **Read when:** initializing or repairing `config/envs.yaml` and `.env`, or
> diagnosing package and Conda readiness before freezing a run.
>
> **Comes from:** operator-supplied machine paths and environment choices,
> configured method identities, and package declarations.
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

| Bootstrap owns | Bootstrap does not own |
|---|---|
| Missing or explicitly selected fields in `config/envs.yaml` | Run manifests, run configuration, `resource_refs`, artifacts, ledger events, checkpoints, or reports |
| Selected `GENE_AGENT_*` overrides in project `.env`, with secrets redacted and protected | Descriptor creation or method registration |
| Validation of package paths and canaries declared in the active config | Installing arbitrary tools, Conda environments, reference databases, or workflow packages |
| Probing configured method environments with execution-time precedence | Freezing a session or rebuilding the dependency snapshot of an existing session |
| The declared Juicer layout-conformance action | A general package repair framework |

Per-run facts such as reads, an assembly, species metadata, restriction enzyme,
stage choice, and review decisions belong to TUI/run inputs and durable run
evidence. Putting them in `.env` or `config/envs.yaml` would cross the boundary.

## Modes and write behavior

The default command builds and validates a plan. If changes are needed it
prints them and exits without writing, directing the operator to `--apply`.
`--apply` writes the approved initialization plan and may execute declared
package conformance. `--update` is the interactive repair path for existing
bootstrap-owned fields: the operator selects fields, sees `old -> new`, and an
empty final confirmation commits only that plan. The CLI makes `--apply` and
`--update` mutually exclusive.

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
   executables; and
5. reports every package, method, target, missing executable, and repair hint
   before returning non-zero.

This is readiness validation, not installation. The only implemented
package-layout mutation is the bounded Juicer link repair declared by the
package config. A general environment solver, downloader, scheduler setup, or
package installer is not implemented and must not be inferred from the word
“bootstrap.”

## Relationship to the two freezes

`config/envs.yaml` is mutable machine state. When the operator later confirms a
TUI plan, the whole-run freeze captures the selected environment and package
closure in `control/dependency_manifest.json`. The frozen executor resolves
from that snapshot, not from later bootstrap edits. Each executable method then
creates its narrower method-attempt freeze from the approved inputs and frozen
dependency selection.

Bootstrap probes the complete configured machine catalog. Separately, after a
run's final confirmation and TTY check, both structured run and TUI perform a
read-only readiness probe for only the canonical effective method closure (and
the mandatory LLM endpoint when the chosen workflow reaches one). The exact
same closure is then frozen into the dependency snapshot. A failed run-specific
probe creates no session. A first-run bootstrap preview with no persisted config
cannot probe a configuration that has not been written yet; the first `--apply`
performs validation and preflight immediately after writing it.
