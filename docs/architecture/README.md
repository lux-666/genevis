# Architecture Specifications

This directory contains the accepted cross-cutting contracts for Gene Agent.
It is deliberately small: a topic belongs to one normative specification, and
other documents link to that owner instead of restating its schema or flow.

[`docs/architecture.md`](../architecture.md) is the system overview, not a
second source of detailed contracts. Code and tests show present conformance;
[`docs/handoff.md`](../handoff.md) is only a current work snapshot. Superseded
design and implementation records remain available through Git history rather
than as parallel working-tree documentation.

## Specification Registry

Design acceptance and implementation conformance are separate facts.

| Specification | Design | Current conformance | Owns |
|---|---|---|---|
| [`data-v2.md`](data-v2.md) | Accepted | Implemented | Run ownership, durable fact placement, artifact refs, checkpoint bounds and compaction, ledger/report authority, audit, and cutover |
| [`execution-evidence.md`](execution-evidence.md) | Accepted | Implemented for process results | Runtime `ExecutionResult`, durable `ExecutionEvidence`, log ownership, and result cursors |
| [`execution-lifecycle.md`](execution-lifecycle.md) | Accepted | Implemented | Shared freeze/run/collect graph, attempt owners, recovery routing, failure policies, and stage-adapter boundary |
| [`parameter-recommenders.md`](parameter-recommenders.md) | Accepted | Assembly and AutoHiC implemented | Model-boundary DTOs, exact request/response flow, parsing, approval, and recommender responsibilities |
| [`autohic-stage.md`](autohic-stage.md) | Accepted | Implemented | Parent/Hi-C boundary, AutoHiC-only execution, and restriction-enzyme recommendation |
| [`bootstrap.md`](bootstrap.md) | Accepted | Implemented | Minimal initialization of `.env` and `config/envs.yaml` |
| [`refactor/architecture-contracts.md`](refactor/architecture-contracts.md) | Accepted | Core refactor implemented; remaining simplification tracked | Resource handoff, execution authorization, method selection, CWD independence, implementation evidence, and single-track simplification rules |

## Ownership Boundaries

- Data v2 decides **where durable facts live** and what checkpoints may point to.
- Execution Evidence decides **what a durable process result contains**.
- Execution Lifecycle decides **how frozen attempts run, resume, and reach
  stage-owned collection**.
- Parameter Recommenders decides **how an already selected method receives an
  approved parameter recommendation**.
- Stage specifications decide **domain flow and biological behavior**.

A frozen attempt is created and made durable before execution. A checkpoint
does not recreate it from current configuration; it carries the immutable ref
needed to load the same runner input.

## Vocabulary Rule

In this repository, informal references to a stage “planner” may mean a
**parameter recommender**. Architecture documents must use the explicit term
`parameter recommender` when that is the intended meaning. It is distinct from:

- `StagePlan`, which records requested stages and method-selection intent;
- method selection, which chooses a method before parameter recommendation;
- parent-graph routing, which chooses the next biological stage.

A parameter recommender receives an already selected method and recommends only
that method's adjustable execution parameters. It must not choose the method,
the stage route, or the requested stage set.

## Change Protocol

1. Update the relevant specification before or with behavior code.
2. Record the specification status and date when a decision changes.
3. Do not add a stage-local storage convention when the shared contract can
   express the requirement.
4. Any exception to a normative invariant must be approved explicitly and
   documented here; a non-normative document or test fixture cannot create an
   implicit exception.
5. Keep the implementation status honest. “Accepted” means the design is
   approved, not that current code already conforms.
