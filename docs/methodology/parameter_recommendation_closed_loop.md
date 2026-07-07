# Evidence-Guided Parameter Recommendation for Genome Assembly

## Problem

Assembly tools expose many behavior-changing parameters, but published methods rarely explain why a value was selected for a particular sample. Phase 10 adds a small closed loop that recommends assembly parameters from sample facts and evidence while keeping every attempt auditable, bounded, and replayable.

The first supported surface is hifiasm and hifiasm_ont. The loop is not an exhaustive optimizer. It recommends only justified overrides, lets method defaults stand when evidence is weak, and validates every model output before command freezing.

## Three-Level Permission Model

The level is the permission boundary. There are no secondary exposure switches.

| Level | LLM permission | Examples |
|---|---|---|
| L0 | LLM does not see or modify | output prefix, threads, input reads, attempt directories, route-fixed flags such as `--ont` |
| L1 | LLM sees but does not modify | genome size, coverage, read N50, read quality, heterozygosity, hom coverage |
| L2 | LLM sees and may modify | purge, filtering, k-mer, overlap, graph-cleaning, ONT/Hi-C/UL internal knobs |

The MVP catalog enumerates the supported hifiasm/hifiasm_ont CLI surface. If a parameter is behavior-changing and is not executor mechanics, route mechanics, or a read-only fact, place it in L2 and include it in the recommender prompt. If humans later decide a knob is unsafe or unsupported, remove it from the method catalog rather than hiding it with a second allowlist.

## Knowledge Pack Separation

The parameter recommender knowledge pack separates evidence, rules, and heuristics:

- `cases.yaml` indexes case shards. Cases are observations: sample facts, method, parameters used, quality outcome, source, and notes.
- `rules.yaml` indexes rule shards. Rules are hard validation boundaries for unsafe, impossible, or explicitly disallowed values.
- `SKILL.md` contains explainable heuristics and output discipline. It does not replace hard validation.

This separation keeps literature evidence from becoming hidden policy and keeps rules testable outside the LLM.

## Closed Loop

The implemented loop is:

```text
read_qc/genome_survey
  -> build full profile
  -> select evidence cases
  -> build task-scoped prompt payload
  -> recommend cataloged L2 params through the shared assembly decision boundary
  -> validate L2-only output, catalog constraints, and hard rules
  -> build deterministic argv from L0/L1/L2 sources
  -> freeze/run
  -> retry on command failure with previous attempt plus log tails
  -> assembly_review
  -> retry on quality failure with review metrics and diagnosis
```

Retries append a new attempt id. Old attempt records are not rewritten as the source of truth for the new recommendation.

## Replay

Every attempt records compact typed state in `parameter_attempts`, including profile, prompt, response, validation refs, selected case ids, recommended and omitted parameters, parameter sources, argv, validation status, and optional execution or review failure context.

Main-flow artifacts use session/attempt paths such as:

```text
data/artifacts/assembly/<session>/parameter_attempt_<N>/input_profile.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/prompt_payload.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/llm_response.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/validation.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/command.json
data/artifacts/assembly/<session>/parameter_attempt_<N>/review_failure.json
```

Each artifact ref carries both `path_ref` and `sha256`. The path is the runtime replay ref; the hash is an integrity field. Hash-only refs are not the main runtime contract for the running graph.

## Current Boundaries

The core hifiasm/hifiasm_ont recommendation, validation, attempt recording, execution retry, review-quality retry, shared decision boundary, and session/attempt artifact paths are integrated.

The following cleanup items are complete:

- runtime help verification of hifiasm and hifiasm_ont catalogs against the local project-supported hifiasm binary;
- planner no-compat cleanup that removes the duplicate planner method registry and derives planner metadata from descriptors/catalogs;
- architecture-regression tests for the removed planner registry and stage-local assembly model calls;
- standalone `command.json` and `review_failure.json` attempt artifacts in addition to compact `parameter_attempts`.

Post-Phase10 follow-up is narrower: broaden closed-loop regression coverage
where useful, keep catalog verification current when the pinned/project-supported
hifiasm binary changes, and consider Flye/NextDenovo migration as a next-phase
expansion rather than a Phase 10 blocker.
