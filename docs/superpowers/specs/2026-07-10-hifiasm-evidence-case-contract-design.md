# Hifiasm EvidenceCase And Manual Source Contract Design

**Date:** 2026-07-10  
**Phase:** Phase 11 Task 2  
**Status:** Implemented

## Objective

Complete Phase 11 Task 2 by turning the existing hifiasm EvidenceCase
foundation into a strict, auditable input boundary for later numeric retrieval
and Methods-aware recommendation.

The implementation must accept a manually curated bundle consisting of one case
record, one normalized `body.md`, and one normalized `methods.md`; validate the
bundle deterministically; and expose only approved, prompt-safe evidence to the
production hifiasm selector.

## Scope

This design covers PacBio HiFi `hifiasm` EvidenceCase inputs only.

It includes:

- manifest and shard loading contracts;
- assembly-specific initial and retry case validation;
- source path, hash, span, and provenance validation;
- deterministic eligibility of approved cases;
- exclusion of draft, rejected, legacy, and source-invalid cases from production
  retrieval;
- compact prompt/retrieval views that do not contain full source documents;
- focused tests and Phase 11 documentation handoff.

It does not add PDF parsing, OCR, GROBID, embeddings, vector search, GraphRAG,
LLM Wiki generation, cross-tool parameter translation, or automatic evidence
promotion.

## Architectural Boundaries

### Knowledge-pack loading

`app/knowledge/packs.py` owns knowledge-pack structure only:

- load the case manifest and referenced shards;
- validate confined relative paths;
- reject malformed manifest and shard containers instead of silently treating
  them as empty;
- preserve mixed rollout support for non-hifiasm cases;
- avoid reading `body.md` or `methods.md`.

For an evidence manifest, the loader validates `version`, `case_schema`, and
`case_files`. This boundary returns structured case metadata and the pack root.
It does not decide whether a case is scientifically or operationally eligible.

### Generic evidence source validation

`app/knowledge/evidence_cases.py` owns filesystem-backed source validation:

- absolute path, `..`, missing path, non-file, and symlink escape rejection;
- SHA256 format and content verification;
- required body and Methods documents for approved cases;
- unique, non-empty span IDs;
- valid document references;
- exact quote presence and single occurrence;
- prompt-safe excerpt output without complete document contents.

This module remains domain-neutral and does not know hifiasm initial/retry
semantics.

### Assembly EvidenceCase validation

`app/domains/assembly/evidence_case.py` owns hifiasm evidence semantics:

- schema version and unique case identity;
- source identity;
- HiFi/hifiasm applicability;
- initial versus retry contracts;
- observed fact and outcome provenance;
- reported parameter provenance from Methods;
- explicit versus inferred rationale provenance;
- curation status and retrieval eligibility;
- normalized compact case views.

The module validates approved cases completely. Draft and rejected cases may
remain incomplete for curator workflow, but their normalized representation is
explicitly ineligible for retrieval.

### Retrieval and prompt consumers

`app/domains/assembly/case_selector.py`, the numeric retriever, and prompt
payload code consume normalized EvidenceCase results. They must not duplicate
manifest, filesystem, checksum, quote, or provenance validation.

The production hifiasm path selects only cases that are both:

- EvidenceCase schema version 2; and
- approved and fully source-validated.

Other assembly methods retain their existing path until separately migrated.

## Contract Details

### Manifest and shard contract

An evidence manifest declares:

```yaml
version: 2
case_schema: assembly_evidence_case
case_files:
  - cases/hifiasm/initial.yaml
  - cases/hifiasm/retry.yaml
```

`case_files` must be a list of non-empty relative strings. Referenced files must
remain inside the pack root and must contain a mapping with a `cases` list.
Malformed evidence manifests or shards are configuration errors, not empty
evidence.

Mixed manifests may continue referencing legacy shards for non-hifiasm methods.
A legacy hifiasm record is never silently adapted and never used by the
production hifiasm selector.

### Initial case contract

An initial case records pre-run facts, a hifiasm intervention, rationale, and
outcomes. It must not depend on retry-only failure context or be retrieved as
failure-specific evidence.

### Retry case contract

A retry case requires:

- `previous_attempt` as a mapping;
- `previous_attempt.parameters` as a mapping;
- a meaningful `failure_mode` or `execution_failure_class`;
- `intervention.parameter_changes` as a non-empty mapping;
- each parameter change to declare both `from` and `to`.

These requirements keep retry evidence distinct from successful initial cases
and provide a stable handoff to the future retry-specific retriever.

### Curation and validation

- `draft`: retained for curator work, never retrieval-eligible;
- `approved`: requires complete schema, sources, hashes, spans, and provenance;
- `rejected`: retained for audit, never retrieval-eligible.

An invalid approved case fails validation immediately. It is never skipped based
on file order.

### Compact representation

Source documents are loaded only during approved-case validation. The validated
consumer view contains only referenced exact excerpts and structured evidence.
It does not expose full body/Methods text or source-document metadata to compact
graph state or prompt payloads.

## Error Handling

All validation failures are deterministic configuration errors. Messages identify
as much of the following context as applicable:

- case ID;
- manifest or shard path;
- field name;
- span ID;
- source path;
- checksum mismatch;
- initial/retry contract failure.

No invalid approved case is silently skipped.

## Testing Strategy

Focused deterministic tests cover:

1. valid evidence manifest and shard loading;
2. invalid manifest version, schema, `case_files`, and shard `cases` container;
3. absolute paths, traversal, missing files, and symlink escape;
4. missing, duplicate, unknown, absent, and ambiguous spans;
5. missing approved hashes and checksum mismatch;
6. valid initial and retry cases and their distinct required fields;
7. reported parameters citing Methods spans;
8. explicit rationale citing Methods and inferred rationale remaining labeled;
9. draft and rejected exclusion from production retrieval;
10. legacy hifiasm exclusion without compatibility conversion;
11. one complete manual case/body/Methods bundle through the production loading
    boundary;
12. absence of full source documents and `source_documents` metadata from the
    prompt-safe compact view.

Tests will use the repository's pytest-based test suite. Relevant Phase 11 and
assembly regression tests will run before documentation is marked complete.

## Documentation And Handoff

Implementation and verification completed the following handoff:

- mark Phase 11 Task 2 complete and check its acceptance criteria;
- remove duplicated wording in the Task 2 source-hash section;
- update the Phase 11 overview to state that Task 3 starts from validated,
  normalized EvidenceCase inputs;
- keep Tasks 3 and 4 responsible only for retrieval/ranking and recommendation,
  respectively;
- make only small Task 3/4 integration edits when required to consume the clean
  boundary, without broadening this work into their full completion.

## Acceptance

The design is complete when all Task 2 acceptance criteria are covered by code,
tests, and documentation, while Task 3/4 consumers reuse the resulting boundary
rather than reimplementing it.
