# Project Bootstrap Architecture

**Scope:** Minimal initialization of project-owned configuration consumed by
Gene Agent, followed by configured external-package and method-environment
conformance/preflight

## 1. Goal

`gene-agent bootstrap` creates or updates only configuration consumed by the
current application, validates it through the runtime loaders, applies approved
conformance actions for configured packages, and verifies package canary paths.
It does not create runtime, audit, or placeholder files without a current
consumer.

## 2. Managed Files

Bootstrap may assist with only these project files:

| File | Purpose |
|---|---|
| `config/envs.yaml` | Conda environment selection and tool/package/model paths consumed by execution |
| `.env` | Secrets and `GENE_AGENT_*` runtime overrides consumed by the entrypoint |

`config/envs.yaml` is created when it is missing. A newly generated file
includes the AutoHiC, Juicer, 3D-DNA, juicer-tools, and AutoHiC model fields
consumed by the Hi-C execution path; bootstrap asks for their concrete values
instead of writing an empty `packages` mapping. `.env` is created only when the
user supplies a secret or a non-default environment override. Bootstrap does
not create any additional state, report, marker, backup, or per-stage
configuration file by default.

Per-run facts such as input reads, assembly paths, restriction enzyme, sample
metadata, and stage choices do not belong in either file.

## 3. Bootstrap Flow

```text
inspect existing .env and config/envs.yaml
  -> identify values required by current Gene Agent consumers
  -> default: ask only for missing values
  -> --update: let the operator select an existing managed field to replace
  -> show the files and fields that will change
  -> create or update the approved fields
  -> load the result through the existing runtime consumers
  -> apply configured package conformance with --apply
  -> verify package canary paths
  -> probe every configured method in its resolved conda environment
  -> report each missing package path or executable with a repair hint
```

In normal initialization mode, bootstrap shows configuration and conformance
changes without writing them. `--apply` writes that plan, conforms packages that
opt in, and then performs package preflight plus a method-level environment
preflight. The method preflight resolves every configured method through the
same method -> stage -> default precedence used at execution time, runs
`conda run ... command -v` probes on the current host, and groups identical
conda targets so repeated tools are checked once. A failed check prints the
specific method, environment, missing executable or package path, and a repair
hint; bootstrap returns non-zero only after printing those details.

Running bootstrap against an existing config repeats both checks. A preview
without `--apply` still stops before writing a newly generated config, so it has
no persisted environment mapping to probe; the first `--apply` performs the
complete check immediately. TUI dependency freeze deliberately remains free of
conda probing.

`gene-agent bootstrap --update` is the explicit repair/update path for a value
that is already present. It lists the fields bootstrap owns, asks for the
replacement only after the operator selects it, and shows each replacement as
`old -> new` in the plan. Empty Enter at the final prompt validates and applies
the displayed plan; Ctrl-C or Ctrl-D cancels without writing. `--update` and
`--apply` are mutually exclusive. The default command preserves valid existing
values.

## 4. Write Rules

- Preserve existing valid values.
- Do not overwrite a value unless the user explicitly approves the change.
- Treat `config/envs.yaml` as the active machine configuration; do not create
  additional environment profiles from bootstrap.
- Do not add fields for tools or features that Gene Agent does not currently
  consume.
- Running bootstrap again with the same answers should make no changes.
- Keep secrets in `.env`, omit them from displayed plans and logs, and create a
  new `.env` with user-only permissions.
- Validate the generated files with the same loaders used by the application.

These rules define the implemented bootstrap boundary.

## 5. AutoHiC Setup Fields

For a newly initialized project, `config/envs.yaml` collects the paths that the
current AutoHiC execution code consumes:

- AutoHiC environment or prefix;
- AutoHiC installation root;
- Juicer installation root and Juicer tools path;
- 3D-DNA installation root;
- AutoHiC error model and chromosome model paths.

These are machine/project setup values and are loaded through `EnvResolver` by
the AutoHiC adapter. The Hi-C reads, adopted assembly, and restriction enzyme
remain run-level inputs handled by the Hi-C stage.
