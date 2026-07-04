# Phase 10: Evidence-Guided Parameter Recommendation Closed Loop

**Phase**: 10
**Status**: Planning
**Created**: 2026-07-02
**Updated**: 2026-07-03

## Goal

Build the smallest executable parameter recommendation loop for assembly methods:

1. profile the sample from read_qc and genome_survey,
2. select a few evidence cases,
3. ask the LLM to fill any cataloged parameter it is allowed to change,
4. validate the result against hard boundaries,
5. freeze and run a replayable attempt,
6. on command failure, retry with the previous prompt inputs plus the last 100 log lines,
7. on assembly_review failure, retry with the review metrics and failure diagnosis.

The first target is hifiasm/hifiasm_ont. Exhaustive search over parameter combinations is out of scope, but an intentionally partial parameter catalog is also out of scope. The MVP catalog should enumerate the supported hifiasm CLI surface from the pinned/project-supported hifiasm version, classify every parameter as L0/L1/L2, expose every L2 parameter to the recommender, and rely on validation plus rules for safety. The MVP is a deterministic, auditable, replayable attempt loop over the full cataloged parameter surface.

## Current Project Shape

Already useful:

- `app/stages/assembly/builder.py` has a LangGraph assembly subgraph.
- Each executable assembly method already follows `freeze -> run -> collect`.
- `run_frozen_attempt()` already records stdout/stderr paths and ledger attempt events.
- `read_qc` can provide read summary facts such as total bases, N50, and quality fields.
- `genome_survey` can provide `genome_size_prior`, heterozygosity, and repeat fraction.
- `assembly_review` already evaluates candidates through QUAST/BUSCO-style signals and has rerun infrastructure.
- `descriptors/knowledge_packs/assembly/*` already uses the three-file knowledge-pack pattern: `cases.yaml`, `rules.yaml`, `SKILL.md`.

Missing:

- A simple parameter catalog with only three LLM permission levels.
- A full parameter input profile artifact separated from task-scoped prompt profiles.
- A recommender prompt contract that uses selected cases, rules, skill text, read_qc, and genome_survey.
- A replayable `parameter_attempts` ledger entry.
- Command construction from accepted parameter sources.
- Retry routing for command failure and assembly_review failure.

## Existing/New Architecture

Legend:

- `(EXISTS)`: already present and should be reused.
- `(NEW)`: new component or artifact.
- `(REFINE)`: existing component that needs a focused contract extension.
- `(REFACTOR)`: existing behavior that should move behind a cleaner interface.

```text
read_qc (EXISTS) ───────────────┐
                                ├─> input_profile_builder / infer_input_profile (REFINE)
genome_survey (EXISTS) ─────────┘        |
                                         |  L1 readonly facts:
                                         |  genome size, coverage, read N50,
                                         |  quality, heterozygosity, hom coverage
                                         v
                                      route_selector (EXISTS)
                                          |  fixes L0 route mechanics: hifi / ont / cyclone, --ont, route inputs
                                          v
                                      parameter_catalog_loader (NEW) <── method_param_catalog params.yaml (NEW)
                                          |                              describes L0 / L1 / L2 for hifiasm, hifiasm_ont
                                          v
                                      case_selector (NEW) <───────────── cases.yaml (NEW)
                                          |  selects 3-8 similar cases
                                          v
                                      parameter_recommender (NEW) <───── SKILL.md (NEW)
                                          |  prompt = L1 readonly facts + L2 modifiable params + selected cases, SKILL
                                          |  retry prompt also includes previous attempt plus failure context
                                          v
                                      rule_validator (NEW) <──────────── rules.yaml (NEW; hard boundaries only)
                                          |  validates JSON, catalog bounds, L2-only output, and biological boundaries
                                          v
                                      command_builder (REFACTOR)
                                          |  merges l0_fixed + l1_readonly + validated l2_recommended into argv
                                          v
                                      executor / method lifecycle (EXISTS)
                                          |  freeze -> run -> collect; stdout/stderr already recorded
                                          |  FAIL: last 100 log lines
                                          |        └──────────────> parameter_recommender retry (NEW)
                                          v
                                      assembly_review (EXISTS)
                                          |  QUAST/BUSCO-style metrics and candidate review
                                          v
                                      parameter_quality_gate (NEW)
                                          |  PASS: continue existing review flow
                                          |  FAIL: review metrics + failure mode
                                          |        └──────────────> parameter_recommender retry (NEW)
                                          v
                                      attempt_ledger / parameter_attempts (NEW)
                                          append-only replay record for every recommend/freeze/run/review attempt
```

## Three-Level Parameter Permission Model

No fourth level. No per-field exposure switches. The level itself is the permission boundary.

| Level | Permission | Meaning | Examples |
|---|---|---|---|
| L0 | LLM does not see and cannot modify | Executor, route, and reproducibility mechanics | `-o`, `-t`, input reads, output dirs, attempt id, `--ont`, Hi-C/trio/UL route inputs, `-h`, `--version` |
| L1 | LLM can see but cannot modify | Factual values derived from read_qc, genome_survey, manifest, or run_config | `--hg-size`, `--hom-cov`, `--n-hap`, coverage, read N50, read quality, heterozygosity, telomere motif when known |
| L2 | LLM can see and may modify | All behavior-changing assembly parameters not treated as fixed facts or executor mechanics | `-l`, `-s`, `--purge-max`, `--rl-cut`, `--sc-cut`, `-k`, `-w`, `-D`, `-N`, graph cleaning thresholds, Hi-C/UL internal thresholds |

