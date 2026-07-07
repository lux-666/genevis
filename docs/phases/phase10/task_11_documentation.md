# Task 11: Documentation for Three-Level Parameter Recommendation

**Phase**: 10
**Status**: Completed for current implementation; follow-ups documented
**Depends on**: All implementation tasks
**Created**: 2026-07-02
**Updated**: 2026-07-07

## Goal

Document the Phase10 methodology after the MVP works. The documentation should explain the three-level permission model, full-catalog L2 recommender surface, evidence/rule/skill separation, replayable retry loop, and the no-compat cleanup that removes the previous duplicate planner registry, scattered LLM calls, and loose parameter-loop state.

## Deliverable 1: Methodology

Target file:

```text
docs/methodology/parameter_recommendation_closed_loop.md
```

Outline:

```markdown
# Evidence-Guided Parameter Recommendation for Genome Assembly

## Problem

Assembly tools expose many parameters, but published methods rarely explain how
values are selected. We need a system that can recommend parameters from sample
facts and literature evidence while remaining auditable and retryable.

## Three-Level Permission Model

| Level | LLM permission | Examples |
|---|---|---|
| L0 | Does not see, cannot modify | output prefix, threads, reads, attempt dirs, route-fixed flags |
| L1 | Sees, cannot modify | genome size, coverage, read N50, heterozygosity, hom coverage |
| L2 | Sees, may modify | purge, filtering, k-mer, overlap, graph-cleaning, ONT/Hi-C/UL internal knobs |

The level itself is the permission boundary. There are no secondary exposure
flags. The MVP catalog enumerates the full supported hifiasm/hifiasm_ont CLI
surface. If a parameter is behavior-changing and is not a fixed mechanic or
read-only fact, place it in L2 and include it in the recommender prompt. If
humans later decide a knob is unsafe or unsupported, remove it from the catalog.

## Knowledge Pack Separation

- cases.yaml contains evidence only.
- rules.yaml contains hard boundaries only.
- SKILL.md contains explainable heuristics only.

## Closed Loop

read_qc/genome_survey -> select cases -> recommend L2 params -> validate ->
freeze/run -> retry on command failure -> assembly_review -> retry on quality
failure.

## Replay

Every attempt records prompt refs, response refs, parameter sources, argv,
execution logs, review failure context, and status. Retries append new
recommendation attempts; freeze, execution, and review gates may complete the
current attempt entry with lifecycle facts.
```

## Deliverable 2: Implementation Guide

Target file:

```text
docs/implementation/parameter_recommendation_loop.md
```

Must cover:

- catalog schema,
- descriptor-derived planner method metadata,
- adding a full method catalog from the supported tool CLI surface,
- adding evidence cases,
- adding hard rules,
- prompt payload shape,
- the shared assembly decision/advisor boundary,
- validator behavior,
- command builder behavior,
- execution failure retry,
- assembly_review failure retry,
- how to replay an attempt from recorded refs,
- why main-flow refs use session/attempt paths with hashes as integrity fields,
- which old APIs and compatibility paths were removed.

## Deliverable 3: User-Facing Notes

Target file:

```text
docs/user_guide/parameter_recommendation.md
```

Must explain:

- what the recommender uses as evidence,
- why it may leave parameters unset,
- how retries work,
- where to find attempt records and logs,
- how a user can manually remove an L2 parameter from consideration by editing the catalog.
- why the MVP exposes every cataloged L2 parameter instead of maintaining a hidden prompt subset.
- why a run's parameter recommendation artifacts live under
  `data/artifacts/assembly/<session>/`.

## Required Language

Use the final three-level model only:

- L0: LLM does not see or modify.
- L1: LLM sees but does not modify.
- L2: LLM sees and may modify.

Do not mention or reintroduce older permission models or secondary exposure switches.

## Cleanup Notes To Include

Document the final architecture, not the migration path:

- `descriptors/registry.yaml` plus method descriptors are the method source of
  truth; the interactive planner derives from them.
- Assembly LLM decisions go through the shared decision/advisor boundary.
- Parameter loop state is represented by typed compact contracts and
  `parameter_attempts`; legacy ad hoc keys are not a parallel API.
- Main-flow artifacts use session/attempt-scoped paths; hashes are recorded for
  integrity, not as the primary runtime ref.
- Historical compatibility shims removed during Phase10 should not be described
  as supported extension points.

## Acceptance Criteria

- [x] Methodology doc written: `docs/methodology/parameter_recommendation_closed_loop.md`.
- [x] Implementation guide written: `docs/implementation/parameter_recommendation_loop.md`.
- [x] User guide written: `docs/user_guide/parameter_recommendation.md`.
- [x] Docs include the attempt replay contract: `docs/phases/phase10/replay_contract.md`.
- [x] Docs match implementation behavior, including descriptor-derived planner metadata, runtime help verification, and standalone command/review artifacts.
- [x] Docs describe the cleanup target without preserving old compatibility paths as supported APIs: `docs/phases/phase10/no_compat_cleanup_note.md`.
