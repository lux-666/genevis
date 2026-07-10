# Task 2: Hifiasm EvidenceCase And Manual Source Contract

**Phase**: 11
**Status**: Complete
**Depends on**: Phase10 Task14 typed parameter-attempt contract
**Created**: 2026-07-10
**Updated**: 2026-07-10

## Goal

Replace the hifiasm recommender's seed-observation input with a validated,
traceable evidence contract that is authored manually outside the runtime.

The project does not ingest PDFs in this task. A curator prepares three aligned
inputs:

1. one structured case YAML entry,
2. one normalized article or internal-run body document,
3. one normalized Methods document.

The runtime consumes those prepared inputs, validates their references, and
uses only approved evidence cases for hifiasm recommendation.

## Implemented Contract

- legacy hifiasm seed observations are ignored by the production selector;
- case IDs, case type, applicability, source identity, intervention, and
  curation status are validated;
- manifest and shard structure is validated without silently turning malformed
  inputs into empty evidence;
- approved body/Methods paths are confined to the knowledge-pack root;
- approved source hashes and exact, unambiguous quotes are verified;
- numeric facts, reported parameters, and explicit rationale claims validate
  their span provenance;
- only prompt-safe exact excerpts are returned, never complete documents;
- initial and retry contracts are validated separately;
- draft and rejected cases are retained as ineligible audit records;
- empty initial/retry shards provide the manual curation landing point.

## Scope

This task covers the PacBio HiFi `hifiasm` method only.

In scope:

- the current EvidenceCase schema;
- one-to-one case/body/Methods source references;
- stable evidence excerpts with source validation;
- initial and retry case types;
- explicit separation of reported facts and curator/system inference;
- evidence curation status;
- deterministic loader and validation errors;
- source hashes and prompt-safe excerpts;
- migration treatment for the current synthetic seed cases.

Out of scope:

- PDF download, parsing, GROBID, OCR, or automatic section detection;
- GraphRAG, graph databases, or LLM Wiki generation;
- embedding generation or vector databases;
- Flye, NextDenovo, or hifiasm-ont evidence recommendation;
- automatic promotion of runtime records into the production knowledge pack;
- cross-tool parameter translation.

## First-Release Directory Contract

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
├── cases.yaml
├── cases/
│   └── hifiasm/
│       ├── initial.yaml
│       └── retry.yaml
└── sources/
    └── <case_id>/
        ├── body.md
        └── methods.md
```

`cases.yaml` remains a manifest. It identifies the schema version and case
shards. Approved source documents are read only during source validation and
are never retained in compact graph state or prompt payloads.

```yaml
version: 2
case_schema: assembly_evidence_case
case_files:
  - cases/hifiasm/initial.yaml
  - cases/hifiasm/retry.yaml
```

The runtime must reject absolute paths, `..` traversal, symlink escape, or any
source reference outside the knowledge-pack root.

## EvidenceCase

```yaml
cases:
  - schema_version: 2
    case_id: hifiasm_hifi_example_001
    case_type: initial

    source:
      kind: paper
      title: "Example genome assembly study"
      doi: "10.xxxx/example"
      year: 2025
      internal_run_id: null

    applicability:
      read_type: hifi
      source_tool: hifiasm
      source_tool_version: "0.21.0"
      target_methods:
        - assembly.candidate.hifiasm
      assembly_goal: primary

    observed_facts:
      organism:
        value: Example species
        evidence_span_ids: [body.organism]
      clade:
        value: vertebrate
        evidence_span_ids: [body.organism]
      genome_size_mb:
        value: 1200
        uncertainty:
          kind: point
        evidence_span_ids: [methods.genome_context]
      coverage:
        value: 32
        unit: x
        evidence_span_ids: [methods.read_context]
      read_n50_kb:
        value: 18.4
        evidence_span_ids: [methods.read_context]
      heterozygosity:
        value: 0.012
        unit: fraction
        evidence_span_ids: [methods.genome_context]
      ploidy:
        value: 2
        evidence_span_ids: [body.organism]

    intervention:
      tool: hifiasm
      version: "0.21.0"
      parameters:
        purge_level:
          value: 1
          reported: true
          evidence_span_ids: [methods.assembly_command]
        kmer_length:
          value: 51
          reported: true
          evidence_span_ids: [methods.assembly_command]

    rationale_claims:
      - claim_id: preserve_haplotype_signal
        statement: "The chosen configuration was intended to preserve heterozygous sequence while producing a primary assembly."
        explicitness: explicit
        effect_area: haplotype_preservation
        evidence_span_ids: [methods.parameter_rationale]

    outcomes:
      assembly_size_mb:
        value: 1188
        evidence_span_ids: [body.assembly_result]
      contig_n50_mb:
        value: 36.2
        evidence_span_ids: [body.assembly_result]
      busco_complete:
        value: 0.972
        evidence_span_ids: [body.quality_result]
      busco_duplicate:
        value: 0.021
        evidence_span_ids: [body.quality_result]

    source_documents:
      body:
        path: sources/hifiasm_hifi_example_001/body.md
        sha256: null
      methods:
        path: sources/hifiasm_hifi_example_001/methods.md
        sha256: null
      spans:
        - span_id: body.organism
          document: body
          quote: "Exact organism and ploidy statement copied from body.md."
        - span_id: methods.genome_context
          document: methods
          quote: "Exact genome-size and heterozygosity statement copied from methods.md."
        - span_id: methods.read_context
          document: methods
          quote: "Exact coverage and read-N50 statement copied from methods.md."
        - span_id: methods.parameter_rationale
          document: methods
          quote: "Exact curator-copied rationale text from methods.md."
        - span_id: methods.assembly_command
          document: methods
          quote: "Exact command or parameter sentence copied from methods.md."
        - span_id: body.assembly_result
          document: body
          quote: "Exact assembly-size and contig-N50 result copied from body.md."
        - span_id: body.quality_result
          document: body
          quote: "Exact BUSCO result copied from body.md."

    curation:
      status: draft
      curator: null
      reviewed_at: null
      notes: null
