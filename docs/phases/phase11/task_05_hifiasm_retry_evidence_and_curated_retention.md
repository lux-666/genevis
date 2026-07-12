# Task 5: Superseded Hifiasm Retry-Evidence Design

**Phase**: 11
**Status**: Superseded; no retry-evidence, retention, or promotion contract remains
**Depends on**: None; Task 6B uses the Phase 10 typed-attempt lifecycle and Task 4 validation boundary directly
**Created**: 2026-07-10
**Updated**: 2026-07-12

## Status Of This Task

The former Task 5 proposed a retry-specific EvidenceCase query, retriever,
backend, shard, evaluation corpus, draft runtime case, and curator-promotion
workflow. The approved Task 6B design supersedes that approach in full. These
entities are not implemented features, dormant extension points, or future
activation gates.

This file remains only to preserve the Phase 11 task-number history and to make
the deletion boundary explicit. It does not define a runtime contract.

## Replacement Boundary

Hifiasm retry is now a same-sample, same-run attempt transition:

```text
sealed preceding attempt
  + immutable attempt 0 profile/evidence/policy snapshot
  + preceding structured execution or review failure
  + remaining owning-domain budget
  -> retry recommendation
  -> automatic validation
  -> shared parameter_approval human gate
  -> approve: freeze/append/increment/execute
     reject: terminate without append/increment/execute
```

Execution and review retry remain independently owned and budgeted. Task 6B,
not Task 5, documents their routing and terminal outcomes.

## Deleted Concepts

The Phase 11 hifiasm contract contains no:

- retry EvidenceCase or retry-specific historical retrieval space;
- retry query, database, corpus, shard, retriever, or selector backend;
- `hifiasm_retry` backend identifier;
- retry retrieval artifact or retry case shortlist;
- runtime draft `candidate_evidence_case` artifact;
- automatic or manual runtime-to-knowledge-pack promotion workflow;
- retry evaluation set or cutover permission derived from it;
- failure classifier, repairability label, or repairability gate.

Other methods' legacy retry shards are outside this task and remain unchanged.

## Acceptance Criteria

- [x] The former retry-evidence and curated-retention design is explicitly
  superseded.
- [x] No Task 5 text authorizes a retry database/retriever/backend or candidate
  promotion path.
- [x] Same-run retry ownership is delegated to Task 6B.
- [x] Legacy retry descriptors for other methods are preserved.
