# Project Bootstrap

> **Status:** implemented as an optional machine-configuration assistant
>
> **Read when:** initializing, manually editing, or repairing
> `config/envs.yaml` and `.env`, or diagnosing configured package and Conda
> readiness before freezing a run.
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
run, it does not own biological inputs, and it is not required before every
execution. An operator may create or edit `config/envs.yaml` and `.env`
directly; bootstrap is the assisted path for generating, extending, validating,
and applying bounded conformance to the same files.

`docs/examples/envs.yaml.example` is the committed generic seed. The default
active `config/envs.yaml` is ignored, machine-local state created by bootstrap
or the operator; execution never falls back to the example when the active file
is missing. Active-config selection is shared with execution: explicit
`bootstrap --config PATH`, then `GENE_AGENT_ENVS_CONFIG`, then the repository
default. An override should be absolute when callers may use different current
directories: relative overrides follow the caller's current directory, whereas
the default `config/envs.yaml` is checkout-anchored.

| Bootstrap owns | Bootstrap does not own |
|---|---|
| Assisted creation and repair of the active environment/package/database mapping | Exclusive authorship of the mapping; direct operator edits are supported |
| Selected `GENE_AGENT_*` overrides in project `.env`, with secrets redacted and protected | Requiring `.env` to be created through bootstrap |
| Validation of every environment, package, and database entry that is declared | Treating an undeclared optional database or AutoHiC group as malformed configuration |
| Probing configured method environments with execution-time precedence | Proving that an arbitrary future workflow is ready |
| The declared Juicer layout-conformance action | Installing arbitrary tools, Conda environments, reference databases, or workflow packages |
| Previewing and atomically publishing the selected file changes | Freezing a session or rebuilding the dependency snapshot of an existing session |

Per-run facts such as reads, an assembly, species metadata, restriction enzyme,
stage choice, and review decisions belong to TUI/run inputs and durable run
evidence. Putting them in `.env` or `config/envs.yaml` would cross the boundary.

## Manual and assisted configuration

Manual editing and bootstrap produce the same active configuration contract.
There is no bootstrap marker, ownership bit, or alternate runtime format. After
a manual edit, the operator may run bootstrap to validate configured entries;
after a bootstrap edit, the operator may continue maintaining the file by hand.

Host capabilities may be configured incrementally:

1. create the core mapping with the shared Conda environment or prefix;
2. omit Assembly Review databases or AutoHiC while those resources are not
   installed;
3. install the resources later; and
4. either add their complete YAML groups manually or rerun ordinary
   `gene-agent bootstrap` and choose to configure the missing group.

An omitted optional group means “not configured on this host yet.” A group that
is present remains strict: placeholders, missing required fields, nonexistent
paths, invalid database contents, or missing package canaries are errors. A
workflow that selects an omitted capability fails its run-specific readiness
check before freeze.

## Modes and write behavior

The default command builds and validates one plan, prints every configuration
change and declared package-conformance action it will perform, and waits for
an empty Enter confirmation before applying that same in-memory plan. It does
not exit and reconstruct a second plan in a later invocation.

On a missing active file, bootstrap always collects the shared Conda target and
asks separately whether to configure Assembly Review databases and AutoHiC.
Skipping either optional group removes its placeholders from the generated
candidate and does not prevent `config/envs.yaml` or independent `.env` changes
from being written. Rerunning the ordinary command later offers any still
missing optional group again.

`--update` is the interactive replacement path for existing bootstrap-managed
fields: the operator selects fields, sees `old -> new`, and uses the same final
confirmation rule. Adding a currently absent database or AutoHiC group uses the
ordinary command; `--update` does not maintain a second schema or provision a
missing group. There is no separate `--apply` mode.

Existing valid values are preserved unless explicitly selected for update.
Candidate configuration is loaded through the same `EnvResolver` used by
execution; stale-plan and unrecorded-mutation checks prevent a concurrent edit
from being silently overwritten. Secret values are never printed, and a new or
updated `.env` is written with user-only permissions.

## Validation and conformance

After a persisted candidate is available, bootstrap:

1. validates environment and package references;
2. performs the implemented Juicer symlink conformance when the package opts in
   and the chosen mode authorizes writes;
3. verifies every declared package canary;
4. groups configured method entries by Conda target and probes their required
   executables;
5. verifies every declared BUSCO lineage and every snapshot directly under the
   configured NCBI reference root; and
6. reports every configured package, method, database, target, missing
   executable, and repair hint before returning.

Bootstrap validates declared scope, not installation completeness for every
possible workflow. `server_check.sh` owns the full readiness of its documented
contig profile, including BUSCO and NCBI, while confirmed-run readiness checks
the exact selected closure. Therefore a core-only bootstrap may succeed and a
later contig readiness check may correctly fail until its databases or packages
are added.

This is readiness validation, not installation. The only implemented
package-layout mutation is the bounded Juicer link repair declared by the
package config. A general environment solver, downloader, scheduler setup, or
package installer is not implemented and must not be inferred from the word
“bootstrap.”

## Relationship to the two freezes

`config/envs.yaml` is mutable machine state. When the operator later confirms a
TUI plan, the whole-run freeze captures the selected environment/package
closure and any required typed database registry in
`control/dependency_manifest.json`. The frozen executor resolves from that
snapshot, not from later bootstrap or manual edits. Each executable method then
creates its narrower method-attempt freeze from the approved inputs and frozen
dependency selection.

The only implicit dotenv source is `.env` beside the project selected by the
active host mapping: for `config/envs.yaml` it is the repository-root `.env`;
for an explicit/`GENE_AGENT_ENVS_CONFIG` path under another `config/` directory
it is that project's root `.env`. A caller's current-directory `.env` is never
an alternate source. Already exported variables still win through setdefault
loading.

Bootstrap probes every package and method entry declared in the active machine
mapping. Separately, after a run's final confirmation and TTY check, both
structured run and TUI perform a read-only readiness probe for only the
canonical effective method closure and the mandatory LLM/database services for
that workflow. The exact same closure is then frozen into the dependency
snapshot. A failed run-specific probe creates no session.

Every locally executable method in that selected closure must resolve through
the active config to a non-empty `required_tools` and/or `package_tools`
declaration. Method-level entries may override a stage-level declaration, while
stage-level requirements remain valid user-authored configuration. A bare
default Conda target or an empty resolved requirement set fails closed, so a
configuration omission cannot become a zero-tool pass.
