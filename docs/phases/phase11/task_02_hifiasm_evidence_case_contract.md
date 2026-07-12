# Task 2: Hifiasm EvidenceCase And Methods Contract

**Phase**: 11
**Status**: Implemented and verified for the v3 methods-only contract
**Depends on**: Phase 10 assembly parameter catalog
**Updated**: 2026-07-12

## Goal

Represent curator-approved hifiasm paper evidence as small YAML + Markdown
pairs that can be validated, scored, and projected into bounded recommendation
prompts without storing or parsing the original PDFs.

## File Layout

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
├── cases.yaml
└── cases/hifiasm/papers/<paper-id>/
    ├── case.yaml
    └── methods.md
```

The manifest keeps `version: 2` and lists each case shard. A case shard may use a
top-level `cases:` list.

## EvidenceCase v3

The persistent closed schema contains:

- `schema_version: 3`;
- case identity and type;
- paper provenance under `source`;
- pack-root-relative `methods_path`;
- bounded curator-authored `evidence_spans`;
- ordinary applicability attributes such as `read_type`, `source_tool`, tool
  version, `target_methods`, and assembly goal;
- observed facts, intervention parameters/changes, rationale claims, outcomes,
  and curation status.

`read_type` and method identity are attributes for retrieval scoring. They do
not define discrete routing or eligibility in this task.

## Methods-Only Source Boundary

v3 deliberately removes the old mini-document archive:

- no `body.md`;
- no `source_documents` mapping;
- no source-file SHA256;
- no exact substring, offset, uniqueness, or document-coverage validation.

The loader validates `methods_path` as a confined regular UTF-8 file containing
non-whitespace text and no more than 256 KiB. Evidence span text is maintained
in YAML and does not have to occur verbatim in Markdown.

Runtime consumers continue receiving bounded excerpts in the stable shape:

```yaml
source_excerpts:
  - span_id: methods.command
    document: methods
    quote: Curator-authored evidence text.
```

Only referenced spans are projected. Closed-schema validation, field/text/span
budgets, curation eligibility, provenance, prompt budgets, and artifact/replay
hashes remain in force.

## Curator Authoring Rules

`methods.md` is the complete human-reviewed methods note for the paper. It can
retain commands, rationale, caveats, and mixed-workflow context needed for
future review, but the file is not copied wholesale into the LLM prompt.

`case.yaml.evidence_spans` is the bounded, model-visible evidence surface. Every
span must be referenced by at least one structured fact, intervention
parameter, rationale claim, or outcome. Conversely, any parameter, command, or
rationale that the model may use must be represented by a referenced span; text
that exists only in `methods.md` remains curator context. This reference-closure
rule, including evidence for inferred rationale, is a curator-authoring and
production-approval gate for this corpus. The general loader schema does not
make evidence spans mandatory for every inferred rationale in all callers.

Cross-method or mixed-pipeline information belongs in rationale or context by
default. Do not encode it as a target-method intervention effect or outcome.
`outcomes` currently contains only results attributable to the target-method
run; leave it empty when the paper reports only a combined pipeline result.

A paper directory may contain multiple entries in the same top-level `cases:`
list, named `hifiasm.paper.<paper-id>.<scenario-id>`, only when the scenarios
have materially different transferable L2 combinations, numeric applicability,
attributable outcomes, or transfer semantics. Do not split a paper mechanically
because it reports two commands.

The current contract deliberately adds no `scope`, `attribution`, `reviewer`,
or PDF-storage fields. Additions require a separate demonstrated runtime need;
they are not placeholders for information already preserved in `methods.md`.

## Current Curated Cases

The initial v3 corpus contains the four supplied paper summaries:

- `s41587-022-01261-x` — HiFi hifiasm with reported Hi-C and HiFi-only commands;
- `s41592-024-02269-8` — HiFi plus UL-ONT context and reported filtering;
- `s41586-026-10105-6` — ONT simplex hifiasm mode;
- `s41586-022-05325-5` — Trio hifiasm plus documentary downstream gap filling.

Hi-C, UL-ONT, Trio, and telomere details belong in `methods.md` and evidence
text; they are not topology conditions.

## Acceptance Criteria

- [x] v3 accepts one confined non-empty `methods.md` per case.
- [x] v2 source-document fields are rejected rather than adapted.
- [x] approved/draft/rejected eligibility semantics remain explicit.
- [x] downstream excerpts preserve stable span IDs and `document: methods`.
- [x] all four paper shards are listed in the manifest and load independently.
- [x] missing numeric facts remain missing.
