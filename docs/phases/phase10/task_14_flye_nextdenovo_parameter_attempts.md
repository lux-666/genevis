# Task 14: Flye and NextDenovo Typed Parameter Attempts

**Phase**: 10
**Status**: Completed
**Depends on**: Phase10 Tasks 1-13 typed parameter-attempt contract
**Created**: 2026-07-07
**Updated**: 2026-07-10

## Goal

Move Flye and NextDenovo onto the same catalog-driven parameter-attempt
contract already used by the Phase10 hifiasm/hifiasm_ont implementation.

This removes the last assembly-method split where hifiasm methods used typed
`parameter_attempts`, attempt-scoped command artifacts, and retry-gate routing,
while Flye and NextDenovo still relied on method-local parameter handling.

## Implemented Behavior

### Method Catalogs

New catalogs:

```text
descriptors/knowledge_packs/assembly/methods/flye/params.yaml
descriptors/knowledge_packs/assembly/methods/nextdenovo/params.yaml
```

Descriptor wiring:

- `descriptors/methods/assembly/flye_candidate.yaml` declares
  `parameter_catalog: flye`.
- `descriptors/methods/assembly/nextdenovo_candidate.yaml` declares
  `parameter_catalog: nextdenovo`.

The catalogs keep executor/run-configuration mechanics in L0, read-only sample
facts in L1, and recommender-controlled behavior knobs in L2.

### Flye Freeze Path

`freeze_flye_attempt` now:

- loads the Flye parameter catalog,
- reads the current typed parameter attempt,
- builds catalog-driven `parameter_sources`,
- builds deterministic Flye argv through the shared command builder,
- writes attempt-scoped `command.json`,
- stores `parameter_sources`, `argv`, `command_ref`, and `parameter_attempt_id`
  on the frozen attempt metadata,
- updates the current typed parameter attempt to `frozen`.

### NextDenovo Freeze Path

`freeze_nextdenovo_attempt` now:

- loads the NextDenovo parameter catalog,
- reads the current typed parameter attempt,
- builds catalog-driven `parameter_sources`,
- merges L0/L1/L2 catalog values into the generated `run.cfg`,
- writes attempt-scoped `command.json`,
- stores `parameter_sources`, `rendered_parameters`, `argv`, `command_ref`, and `parameter_attempt_id`
  on the frozen attempt metadata,
- updates the current typed parameter attempt to `frozen`.

### Shared Lifecycle And Routing

`run_method_attempt` now always updates the current typed parameter attempt with
execution success or failure. The old Phase10 skip path for Flye/NextDenovo was
removed because these methods now have `parameter_attempts`.

After candidate selection, all executable assembly methods now enter
`recommend_assembly_parameters` before freeze/run. This includes Flye,
NextDenovo, hifiasm, and hifiasm_ont.

Assembly routing now sends method execution failures to `retry_gate`
consistently. A method failure no longer bypasses the parameter retry path just
because the method is Flye or NextDenovo.

### LLM Availability And Defaults

The production contract is strict: a local Flye/NextDenovo run still needs a
valid typed parameter recommendation attempt before method freeze, even when
the run config already provides the L0 execution inputs such as read type,
genome size, and threads. If the model or assembly decision service is not
available, `recommend_assembly_parameters` records a validation-failed attempt
and stops before freeze/run.

`parameter_recommendation.allow_model_unavailable_defaults: true` is an
explicit operator/test escape hatch. When set, the recommender may emit an empty
L2 recommendation and let catalog defaults render the method command or
`run.cfg`. Tests use this flag to exercise local execution without requiring a
live model.

This behavior matches hifiasm/hifiasm_ont. There is no silent method-local
fallback for Flye or NextDenovo after this Phase10 expansion.

When a run stops at assembly because the model is unavailable, the intended
operator flow is to fix the LLM configuration and resume from the assembly
checkpoint rather than rerunning earlier stages. The TUI should expose that
resume/retry action later; this task only records the contract and keeps the
graph state resumable.

## Artifacts

Each Flye/NextDenovo parameter attempt records compact replay state in
`stage_outputs["assembly"]["parameter_attempts"]` and writes a standalone
command artifact:

```text
data/artifacts/assembly/<session>/parameter_attempt_<n>/command.json
```

The command artifact records:

- `attempt_id`
- `method_id`
- `argv`
- shell command string
- script path
- working/output paths
- draft assembly path
- catalog-derived `parameter_sources`
- resolved `rendered_parameters` for generated config files

## Tests

Updated coverage:

- Flye successful local execution records a typed parameter attempt with
  `method_id`, `status: executed`, and `command_ref`.
- Flye execution failure records `status: execution_failed`, preserves the
  failure log tail, and fails through the retry gate when retries are exhausted.
- NextDenovo successful local execution records a typed parameter attempt with
  `method_id`, `status: executed`, and `command_ref`.
- All executable assembly methods must load shared typed-attempt-compatible
  parameter catalogs with at least one L0, one L1, and one L2 parameter:
  - `assembly.candidate.flye`
  - `assembly.candidate.nextdenovo`
  - `assembly.candidate.hifiasm`
  - `assembly.candidate.hifiasm_ont`

## Acceptance Criteria

- [x] Flye declares and loads a parameter catalog.
- [x] NextDenovo declares and loads a parameter catalog.
- [x] Flye freeze uses catalog-derived parameter sources and writes a command
  artifact.
- [x] NextDenovo freeze uses catalog-derived parameter sources and writes a
  command artifact.
- [x] Flye and NextDenovo update typed `parameter_attempts` during freeze and
  execution.
- [x] Flye and NextDenovo enter the same parameter recommendation path as
  hifiasm/hifiasm_ont before freeze/run.
- [x] Method execution failures route through the parameter retry gate.
- [x] Regression tests cover catalog availability and typed attempt lifecycle
  records for Flye and NextDenovo.

## Follow-Ups

- Add method-specific evidence cases and hard-rule shards for Flye and
  NextDenovo if their L2 recommender surface grows beyond the initial safe
  knobs.
- Exercise a full multi-attempt retry scenario for Flye or NextDenovo once the
  recommender has method-specific evidence beyond default fallback behavior.
- Add a TUI action that surfaces assembly-stage model-unavailable failures and
  resumes/retries the same session after the operator fixes LLM configuration.
