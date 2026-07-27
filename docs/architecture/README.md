# Architecture Specifications

This directory contains the current cross-cutting contracts. Each topic has one
owner; other documents link to it instead of restating its schema or flow.
[`../architecture.md`](../architecture.md) is the system overview. Code and
tests provide implementation evidence, while Git history retains superseded
design and implementation records.

## Specification registry

| Specification | Owns |
|---|---|
| [`resource-handoff.md`](resource-handoff.md) | Cross-stage logical resources, resolver priority, central `resource_refs`, and FOFN boundary |
| [`data-v2.md`](data-v2.md) | Run ownership, durable fact placement, artifact refs, checkpoint bounds and compaction, ledger/report authority, storage audit, and retention/GC |
| [`execution-evidence.md`](execution-evidence.md) | Runtime `ExecutionResult`, durable `ExecutionEvidence`, process logs, and result cursors |
| [`execution-lifecycle.md`](execution-lifecycle.md) | Shared freeze/run/collect graph, attempt owners, recovery routing, failure policies, and stage-adapter boundary |
| [`parameter-recommenders.md`](parameter-recommenders.md) | Model-boundary DTOs, exact request/response flow, approval, persistence, and recommender responsibilities |
| [`autohic-stage.md`](autohic-stage.md) | Parent/Hi-C boundary, AutoHiC-only execution, sparse multi-attempt parameter recommendation, retry, and result adoption |
| [`bootstrap.md`](bootstrap.md) | Minimal initialization of `.env`, `config/envs.yaml`, and configured external-package conformance |

## Ownership boundaries

- Resource Handoff decides how materialized inputs move between stages.
- Data v2 decides where durable facts live and what checkpoints may reference.
- Execution Evidence defines the durable process-result body.
- Execution Lifecycle defines how frozen attempts run, resume, and reach
  stage-owned collection.
- Parameter Recommenders defines how a selected method receives a reviewed
  parameter recommendation.
- Stage Contracts defines biological inputs, outputs, planning behavior, and
  review triggers.
- Descriptor Spec defines registered package and method capability metadata.

No stage specification may create a second storage, execution, resource, or
parameter-recommendation ownership model.

## Vocabulary

- **method selection** chooses a registered executable method;
- **parameter recommendation** proposes values for an already selected method;
- **parameter approval** is the human decision on that exact proposal;
- **planning** creates bounded workflow or stage intent and does not authorize a
  process;
- **execution** consumes a persisted frozen attempt;
- **report** is a regenerable operator projection and never controls workflow.

## Change rule

1. Update the owning specification and implementation in the same change.
2. Verify current code and tests against the changed invariant.
3. Do not add compatibility branches for deleted development-only contracts.
4. Register any new cross-cutting specification here.
5. A test fixture or non-normative guide cannot create an architecture
   exception.
