# CasePack Contract

> **Status:** the external loader contract is implemented; production recommendation
> code has no CasePack call site yet.
> **Owner:** `app/knowledge/cases.py`.
> **Code evidence:** `app/knowledge/cases.py`, `tests/test_case_library.py`.

## Boundary

```text
Knowledge Repo
        │
        │ CasePack V1
        ▼
Gene Agent
```

The repositories exchange versioned data. Gene Agent receives a CasePack root,
validates the complete snapshot once, and exposes immutable `CaseLibrary` records to
future in-memory consumers. Producer implementation and intermediate document formats
stay outside this repository.

## Layout

```text
<casepack-root>/
├── manifest.yaml
├── sources/<paper_id>/
│   ├── paper.md
│   ├── supplement.md
│   └── scripts/...
└── cases/
    ├── assembly/<case_id>.yaml
    ├── hic_scaffolding/<case_id>.yaml
    └── t2t/<case_id>.yaml
```

The manifest fixes the contract version and exact stage set:

```yaml
schema_version: 1
stages: [assembly, hic_scaffolding, t2t]
```

One Case represents one technical arm for one paper sample and stage. Its identifier is
`<paper_id>.<sample_id>.<stage>.<arm_slug>`.

## Load boundary

`load_case_library(root: Path)` requires an explicit CasePack root. It validates the
whole snapshot before returning an immutable `CaseLibrary`:

- manifest version and stage set;
- typed Assembly, Hi-C scaffolding, and T2T records;
- Case ID, filename, paper, sample, and stage agreement;
- closed enums and canonical units;
- source paths confined to `sources/<paper_id>/`;
- every evidence quote as an exact source-text substring;
- approved curation metadata and direct stage outcome;
- comparison-group context, status, distinct actions, and arm limit;
- optional `runtime_method_id` against the Gene Agent method registry;
- per-source and per-Case evidence budgets.

The loader returns records owned by the validated snapshot. Downstream code receives a
`CaseLibrary`, rather than a directory path, so retrieval cannot reopen or reinterpret
source files.

## Current runtime state

The loader and its contract tests are implemented. Current production recommendation
paths continue to use their existing evidence sources; `load_case_library` is called by
contract tests only.
