# Project Bootstrap Architecture

**Status:** Accepted; implemented

**Accepted:** 2026-07-15

**Scope:** Minimal initialization of project-owned configuration consumed by
Gene Agent

## 1. Goal

`gene-agent bootstrap` should help a user create the project configuration that
Gene Agent actually consumes. It should generate as few files as possible and
should not create runtime, audit, or placeholder files for future features.

This specification defines only project configuration initialization. External
software conformance remains outside this design and can be handled separately
when needed.

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
  -> ask only for missing values
  -> show the files and fields that will change
  -> create or update the approved fields
  -> load the result through the existing runtime consumers
```

The initial implementation keeps the current `gene-agent bootstrap` entrypoint.
It adds initialization before the current validation behavior rather than
introducing a family of new commands.

## 4. Write Rules

- Preserve existing valid values.
- Do not overwrite a value unless the user explicitly approves the change.
- Do not add fields for tools or features that Gene Agent does not currently
  consume.
- Running bootstrap again with the same answers should make no changes.
- Keep secrets in `.env`, omit them from displayed plans and logs, and create a
  new `.env` with user-only permissions.
- Validate the generated files with the same loaders used by the application.

These rules are sufficient for the first implementation. More elaborate merge,
recovery, plugin, and package-management contracts remain future decisions.

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

## 6. Implementation Changes

1. Define the minimal fields currently consumed from `.env` and
   `config/envs.yaml`.
2. Let bootstrap create a missing `config/envs.yaml` instead of failing with a
   manual copy instruction.
3. Create `.env` only when required and preserve existing user values.
4. Validate the generated files with `_load_dotenv_without_override()` and
   `EnvResolver`.
5. Add tests for a new project, an existing configuration, idempotent rerun,
   secret redaction, and AutoHiC path collection.
