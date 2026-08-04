# Parameter Recommendation

> **Status:** implemented for Assembly, AutoHiC, and T2T using schema-3 roots
>
> **Read when:** changing model-visible parameter inputs, approval behavior,
> retries, or the handoff from a recommendation to an executable attempt.
>
> **Comes from:** stage-owned method selection, canonical upstream facts,
> [data and storage](data-and-storage.md), and immutable predecessor evidence.
>
> **Feeds into:** mandatory human approval, [method execution](execution.md),
> stage-specific collection, and paths such as [AutoHiC](autohic.md).
>
> **Contract dependencies:** [parameter recommendation](../reference/parameter-recommendation.md),
> [run data](../reference/data-contract.md),
> [method execution lifecycle](../reference/execution-lifecycle.md), and
> [execution evidence](../reference/execution-evidence.md).
>
> **Code evidence:** `app/domains/parameter_recommendation/facts.py`,
> `app/domains/parameter_recommendation/resolution.py`,
> `app/schemas/parameter_recommendation.py`, `app/schemas/artifacts.py`,
> `app/llm/parameters/base.py`, `app/llm/parameters/assembly/`,
> `app/llm/parameters/hic/`, `app/llm/parameters/t2t/`,
> `app/storage/parameter_evidence.py`, and the three
> `app/stages/*/parameters/workflow.py` implementations.

## What the recommender does

A recommender starts only after a method is known. It builds a strict
model-facing request, calls the model, parses one sparse parameter mapping,
persists the evidence chain, and opens a human approval gate. It does not
select a method, route the parent graph, approve its own output, or fabricate a
fallback when the model call or response shape fails.

The implemented flow is:

```text
authoritative run facts + selected method
  -> canonical typed facts and stage context
  -> schema-3 input and basis snapshots
  -> exact persisted model messages
  -> raw response + parse record + sparse recommendation
  -> schema-3 recommendation manifest
  -> mandatory human approval
  -> schema-3 ParameterAttempt
  -> method-attempt freeze and execution
```

The manifest is the integrity root for the recommendation chain. “Schema-3
parameter recommendation” does not mean every leaf file has version `3`:
input, basis, manifest, cursor, and approved attempt are the v3 boundary, while
raw-response, parsed-recommendation, and parse-result leaves retain their own
declared versions. The strict loader verifies those mixed leaf versions and
rejects a pre-v3 root or mixed identity.

## Shared lifecycle, stage-specific biology

Assembly, AutoHiC, and T2T share canonical fact models, evidence placement,
manifest verification, approval binding, and attempt ownership. They do not
share one universal prompt DTO:

- Assembly adds its route, read type, target level, and bounded literature or
  method guidance.
- AutoHiC exposes species facts, Hi-C library context, an optional provided
  restriction enzyme, and its adjustable catalog.
- T2T exposes species facts, the resolved telomere motif, its source, and the
  long-read input count. Motif resolution is a separate evidence-producing
  step only when a supplied fact is unavailable.

All DTOs reject undeclared audit and storage fields. Session IDs, filesystem
locators, artifact IDs and hashes, credentials, and unrestricted registry
metadata stay in durable audit evidence rather than model messages.

## Sparse proposal and approval

The accepted model response is an attempt-local sparse mapping plus an
optional explanation. An empty mapping is valid. Parsing checks the response
shape, not biological quality or whether a value is a sensible catalog value.
Every shape-valid mapping goes to a human unchanged.

After approval, parameter resolution keeps three sources distinguishable:
executor-owned values, canonical provided or measured facts, and the approved
model mapping. Command construction may still reject a value that cannot be
represented safely by the selected tool. That is an execution-definition
failure; it is not permission to silently repair the model answer.

The exact DTO fields, response schema, source precedence, and validation
invariants are owned by the
[parameter-recommendation contract](../reference/parameter-recommendation.md),
not repeated here.

## Attempts, retries, and ownership

Approval creates the compact `ParameterAttempt`; no execution attempt exists
before approval. That cursor is the sole relation owner for the recommendation
root, approval decision, frozen parameters, command, frozen runner input,
execution, and later review evidence. Stage cursors select the current attempt
and must not mirror those refs in method-specific keys.

For retry-capable Assembly and AutoHiC flows, a retry first seals its
predecessor and then builds a new schema-3 recommendation chain. The prompt
receives a bounded chronological projection of earlier recommendations,
effective values, execution outcomes, and review outcomes. That history is
evidence only: the application does not score it, derive a delta, or merge
omitted keys from the preceding mapping. The implemented T2T threshold request
is initial-only; this page does not imply a T2T parameter-retry route.

There is no implemented automatic fallback recommendation, parameter-patch
semantics, silent default synthesis at the model boundary, or auto-approval.
Model failure becomes `response_invalid`; human rejection creates no execution
attempt. Any future behavior of that kind is planned work until both the
owning contract and code change.
