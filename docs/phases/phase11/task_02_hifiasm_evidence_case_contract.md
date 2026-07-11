# Task 2: Hifiasm EvidenceCase And Manual Source Contract

**Phase**: 11
**Status**: Complete (contract and validation); production cutover deferred to Task 6
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

The EvidenceCase backend consumes those prepared inputs, validates their
references, and exposes only approved cases as retrieval-eligible. Enabling
that backend in the production selector remains a Task 6 cutover decision.

## Implemented Contract

- schema-v2 EvidenceCase validation does not silently convert legacy hifiasm
  seed observations;
- case IDs, case type, applicability, source identity, intervention, and
  curation status are validated;
- top-level and nested mappings use explicit closed allowlists; unknown fields
  fail closed with deterministic field paths;
- manifest and shard structure is validated without silently turning malformed
  inputs into empty evidence;
- records from a v2 `assembly_evidence_case` manifest's inline cases and
  `cases/hifiasm/*` shards are selected by manifest/shard provenance, not by
  guessing from the record's tool fields; each such record must declare the
  exact non-boolean integer `schema_version: 2`;
- approved body/Methods paths are confined to the knowledge-pack root;
- approved source hashes and exact, unambiguous quotes are verified;
- numeric facts, reported parameters, and explicit rationale claims validate
  their span provenance;
- the normalized case is recursively rebuilt from allowed fields rather than
  copied from curator input, and `source_documents` is never prompt-visible;
- Task 2 records an in-process attestation over the complete normalized case,
  including prompt-visible evidence, source excerpts, curation status, and
  retrieval eligibility; ordinary mappings cannot self-assert validation, and
  any post-validation semantic modification fails closed before Task 3 or
  Task 4;
- Task 3 obtains a controlled validated snapshot before reading hard-filter or
  ranking fields, then owns the private one-way `validated -> ranked`
  transition; the public Task 2 prompt projector never accepts or re-casts
  caller-supplied `numeric_match` metadata;
- prompt-visible projection removes the complete `curation` audit object,
  including `status`, `curator`, `reviewed_at`, and `notes`;
- free-text fields in the normalized case are individually bounded to 1,000
  characters and the structured case has a 16,000-character text budget;
- source evidence is bounded to 2,000 characters per span, 32 unique span IDs
  per case, 12,000 referenced quote characters per document, and 16,000 total
  declared/prompt-visible quote characters per case; the prompt budget check is
  performed after normalized excerpts are attached (`16,000` passes and
  `16,001` fails);
- unique quote offsets are retained only during validation; referenced
  intervals are merged per document and must cover less than 90% of that
  document, preventing multi-span full/near-full document reconstruction;
- only referenced, validated exact excerpts are returned, never complete
  documents;
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
- contract treatment for the current synthetic seed cases without silent
  compatibility conversion.

Out of scope:

- PDF download, parsing, GROBID, OCR, or automatic section detection;
- GraphRAG, graph databases, or LLM Wiki generation;
- embedding generation or vector databases;
- Flye, NextDenovo, or hifiasm-ont evidence recommendation;
- automatic promotion of runtime records into the production knowledge pack;
- cross-tool parameter translation;
- retry-specific retrieval semantics, which belong to Task 5;
- production selector activation and evaluation, which belong to Task 6.

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
are never retained in the normalized EvidenceCase, compact graph state, or
prompt payloads.

Within this mixed migration manifest, inline cases and records loaded from
`cases/hifiasm/*` are strict schema-v2 hifiasm records. Legacy shards at other
paths remain a separate input for methods that have not migrated; their
records are not inferred to be v2 hifiasm cases from `source_tool`,
`target_methods`, or `intervention.tool` values.

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
- each exact quote has one unique offset, using overlap-aware matching (for
  example, `aa` is ambiguous in `aaa` at offsets 0 and 1);
- a quote is at most 2,000 characters and cannot equal the complete source
  document after trimming surrounding whitespace;
- referenced quote lengths total at most 12,000 characters per document;
- referenced intervals are merged by offset per document, cannot cover the
  complete document, and must cover less than 90% of its characters;
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

The source bundle is a closed schema:

- `source_documents` allows only `body`, `methods`, and `spans`;
- each document allows only `path` and `sha256`;
- each span allows only `span_id`, `document`, and `quote`;
- paths and span IDs are non-empty and bounded to 1,000 characters;
- one quote is bounded to 2,000 characters;
- one case is bounded to 32 declared spans and 16,000 total quote characters;
- one document is bounded to 12,000 referenced quote characters and less than
  90% merged referenced coverage.

