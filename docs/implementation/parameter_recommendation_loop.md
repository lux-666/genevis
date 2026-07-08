# Assembly Parameter Recommendation Loop Implementation Guide

## Implemented Surface

Phase 10 is currently integrated for the hifiasm and hifiasm_ont parameter loop. The implementation writes attempt-scoped profile, prompt, LLM response, and validation artifacts; appends typed `parameter_attempts`; validates L2-only recommendations; builds deterministic argv from L0/L1/L2 source groups; and can re-enter recommendation after execution or review-quality failure.

Planner metadata is derived from descriptors through `app/entrypoints/planner_catalog.py`; runtime hifiasm help verification is covered by a skip-safe test; and command/review-failure context is written as standalone attempt-scoped artifacts.

## Catalog Schema

Method catalogs are loaded through `app/stages/assembly/parameter_catalog.py` and modeled by `app/schemas/parameter_catalog.py`. A catalog parameter has one permission level:

- L0: never shown to or modified by the LLM. Values come from `fixed_value` or `source_priority`.
- L1: shown as read-only facts. Values come from `source_priority`.
- L2: shown as adjustable parameters and accepted from model output after validation.

Catalogs for hifiasm and hifiasm_ont live under:

```text
descriptors/knowledge_packs/assembly/methods/<method>/params.yaml
```

Add a method catalog by enumerating the project-supported tool CLI surface, assigning every parameter to L0/L1/L2, setting coarse type/range/enums, and adding `source_priority` paths for derived L0/L1 values. Do not add a hidden prompt allowlist; removing a risky L2 knob from consideration means removing it from the catalog.

Runtime verification of these catalogs against the project-supported hifiasm help output is covered by `tests/test_phase10_parameter_catalog.py`. Set `HIFIASM_BIN` or put `hifiasm` on `PATH` to run the binary-backed check; without a binary, the test skips.

## Descriptor and Planner Metadata

Method descriptors already point to parameter catalogs, and assembly runtime catalog loading resolves through those descriptors. The intended no-compat architecture is for the interactive planner to derive method metadata from descriptors/catalogs as the same source of truth.

Current status: planner no-compat cleanup is implemented. Planner-facing required parameters, defaults, selection roles, display names, and planner-only methods live in method descriptors under `planner:`. `ask_planner.py` and `ask_interactive.py` consume the descriptor-derived catalog and no longer maintain `_STAGE_METHODS` as a second method registry.

## Knowledge Pack

The recommender pack is:

```text
descriptors/knowledge_packs/assembly/parameter_recommender/
```

Add evidence cases in `cases/*.yaml` and register them in `cases.yaml`. Cases should contain observations only, not rules or few-shot target answers.

Add hard validation rules in `rules/*.yaml` and register them in `rules.yaml`. Rules block unsafe or impossible values when their conditions match profile facts.

Keep `SKILL.md` to heuristics and output discipline: use evidence, be conservative, return JSON, put only L2 keys under `params`, and omit uncertain L2 parameters.

## Prompt Payload Shape

`app/stages/assembly/parameter_profile.py` builds the complete profile with `sample`, `assets`, `read_qc`, `genome_survey`, `previous_attempt`, and `assembly_review` sections.

`app/stages/assembly/prompt_payload.py` then writes:

- `input_profile.json`: the full replay/validation profile;
- `prompt_payload.json`: task-scoped prompt profile, L1 readonly params, L2 adjustable params, selected evidence cases, hard rules, and skill instructions.

Prompt tasks are `recommend_initial`, `retry_after_execution_failure`, and `retry_after_review_failure`. Initial prompts include sample/assets/read_qc/genome_survey; execution retries include previous command, params, exit code, failure class, and log tails; review retries include previous params/command/cases and review metrics.

## Decision and Validation Boundary

`recommend_assembly_parameters()` routes parameter recommendation through `make_assembly_decision()` with `kind="parameter_recommendation"`. Route selection and parameter recommendation therefore share the assembly decision/advisor boundary rather than calling a model directly from stage-local code.

`validate_recommended_parameters()` accepts only a JSON object whose keys are cataloged L2 parameters. It rejects unknown keys, L0/L1 keys, bad types, out-of-range values, enum violations, and matching hard-rule violations. Invalid recommendations set `parameter_validation_failed` and stop before freeze/run.

## Command Building

`build_parameter_sources()` derives:

- `l0_fixed` from catalog `fixed_value` and `source_priority`;
- `l1_readonly` from catalog `source_priority`;
- `l2_recommended` from validated model output.

`build_command()` validates that each source group contains only the matching level, emits arguments in catalog order, and appends positional values last. Omitted L2 parameters are not emitted, so the tool default applies unless the method contract supplies a value elsewhere.

Freeze paths for catalog-driven assembly methods store `parameter_sources` and
the rendered execution representation on the current `parameter_attempts` entry
and use `parameter_attempt_<N>` work/artifact directories. hifiasm,
hifiasm_ont, and Flye store `argv`; NextDenovo stores `rendered_parameters` for
the generated `run.cfg` and records `argv` for the `nextDenovo <run.cfg>`
invocation.

They also write `command.json` under the same attempt directory and store
`command_ref` on the current attempt.

## Model Availability

After candidate selection, every executable assembly method with a parameter
catalog enters `recommend_assembly_parameters` before freeze/run. This is the
same contract for Flye, NextDenovo, hifiasm, and hifiasm_ont.

Model unavailability is treated as a recommendation failure by default. The
stage records a validation-failed `ParameterAttempt` and stops before local
execution. `parameter_recommendation.allow_model_unavailable_defaults: true` is
an explicit test/operator fallback that lets the model boundary return an empty
L2 recommendation so catalog defaults can render the command.

The intended production recovery is to fix LLM configuration and resume/retry
from the assembly checkpoint. TUI support for that operator flow is a follow-up;
the graph contract already avoids rerunning earlier stages unnecessarily.

## Retry Gates

`execution_retry_gate()` runs after a failed assembly status with a recorded parameter attempt. It reads the previous attempt execution result, stores the last log tails when available, increments `current_parameter_attempt_id`, and sets `retry_parameters_after_execution_failure` until the configured execution retry cap is reached.

`parameter_quality_gate()` classifies assembly_review metrics. Passing metrics set `parameter_quality_passed`; failing metrics set `retry_parameters_after_review_failure`, increment the attempt id, store review diagnosis, and honor the configured review retry cap.

Failing review metrics also write `review_failure.json` under the reviewed attempt directory and store `review_failure_ref` on that attempt.

## Replay Contract

Replay starts from the compact `parameter_attempts` entry:

- open `input_profile_ref.path_ref` and verify `sha256`;
- open `prompt_payload_ref.path_ref` and verify `sha256`;
- inspect `llm_response_ref`, `validation_ref`, `selected_case_ids`, `recommended_params`, `omitted_parameters`, `parameter_sources`, and `argv`;
- for retries, follow `previous_attempt_id`, `failure_log_tail`, `review_failure`, `command_ref`, and `review_failure_ref`.

Command metadata and review failure context are recorded both in compact attempt state and in standalone attempt-scoped artifact files.

## Cleanup Contract

Supported extension points are descriptors, method catalogs, knowledge-pack cases/rules, `SKILL.md`, prompt/profile builders, validator rules, command-builder source groups, and typed `parameter_attempts`.

Do not treat legacy ad hoc parameter-loop keys, stage-local model calls, duplicate planner registries, or hash-only runtime refs as supported APIs. Planner registry removal and its focused regression tests are complete; keep future extensions on descriptors/catalogs rather than adding planner constants back.