Operational rule: enumerate the full supported hifiasm CLI surface in the method catalog. If a parameter is not L0 or L1, put it in L2 and include it in the recommender prompt. Humans may later remove unsafe or unsupported knobs by deleting them from the catalog, but the MVP should not start from a hand-picked subset and should not add secondary permission switches.

## Knowledge Pack Boundary

`descriptors/knowledge_packs/assembly/parameter_recommender/` must follow strict separation:

- `cases.yaml`: evidence only. Each case records sample facts, method, parameters used, quality outcome, source, and a short note. It does not encode rules.
- `rules.yaml`: hard boundaries only. It constrains valid values or combinations. It does not contain soft recommendation heuristics.
- `SKILL.md`: explainable heuristics only. It explains how to reason from evidence, how to interpret failure modes, and how to be conservative under uncertainty. It does not invent hard constraints.

## MVP LangGraph Shape

The architecture diagram above is the canonical Phase10 graph. In implementation terms, insert `recommend_parameters` and `rule_validator` after primary method selection and before method attempt freezing. Add one retry gate after method execution failure and one quality gate after assembly_review metrics. Both retry paths re-enter `parameter_recommender` with `attempt_id + 1` and append a new replay record.

## Attempt Replay Contract

Every recommendation/run/review attempt appends a compact immutable entry:

```yaml
attempt_id: 0
method_id: assembly.candidate.hifiasm_ont
status: recommended | validation_failed | frozen | execution_failed | executed | review_failed | passed
input_profile_ref: data/artifacts/assembly/<session>/input_profile.json
read_qc_refs: []
genome_survey_ref: data/artifacts/genome_survey/<session>/result.json
catalog_ref: descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml
knowledge_pack_ref: descriptors/knowledge_packs/assembly/parameter_recommender
selected_case_ids: []
prompt_payload_ref: data/artifacts/assembly/<session>/parameter_attempt_0_prompt.json
llm_response_ref: data/artifacts/assembly/<session>/parameter_attempt_0_response.json
parameter_sources:
  l0_fixed: {}
  l1_readonly: {}
  l2_recommended: {}
validation:
  valid: true
argv: ["hifiasm", "--ont", "-o", "...", "-t", "32", "--hg-size", "1g", "-l", "1", "reads.fq"]
frozen_attempt_ref: {}
execution_result:
  return_code: 1
  stdout_path: data/logs/...
  stderr_path: data/logs/...
failure_log_tail: "last 100 lines"
quality_gate:
  passed: false
  failure_mode: under_purged
```

Replay rule: a retry never mutates a previous entry. It reads previous attempt refs and writes `attempt_id + 1`.

## Full Profile vs Prompt Profile

Phase10 maintains two related contracts:

- `input_profile_ref`: a complete replay/audit artifact with `sample`, `assets`, `read_qc`, `genome_survey`, `previous_attempt`, and `assembly_review` sections.
- prompt profile: a task-scoped slice of the full profile. The LLM does not receive every field by default.

Prompt slicing:

- Initial recommendation receives sample context plus read_qc, genome_survey, and selected cases.
- Execution repair receives previous command/params, exit code, failure class, and log tails.
- Quality replan receives previous params/command/cases plus BUSCO/QUAST and assembly size ratio signals.

The rule validator may use the complete full profile for hard constraints, including fields not shown to the LLM.

## First Executable Surface

The first closed loop should cover:

- hifiasm_ont route with fixed `--ont` as L0.
- L1 facts: genome size, coverage, read N50, quality summaries, heterozygosity when available.
- L2 recommendation: every cataloged tunable hifiasm_ont parameter from the supported hifiasm CLI surface, including `-l`, `-s`, `--rl-cut`, `--sc-cut`, overlap, graph-cleaning, purge, and lower-level knobs.
- Rules: value ranges, impossible combinations, and safety boundaries.
- Retry: command failure with last 100 log lines.
- Review retry: under-purged, over-purged, fragmented, low-completeness failure modes.

## Task Order

1. Define the three-level parameter catalog schema.
2. Create full hifiasm and hifiasm_ont catalogs using only `level`, `cli`, `value_type`, defaults, allowed/bounds, and source hints.
3. Create the `parameter_recommender` knowledge pack.
4. Add seed evidence cases.
5. Implement rule validation.
6. Refactor command building around L0/L1/L2 sources.
7. Implement initial/retry parameter recommender node.
8. Implement execution failure and assembly_review failure retry gates.
9. Add integration tests for a replayable pass/fail/retry loop.
10. Document the methodology.

## Acceptance Criteria

- No Phase10 document uses any permission model beyond L0/L1/L2.
- The MVP docs consistently require a full supported hifiasm/hifiasm_ont parameter catalog, not a small L2 subset.
- The graph can produce a full attempt entry before running a command.
- The graph records a complete profile artifact while prompt payloads remain task-scoped.
- The executor does not run when recommendation JSON is invalid or rules fail.
- A command failure can be retried with previous inputs plus last 100 log lines.
- A review failure can be retried with metrics and failure mode.
- Every attempt is replayable from recorded refs and argv.