```

The YAML may omit unavailable optional facts. It must not invent placeholder
numbers solely to satisfy the schema.

Source identification is required. Paper cases use `source.kind: paper` plus a
DOI or other stable citation when available. Internal cases use
`source.kind: internal_run` plus the immutable run/session identifier and the
relevant artifact refs.

## Source Span Contract

A span is represented by an exact quote stored in the case YAML and a document
reference stored under `source_documents`.

The validator must verify that:

- every referenced `span_id` exists;
- each quote occurs in the declared source document;
- an exact quote expected to identify a single passage is not ambiguous;
- a quote is at most 2,000 characters and cannot equal the complete source
  document after trimming surrounding whitespace;
- every evidence-bearing numeric fact cites at least one span;
- every reported source parameter cites at least one Methods span;
- every explicit rationale claim cites at least one Methods span;
- inferred claims are labeled `explicitness: inferred` and cannot masquerade as
  source-reported statements.

Exact quotes avoid fragile line-number references and do not require custom
markers inside the manually prepared Markdown documents.

Source SHA256 values may initially be `null` while a case is in `draft` status.
An `approved` case must have source hashes generated and verified by the
knowledge-pack validation command before it is retrieval-eligible.

## Initial And Retry Case Separation

`case_type: initial` records a pre-run context, source parameters, rationale, and
outcomes.

`case_type: retry` additionally requires:

```yaml
previous_attempt:
  parameters: {}
  failure_mode: under_purged
  execution_failure_class: null
  review_metrics: {}

intervention:
  parameter_changes:
    purge_level:
      from: 2
      to: 1
```

An initial query must never retrieve a retry case. A retry query must not treat
an initial success case as evidence for a failure-specific parameter change
unless a later task explicitly introduces a separate background-evidence role.

## Reported Facts Versus Inference

The runtime must keep three concepts separate:

- `observed_facts`: values reported by the paper or internal run record;
- `rationale_claims`: explicit statements or clearly labeled curator inference;
- recommendation reasoning: generated at runtime for the current query.

Runtime reasoning must not be written back into an approved source case.

## Curation Status

Allowed values:

- `draft`: structurally incomplete or awaiting review; never retrieval-eligible;
- `approved`: passes schema, source, and hash validation; retrieval-eligible;
- `rejected`: retained for audit but never retrieval-eligible.

The production selector consumes only `approved` cases.

## Synthetic Seed Cases

The current 2026 `internal seed case` records are not treated as scientific
evidence. During implementation they must be either:

1. migrated to the current contract as `draft` fixtures and excluded from
   production retrieval; or
2. moved into test fixtures.

There must not be a silent compatibility conversion for hifiasm production
selection. Hifiasm uses one current case schema after Task 2.

Other assembly methods may continue using their existing selector until their
own future migration; that is a method-scoped rollout, not two active hifiasm
contracts.

## Proposed Code Boundaries

```text
app/knowledge/evidence_cases.py
    Generic manifest, path, hash, curation, and source-span validation.

app/domains/assembly/evidence_case.py
    Assembly-specific EvidenceCase field validation and normalized view.

app/knowledge/packs.py
    Loads the manifest and structured case metadata without eagerly injecting
    complete source documents into prompt state.
```

`app/domains/assembly/case_selector.py` must not absorb schema loading,
filesystem validation, numeric distance, prompt assembly, and retry logic into
one class.

## Errors

Validation failures are deterministic configuration errors. They must identify:

- case ID;
- field or span ID;
- invalid source path;
- missing or ambiguous quote;
- checksum mismatch;
- unsupported schema version;
- invalid curation status;
- initial/retry contract violation.

One invalid approved case fails knowledge-pack validation. It must not be
silently skipped because that would make production evidence depend on file
ordering.

## Tests

Implemented coverage includes:

- evidence manifest and shard loading;
- fail-closed validation for every schema-v2 record and any current-shape
  hifiasm record with a missing/unsupported schema version;
- source path confinement;
- exact quote validation, complete-document rejection, and a 2,000-character
  excerpt ceiling;
- missing, duplicate, and ambiguous span IDs;
- missing source hashes on approved cases;
- checksum mismatch;
- finite point/interval validation for approved numeric facts and outcomes,
  plus biological domain constraints for the five retrieval facts;
- initial/retry required fields;
- reported parameter provenance;
- inferred rationale labeling;
- draft and rejected case exclusion;
- no eager inclusion of full body/Methods text in compact state.

## Acceptance Criteria

- [x] A manually prepared case, body document, and Methods document load as one
  validated EvidenceCase.
- [x] Approved cases have verified source hashes and valid exact-quote spans.
- [x] Malformed schema-v2 cases fail validation instead of being silently
  discarded.
- [x] Approved numeric retrieval facts have finite, interval-consistent schema
  validation.
- [x] Initial and retry cases have distinct validated contracts.
- [x] Draft, rejected, legacy, and source-invalid cases cannot enter hifiasm
  production retrieval.
- [x] Full source documents remain ref-backed and out of compact graph state.
- [x] Existing synthetic seed cases are no longer production evidence for
  hifiasm.
- [x] No PDF ingestion, GraphRAG, LLM Wiki, embedding, or cross-tool mapping is
  introduced.
