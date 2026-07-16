# Architecture Specifications

This directory contains accepted, cross-cutting architecture specifications for
Gene Agent. These documents define contracts that multiple stages must share;
they are not implementation retrospectives or phase history.

The system overview remains in [`docs/architecture.md`](../architecture.md).
When a topic has a dedicated specification here, that specification is the
normative source for that topic. Phase documents describe how an earlier change
was implemented and must not override an accepted architecture specification.

## Accepted Specifications

| Specification | Status | Scope |
|---|---|---|
| [`data-v2.md`](data-v2.md) | Accepted; core implemented, operational cleanup pending | Run-owned data layout, fact ownership, artifact references, parameter-recommendation records, attempts, review records, checkpoints, ledger events, reports, retention, and cutover |
| [`autohic-stage.md`](autohic-stage.md) | Accepted; core implemented, conformance work pending | Parent/Hi-C graph boundary, AutoHiC-only execution, and the restriction-enzyme parameter recommender |
| [`bootstrap.md`](bootstrap.md) | Accepted; implemented | Minimal initialization of `.env` and `config/envs.yaml` |

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
   documented here; a phase document or test fixture cannot create an implicit
   exception.
5. Keep the implementation status honest. “Accepted” means the design is
   approved, not that current code already conforms.
