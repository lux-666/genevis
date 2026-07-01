# 10 Ask Staged Clarification

Task status: done.

## Goal

Make `gene-agent ask` a true staged operator planning entry: instead of
silently falling back to planned-only execution, ask should collect stage
selection, method choices, and required parameters through explicit
clarification steps, then confirm the execution plan before starting.

## Scope

- First version covers the assembly/Flye end-to-end path only.
- Stages without real method_decision consumption (genome_survey,
  gene_annotation, function_annotation) are not exposed for method selection.
- Final confirm step required before any graph invocation.
- `--run-config` remains a direct bypass for operators who provide their own
  configuration.

## Non-Goals

- Do not expand RunConfig schema or routing logic in this task.
- Do not build interactive TTY prompts; use file-mediated JSON exchanges.
- Do not expose method selection for stages that do not consume it.

## Tasks

- [x] Define `AskPlanState`, `AskClarificationRequest`, `AskClarificationResponse`
  schemas with strict JSON loaders.
- [x] Implement 4-step clarification flow: stage_selection, method_selection,
  parameter_values, confirm.
- [x] Add dependency resolution: selecting assembly auto-includes read_qc unless
  manifest provides clean_long_reads.
- [x] Method selection only for assembly (the only stage consuming
  method_decision.primary); validate method names against known registry.
- [x] Parameter inference from manifest (platform to read_type, genome_size_estimate)
  with actual missing-param check; skip parameter step when all defaults inferred.
- [x] Surface genome_size conflicts between manifest and request as warnings.
- [x] Final confirm step with stage_roles (executable/dependency/candidate_only),
  run_config_preview, and warnings.
- [x] Reject `--local-exec` in ask mode without `--run-config`; clarification
  flow manages execution stages.
- [x] Narrow exception handling: no catch-all Exception in _run_ask.
- [x] Confirmed plan writes `ask_run_config.json` and launches `_run_session`.
- [x] `--run-config` bypass preserved for operators with own configs.
- [x] 24 planner unit tests + entrypoint integration tests (406 total pass).

## Implementation Key Points

- `app/schemas/ask_plan.py`: `AskPlanState` carries the full clarification
  session (session_id, entry_request as JSON dict, manifest_ref,
  selected_stages, resolved_execution_order, selected_methods,
  method_parameters, confirmed). `AskClarificationResponse` has a
  `model_validator` that enforces kind-specific payload requirements.
- `app/entrypoints/ask_planner.py`: `next_clarification()` is the state
  machine — returns the next `AskClarificationRequest` or `None` when the
  plan is confirmed. `merge_clarification_response()` applies operator
  answers. `build_run_config_payload()` generates a `RunConfig`-valid dict
  from the confirmed plan.
- Historical note: this phase originally added upstream stages inside
  `_resolve_execution_order`; Phase 7 superseded that behavior. Current ask
  planning treats the operator-selected stages as the execution boundary and
  only orders those stages by `STAGE_ORDER`.
- Parameter inference (`_infer_parameter_defaults`): reads manifest
  raw_long_reads platform for Flye read_type, manifest genome_size_estimate,
  and entry_request genome_size_estimate. Conflicts surface as warnings
  rather than being silently swallowed.
- `_needs_parameter_values` checks actual missing required params via
  `_missing_parameters_and_defaults`, not merely whether method_parameters
  is non-None. When all required params have inferred defaults, the
  parameter step is skipped and `_auto_fill_inferred_defaults` populates
  method_parameters before confirm.
- `_validate_selected_methods` rejects empty strings and unknown method
  names against `_ALL_KNOWN_METHODS` (union of executable + planned).
- Confirm summary includes `stage_roles` dict classifying each stage as
  executable, dependency, or candidate_only so operators know which stages
  will actually run vs. which are preflight dependencies.
- `--local-exec` is rejected in ask clarification mode (only valid with
  `--run-config` bypass) since the clarification flow manages execution
  stages automatically.
- `app/entrypoints/run.py`: `_run_ask` dispatches to planner flow or
  `_run_ask_with_explicit_config` bypass. Old auto-config helpers
  (`AskAutoRunFacts`, `_maybe_write_auto_ask_run_config`,
  `_resolve_ask_auto_run_facts`, `_flye_read_type_for_platform`) removed.
- `_ASSEMBLY_EXECUTABLE_METHODS` is a temporary bridge registry; follow-up
  should move execution_support, required_parameters, and parameter_defaults
  into MethodDescriptor / descriptor YAML.

## Follow-up Items

- ASK_METHOD_CAPABILITIES bridge: move `execution_support`, `required_parameters`,
  and `parameter_defaults` into MethodDescriptor / descriptor YAML so ask planner
  reads from a single source of truth instead of maintaining a parallel registry.
- genome_survey parameters: keep method-specific (jellyfish_parameters,
  genomescope2_parameters) rather than a shared survey_parameters model, even if
  current methods happen to share fields.
- planned-only stages: show them as planned-only in clarification output; do not
  let users assume method_selections controls execution when the stage does not
  consume them.
- descriptor CWD: all stage nodes use `Path("descriptors/registry.yaml")` which
  assumes repo root as CWD; centralize or make configurable.
- manifest reuse: current repeated `_load_manifest()` calls are non-blocking but
  could be consolidated into an `AskPlanContext` if `next_clarification()` grows.
- method validation generalization: current assembly-only validation stays narrow;
  before exposing other stage method selection, move to descriptor/capability facts.

## Verification

```bash
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest tests.test_ask_plan tests.test_entrypoints tests.test_intent tests.test_run_config -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m unittest discover -s tests -t . -p 'test*.py' -v
PYTHONPYCACHEPREFIX=/tmp/gene_agent_pycache conda run -n gene_agent python -m compileall app tests
git diff --check
```
