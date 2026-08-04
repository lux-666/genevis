# Review and Resume

> **Status:** implemented.  
> **Read when:** changing a human gate, decision persistence, approval binding,
> resume validation, or post-review routing.  
> **Comes from:** a stage-owned immutable review request and compact blocked
> checkpoint.  
> **Feeds into:** [runtime orchestration](runtime-orchestration.md), a new
> parameter attempt, resource adoption, or terminal failure.  
> **Contract dependencies:** [run data](../reference/data-contract.md),
> [execution lifecycle](../reference/execution-lifecycle.md), and
> [stage contracts](../reference/stage-contracts.md).  
> **Code evidence:** `app/review/gates.py`, `app/review/persistence.py`,
> `app/review/approval.py`, `app/control/resume_transaction.py`,
> `app/control/lifecycle.py`, and `app/review/annotation_intake.py`.

## Durable gate model

A human gate has three different records:

1. the stage publishes an immutable review-request artifact;
2. the checkpoint stores only a bounded interrupt cursor with request ref,
   request hash, stage, gate, and attempt bindings;
3. the operator decision is published once beneath that request identity and
   binds the request hash.

The prompt shown to a person is a projection of the request, not the recovery
authority. A checkpoint must not embed the full request or decision body, and a
Ledger event must not replace either artifact.

Gate definitions centralize allowed decisions, prompt construction, policy,
and resume application. The external resume DTO is one strict
`ResumeReviewRequest` with a string decision field; parameter approval,
assembly adoption, Hi-C adoption, T2T adoption, and annotation intake remain
different gate-scoped decision vocabularies because each registered gate and
stage applier validates its own allowed values and bindings.

## Resume validation

Before state changes, resume reloads the current checkpoint and verifies the
session, stage, gate, request ID and hash, decision vocabulary, manifest
identity, and any recommendation/attempt bindings. Missing, stale,
cross-session, conflicting, or already-consumed input fails closed. Republishing
the identical immutable bytes is idempotent, but it does not authorize applying
an already-consumed gate a second time.

The resume flow is:

```text
record resume start
  -> read and validate blocked checkpoint
  -> validate the structured decision against the immutable request
  -> gate applier publishes the canonical decision artifact
  -> return the gate-specific compact state update
  -> graph.update_state(...)
  -> graph.invoke(None, ...)
  -> finalize a terminal outcome when reached
  -> record resume completion or failure
```

This is an idempotence-aware saga across artifacts, Ledger, and SQLite
checkpoint operations. It is not an ACID transaction spanning those stores.
Each durable step is therefore replay-aware and validates the evidence left by
earlier steps.

## Decision effects

A decision may approve parameters, accept or reject a biological candidate,
keep a prior resource, authorize a bounded retry, stop, or continue into
annotation intake. It must not silently create an unregistered method or bypass
the frozen-attempt boundary.

Assembly Review is a deliberate plan-mutation boundary. When the operator
continues downstream, annotation intake can persist a typed plan and update the
remaining `requested_stages`. That is an auditable post-freeze decision, not a
claim that the original run config was edited.

Terminal completion or failure may trigger report generation and checkpoint
compaction. Immutable review requests, decisions, execution evidence, and
adopted biological artifacts remain retained independently of checkpoint
history.