Unknown fields such as `source_documents.methods.full_text` fail validation;
complete document bodies cannot be embedded as alternate metadata.

## Closed Schema And Prompt-Safe Normalization

The schema-v2 validator uses nested allowlists for `source`, `applicability`,
facts, uncertainty, intervention parameters and changes, rationale claims,
outcomes, curation metadata, and retry metadata. Dynamic parameter and metric
names are allowed only inside their documented mapping positions; their values
still follow the declared scalar or nested schema.

Normalization constructs a fresh mapping recursively from those allowlists. It
does not deep-copy curator input and remove only one known-dangerous field.
Consequently fields such as `source.full_text`,
`intervention.methods_text`, `observed_facts.<name>.notes`, a free-form
top-level `rationale`, or unknown rationale-claim fields fail closed rather
than reaching compact state or a prompt.

Allowed strings remain evidence data, not executable instructions. They must
be non-empty, are bounded to 1,000 characters each, and contribute to a
16,000-character structured-text budget per case. Approved rationale claims
require non-empty `claim_id`, `statement`, `explicitness`, and `effect_area`;
approved intervention parameter entries require both `value` and boolean
`reported`. Provenance rules still require reported parameters and explicit
rationale claims to cite Methods spans.

Contract identifiers are typed rather than accepted through a generic scalar:
source `title`, `doi`, and `internal_run_id` values, approved
`curation.curator`, and retry `failure_mode` /
`execution_failure_class` labels are non-empty strings when present, with an
approved curator required and at least one retry failure label required.
Parameter `value`, previous-attempt parameter values, and review metrics retain
the documented mixed finite JSON-scalar behavior (`null`, boolean, number, or
bounded non-empty string).

`project_hifiasm_prompt_safe_case()` is the public Task 2 projection. It
rebuilds the single allowlisted schema only after checking a validation-owned
attestation for an exact `validated` live object. The attestation binds a
deterministic serialization of the complete mapping plus its stage kind,
original curation status, and retrieval-eligibility decision. For a validated
case this covers all Task 2-owned semantics: source and applicability profile,
observed facts, intervention parameters and changes, rationale, outcomes,
retry context, curation data, validated excerpts, and retrieval eligibility.
The public projector accepts no numeric argument and returns only a Task 2
`prompt_base` object. That object is intentionally not a Task 4 input.

`project_hifiasm_retrieval_case()` accepts only a complete, unchanged validated
case and returns a controlled deep copy. Task 3 calls it before reading case ID,
curation, eligibility, compatibility fields, numeric facts, or ranking inputs.
It therefore does not trust a mutable mapping's self-declared status or
`retrieval_eligible` flag. Task 3 alone may perform the private one-way
`validated -> ranked` transition that attaches `numeric_match`; that transition
also requires the attested original decision to be `approved` and
retrieval-eligible. Task 4 accepts only this Task 3-owned `ranked` kind. A
validated approved case cannot bypass Task 3 by being sent directly to Task 4,
and a ranked case cannot be passed back through either Task 2 projection to
replace or re-attest its numeric metadata.

The registered mapping type rejects direct construction. Its controlled
`copy.copy()` and `copy.deepcopy()` paths first require a live registry entry
and register the new identity with the same immutable attestation. Converting
to a plain `dict`, serialization, or constructing the mapping subclass through
`dict.__new__` intentionally loses attestation. Lookup verifies both object ID
and weak-reference identity, preventing stale ID reuse; weak-reference cleanup
keeps no strong case reference. The projection rejects duplicate `span_id`
values and applies prompt text/excerpt budgets after normalized excerpts are
attached. Self-supplied, replaced, removed, or appended excerpts—and changes
to facts, rationale, parameters, source/profile fields, status, eligibility, or
Task 3 numeric metadata—therefore fail closed.

This registry is an application-process integrity and misuse-detection
mechanism. It is not claimed as a security boundary against an actor with
arbitrary Python reflection, monkeypatching, or code-execution capability, and
the design does not rely on underscore naming, closure privacy, or an alleged
unforgeable Python token for that stronger threat model.

The shared nested-field projector omits `source_documents`,
`retrieval_eligible`, and the complete `curation` audit object. The public Task
2 projector never includes `numeric_match`; the private Task 3 transition adds
it only while minting a ranked case. Task 4 verifies and copies that ranked
case through the same shared nested schema implementation rather than
maintaining a second EvidenceCase schema. This integration boundary does not
claim that Task 5 retry retrieval or Task 6 production cutover is complete.

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

