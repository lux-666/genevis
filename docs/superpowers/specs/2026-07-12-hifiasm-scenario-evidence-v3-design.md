# Hifiasm Paper EvidenceCase v3 Design

**Date:** 2026-07-12
**Status:** Implemented historical scope; the production corpus was subsequently expanded to 10 cases

## 1. Scope

This original scoped change turned four curated hifiasm papers into small,
reviewable knowledge units. Each paper is represented by exactly two files:

```text
cases/hifiasm/papers/<paper-id>/
├── case.yaml
└── methods.md
```

The YAML contains structured retrieval and recommendation facts. The Markdown
contains the human-readable applicability, reported commands, rationale, and
transfer caveats supplied by the curator.

This change does not inspect or archive the original PDFs. It also does not add
Hi-C, ultra-long ONT, Trio, or assembly-goal routing.

## 2. Remove the old source-document contract

EvidenceCase v3 is methods-only. Delete the following v2 concepts rather than
keeping compatibility adapters:

- `body.md`;
- `source_documents`;
- curator-file SHA256 fields;
- exact quote-to-file substring validation;
- quote offsets, uniqueness checks, and document-coverage checks.

Repository artifact/replay hashes are unrelated runtime provenance and remain
unchanged.

## 3. Persistent case shape

```yaml
schema_version: 3
case_id: paper_example
case_type: initial

source:
  kind: paper
  title: Example title
  doi: 10.x/example
  year: 2024

methods_path: cases/hifiasm/papers/example/methods.md

evidence_spans:
  - span_id: methods.command
    quote: Curator-authored bounded evidence text.

applicability:
  read_type: hifi
  source_tool: hifiasm
  source_tool_version: 0.19.5-r587
  target_methods: [hifiasm]
  assembly_goal: near_t2t

observed_facts: {}
intervention:
  tool: hifiasm
  version: 0.19.5-r587
  parameters: {}
  parameter_changes: []
rationale_claims: []
outcomes: {}
curation:
  status: approved
  curator: knowledge-team
  reviewed_at: 2026-07-12
  notes: Curated from the supplied paper summary.
```

The schema remains closed and retains existing field and text budgets.
`methods_path` is relative to the knowledge-pack root.

## 4. Markdown validation

The loader validates only the file named by `methods_path`:

- the resolved path stays inside the pack root;
- it is a regular file;
- it is valid UTF-8;
- it contains non-whitespace text;
- it is at most 256 KiB.

Evidence span text is curator-authored YAML content. It does not need to occur
verbatim in `methods.md`.

For existing downstream consumers, validated cases synthesize the runtime shape:

```yaml
source_excerpts:
  - span_id: methods.command
    document: methods
    quote: Curator-authored bounded evidence text.
```

Only spans referenced by retained facts, parameters, rationale claims, or
outcomes are projected downstream.

## 5. Retrieval semantics

`read_type` and `target_methods` are ordinary case attributes. Current retrieval
uses similarities and mismatches only as scoring features. Neither attribute is
a discrete eligibility filter.

Consequences:

- a mismatched method or read type may lower a candidate's score but does not
  remove it;
- missing numeric values remain missing and are never converted to zero;
- Hi-C, UL-ONT, Trio, and assembly topology are not query dimensions;
- `assembly_goal` is descriptive metadata for this change, not a hard filter;
- a future configuration may give method identity an effectively infinite
  weight, which would approximate method routing without changing the case
  contract.

The broader assembly method catalog remains four method families:
`hifiasm`, `hifiasm-ont` (internally `hifiasm_ont` where required), `flye`, and
`nextdenovo`. This change only adds the four supplied hifiasm-family paper cases.

## 6. Curated paper cases

### `s41587-022-01261-x`

- attributes: `read_type: hifi`, target method `hifiasm`;
- records hifiasm `0.16.1-r375` commands for HiFi + Hi-C and HiFi-only use;
- explains graph accuracy plus chromosome-scale phase links;
- does not create a Hi-C routing condition.

### `s41592-024-02269-8`

- attributes: `read_type: hifi`, target method `hifiasm`;
- records hifiasm `0.19.5-r587`, UL-read filtering commands, and the reported
  HiFi + UL-ONT + Hi-C command;
- documents that filtering aimed to avoid assembly errors and reduce runtime;
- does not map `seqkit -m` to hifiasm `ul_cut`;
- keeps `hom_cov` documentary/read-only rather than transferable L2 evidence.

### `s41586-026-10105-6`

- attributes: `read_type: ont`, target method `hifiasm_ont`;
- records hifiasm `0.25.0-r726` and the reported `--ont --dual-scaf --telo-m`
  command;
- treats `--ont` as route-owned and `dual_scaffold` as transferable catalog
  evidence;
- documents that `CCCTAA` is human-specific and does not promote it as a
  generally transferable recommendation.

### `s41586-022-05325-5`

- attributes: `read_type: hifi`, target method `hifiasm`;
- records the reported yak and hifiasm v0.3 test commands;
- states that the final reference assembly used hifiasm v0.14.1 Trio mode but
  the complete final command was not reported and must not be reconstructed;
- keeps Flye, Medaka, Merfin, and gap filling as downstream documentary context,
  not hifiasm parameter evidence.

## 7. Manifest and curation

For this original change, the v2 pack manifest listed four paper shards. The
obsolete empty `cases/hifiasm/initial.yaml` is deleted. Each new case is
`approved`; draft and rejected eligibility rules remain unchanged. The current
production manifest contains 10 approved paper shards; Phase 11 task documents
are authoritative for the completed corpus inventory.

## 8. Verification

The original focused tests covered:

- methods-only schema and path validation;
- rejection of removed v2 fields;
- runtime `source_excerpts` synthesis;
- scoring without method/read-type hard filtering;
- preservation of missing numeric values;
- loading all four approved paper shards;
- unchanged attestation, prompt budgets, and artifact/replay provenance.

The completed Phase 11 suite now asserts the exact 10-case production inventory
and validates every shard through the real loader and prompt projection path.
