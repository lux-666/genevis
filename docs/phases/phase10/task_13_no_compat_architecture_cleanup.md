# Task 13: No-Compatibility Architecture Cleanup

**Phase**: 10
**Status**: Completed for current Phase10 scope
**Depends on**: Tasks 8-12
**Created**: 2026-07-06
**Updated**: 2026-07-07

## Goal

Remove the old architecture drift that Phase10 would otherwise preserve:

1. planner method metadata duplicated outside descriptors,
2. assembly LLM decisions scattered inside stage nodes,
3. parameter-loop state stored as loose ad hoc dict keys.

This is not a compatibility migration. Delete the old paths once the new ones
are wired and tested.

## Source Of Truth Cleanup

`descriptors/registry.yaml`, method descriptors, and parameter catalogs are the
source of truth for method availability, operator-selectable methods,
parameters, defaults, and planner display metadata.

Remove the hardcoded planner registry from `app/entrypoints/ask_planner.py`.
Replace it with a descriptor-derived planner catalog builder, for example:

```python
def build_planner_method_catalog(registry: DescriptorRegistry) -> PlannerMethodCatalog:
    """Derive selectable stages, methods, required params, defaults, and roles."""
```

Requirements:

- `ask_planner.py` must not maintain `_STAGE_METHODS` or parallel required
  parameter/default maps.
- Descriptor or parameter-catalog metadata must carry any planner-facing fields
  that are still needed, such as `selection_role`, `display_name`, and safe
  defaults.
- The registry executable invariant should compare registry methods with wired
  runners through stage-owned declarations, not through planner constants.
- Adding or removing a method should require descriptor/catalog/runner changes,
  not a separate planner-constant edit.

## Decision Boundary Cleanup

Assembly decisions that use an LLM must go through one shared decision/advisor
boundary introduced in Task 8.

Requirements:

- Remove assembly-local helpers that directly call the chat model.
- Route selection and parameter recommendation both produce typed decision
  artifacts.
- Decision artifacts record prompt refs, response refs, parsed output,
  validation result, model identifier, allowed actions or adjustable params, and
  rationale.
- Stage nodes only apply a validated decision to graph state.

## Typed State Cleanup

The top-level state can keep `stage_outputs`, but Phase10 parameter-loop content
inside `stage_outputs["assembly"]` must be typed before it is read or written.

Introduce compact contracts such as:

```python
class ParameterAttempt(BaseModel):
    attempt_id: int
    method_id: str
    status: str
    input_profile_ref: ArtifactRef
    prompt_payload_ref: ArtifactRef
    llm_response_ref: ArtifactRef | None = None
    validation_ref: ArtifactRef | None = None
    selected_case_ids: list[str]
    parameter_sources: dict[str, dict[str, Any]] = {}
    argv: list[str] = []


class AssemblyParameterLoopState(BaseModel):
    current_parameter_attempt_id: int
    parameter_attempts: list[ParameterAttempt]
    retry_counts: dict[str, int] = {}
```

Requirements:

- Freeze nodes read the current `ParameterAttempt`, not old loose keys such as
  `recommended_params` as a parallel API.
- Validation failures and LLM failures append typed attempts before stopping.
- Retry gates append typed attempts for new recommendations and only complete
  the reviewed/executed attempt in place with lifecycle facts such as
  execution results, log tails, and review failure refs.
- Large payloads remain artifact refs; the typed state stores compact refs and
  status only.

## Deletion List

Delete, do not wrap:

- planner `_STAGE_METHODS` and related duplicate required/default maps,
- assembly-local direct LLM invocation helpers,
- hash-only prompt/profile artifact runtime paths,
- method-level retry directories for parameter attempts,
- legacy parameter recommendation keys kept only for compatibility.

## Tests

- Planner catalog is derived from descriptors and parameter catalogs.
- `ask_planner.py` has no `_STAGE_METHODS` registry.
- Registry executable invariant no longer imports planner constants.
- Assembly stage nodes do not call `build_chat_model().invoke`.
- Parameter attempts validate through Pydantic models.
- Freeze nodes consume the typed current attempt.
- Running graph uses session/attempt refs only.

## Acceptance Criteria

- [x] Method metadata has one source of truth.
- [x] Interactive planning derives from descriptors/catalogs.
- [x] Assembly LLM decisions use the shared decision boundary.
- [x] Parameter-loop state is typed and compact.
- [x] Old compatibility paths listed above are deleted.
- [x] Architecture-regression tests fail if any deleted path returns.

## Current Cleanup State

- Done: assembly-local direct model invocation was moved behind
  `app/stages/assembly/decision.py`.
- Done: hifiasm/hifiasm_ont freeze nodes consume typed `parameter_attempts`
  instead of old ad hoc recommended-parameter state.
- Done: hash-only prompt/profile runtime artifact paths were removed from the
  parameter-loop writer.
- Done: planner `_STAGE_METHODS` was removed; planner method metadata is
  derived from descriptors through `app/entrypoints/planner_catalog.py`.
- Done: `ask_interactive.py` consumes the same descriptor-derived planner
  catalog.
- Done: registry executable invariant tests no longer import planner constants.