When the schema-v2 EvidenceCase backend is enabled, it consumes only `approved`
and source-validated cases. The repository's production cutover flag remains
disabled pending Task 6 evaluation.

## Synthetic Seed Cases

The current 2026 `internal seed case` records are not treated as scientific
evidence. During implementation they must be either:

1. migrated to the current contract as `draft` fixtures and excluded from
   production retrieval; or
2. moved into test fixtures.

There must not be a silent compatibility conversion from these records into
schema-v2 EvidenceCases. Task 2 establishes one current EvidenceCase schema;
Task 6 decides when production selection cuts over from the existing backend.

Other assembly methods may continue using their existing selector until their
own future migration; that is a method-scoped rollout, not two active hifiasm
contracts.

## Proposed Code Boundaries

```text
app/knowledge/evidence_cases.py
    Generic manifest, path, hash, curation, and source-span validation.

app/domains/assembly/evidence_case.py
    Assembly-specific EvidenceCase field validation, strict hifiasm-shard
    provenance isolation, and the public prompt-safe projection.

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
- fail-closed full-chain validation for every v2 hifiasm-shard record,
  including missing/non-integer schema versions and missing, misspelled, or
  wrongly typed tool identifiers;
- independent handling of legacy seed records outside hifiasm v2 shards;
- source path confinement;
- overlap-aware exact quote validation, complete-document rejection,
  full/near-full multi-span coverage rejection, and a 2,000-character excerpt
  ceiling;
- missing, duplicate, and ambiguous span IDs;
- missing source hashes on approved cases;
- checksum mismatch;
- finite point/interval validation for approved numeric facts and outcomes,
  plus biological domain constraints for the five retrieval facts;
- initial/retry required fields;
- reported parameter provenance;
- inferred rationale labeling and required rationale-claim fields;
- draft and rejected case exclusion;
- recursive prompt-safe reconstruction with nested unknown-field rejection;
- no eager inclusion of full body/Methods text in compact state;
- per-field and per-case structured-text limits plus per-span, span-count,
  per-document quote/coverage, and per-case total-quote limits;
- strict text typing for source identifiers, approved curator identity, and
  retry failure labels while preserving mixed JSON scalar parameter values;
- deep, allowlisted behavior of the public Task 2 prompt-safe projection;
- public Task 2 projection rejecting ranked input and any caller-supplied
  numeric argument, with approved validated cases unable to enter Task 4
  directly;
- validation-owned full-semantic weak identity attestation with exact-identity
  lookup, GC cleanup, controlled copy/deepcopy, and explicit process-local
  threat-model limits;
- fail-closed ordinary-dict, direct-construction, replacement, addition,
  removal, synchronized-tampering, and duplicate-span attacks against
  `source_excerpts`, including the Task 2 -> Task 3 -> canonical Task 4 prompt
  chain;
- fail-closed draft/rejected self-promotion and post-validation mutation of
  rationale, facts, parameters, source/applicability profile, status,
  eligibility, or Task 3 numeric prompt metadata before Task 3/Task 4 use;
- exact 16,000/16,001 total excerpt-character boundaries after projection and
  complete removal of curation audit fields from the final prompt;
- test-only activation of the numeric backend for full-chain EvidenceCase
  validation without changing the production cutover flag.

## Acceptance Criteria

- [x] A manually prepared case, body document, and Methods document load as one
  validated EvidenceCase.
- [x] Approved cases have verified source hashes and valid exact-quote spans.
- [x] Malformed schema-v2 cases fail validation instead of being silently
  discarded.
- [x] V2 hifiasm shard provenance, not self-declared tool fields, determines
  which records must satisfy exact `schema_version: 2` validation.
- [x] Approved numeric retrieval facts have finite, interval-consistent schema
  validation.
- [x] Initial and retry cases have distinct validated contracts.
- [x] Draft, rejected, legacy, and source-invalid records cannot enter the
  schema-v2 EvidenceCase retrieval result.
- [x] Full source documents remain ref-backed and out of compact graph state.
- [x] Nested unknown fields fail closed and prompt-safe normalization rebuilds
  only documented fields under bounded text and excerpt budgets.
- [x] Multiple excerpts cannot reconstruct a complete or near-complete source
  document through overlapping or adjacent span chains.
- [ ] Production selection no longer uses existing synthetic seed cases.
  Activation is intentionally deferred to Task 6 evaluation and cutover.
- [x] No PDF ingestion, GraphRAG, LLM Wiki, embedding, or cross-tool mapping is
  introduced.
