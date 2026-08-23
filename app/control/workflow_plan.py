"""Frozen linear workflow-step projection and runtime cursor helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from app.configuration import load_descriptor_registry
from app.schemas.run_config import RequestedStage, RunConfig
from app.schemas.state import (
    DomainStage,
    ResourceDelivery,
    StateUpdate,
    TopLevelState,
    WorkflowStep,
    WorkflowStepOutcome,
    validate_stage_outputs,
    validate_workflow_steps,
)
from app.workflow import analyze_review_sequence

if TYPE_CHECKING:
    from app.schemas.descriptors import FrozenAttemptSpec


def workflow_step_id(index: int, stage: str) -> str:
    """Return the deterministic identity of one frozen plan occurrence."""

    return f"step-{index:03d}-{stage}"


def workflow_steps_from_run_config(run_config: RunConfig) -> list[WorkflowStep]:
    """Freeze one validated RunConfig into its exact runtime sequence."""

    requests = run_config.requested_stages
    review_bindings = analyze_review_sequence([request.stage for request in requests])
    steps: list[WorkflowStep] = [
        {
            "step_id": workflow_step_id(0, "inventory"),
            "stage": "inventory",
            "allow_local_execution": False,
            "selected_method_names": [],
        }
    ]
    for index, request in enumerate(requests, start=1):
        stage = cast(DomainStage, request.stage)
        selected_method_names = _selected_method_names_for_request(run_config, request)
        step: WorkflowStep = {
            "step_id": workflow_step_id(index, stage),
            "stage": stage,
            "allow_local_execution": bool(request.allow_local_execution),
        }
        # A missing roster is intentional and distinct from an empty roster:
        # Assembly may bind its method only after upstream stage evidence is
        # available. Fixed stages keep their registry-derived immutable roster,
        # including an explicit empty list for manual-only stages.
        if selected_method_names is not None:
            step["selected_method_names"] = list(selected_method_names)
        steps.append(step)
        if stage == "assembly_review":
            producer = steps[review_bindings[index]]
            steps[-1]["review_target"] = {
                "resource_type": "assembly",
                "producer_step_id": producer["step_id"],
            }
    return validate_workflow_steps(steps)


def _selected_method_names_for_request(
    run_config: RunConfig,
    request: RequestedStage,
) -> tuple[str, ...] | None:
    """Derive a fixed roster, or defer Assembly binding to its stage."""

    registry = load_descriptor_registry()
    stage_methods = tuple(registry.by_stage.get(request.stage, ()))
    if request.stage == "read_qc":
        length_filter = (
            run_config.read_qc.length_filter if run_config.read_qc is not None else None
        )
        filter_method = (
            length_filter.selected_method() if length_filter is not None else "skip"
        )
        always = tuple(
            method_name
            for method_name in stage_methods
            if registry.methods[method_name].planner.selection_role == "always"
        )
        selected_filter = (
            "read_qc.length_filter.skip" if filter_method == "skip" else filter_method
        )
        return (*always, selected_filter)
    if request.stage == "assembly":
        # Assembly is the first stage whose method roster is bound at stage
        # time, after upstream evidence and human confirmation.
        return None
    return stage_methods


def current_workflow_step(state: TopLevelState) -> WorkflowStep | None:
    """Return the active/next occurrence, or ``None`` at the terminal cursor."""

    steps = validate_workflow_steps(state.get("workflow_steps"))
    index = state.get("workflow_step_index")
    if type(index) is not int or not 0 <= index <= len(steps):
        raise ValueError("workflow_step_index is outside workflow_steps")
    if index == len(steps):
        return None
    step = steps[index]
    if state.get("current_stage") != step["stage"]:
        raise ValueError("current_stage does not match the frozen workflow step")
    return step


def current_workflow_step_id(
    state: TopLevelState,
    *,
    expected_stage: str | None = None,
) -> str:
    """Return and optionally stage-check the current occurrence identity."""

    step = current_workflow_step(state)
    if step is None:
        raise ValueError("the workflow is already terminal")
    if expected_stage is not None and step["stage"] != expected_stage:
        raise ValueError(
            f"workflow step {step['step_id']} is {step['stage']!r}, "
            f"not {expected_stage!r}"
        )
    return step["step_id"]


def workflow_step_by_id(state: TopLevelState, step_id: str) -> WorkflowStep:
    """Resolve a step ID from the frozen checkpoint projection."""

    matches = [
        step
        for step in validate_workflow_steps(state.get("workflow_steps"))
        if step["step_id"] == step_id
    ]
    if len(matches) != 1:
        raise ValueError(f"unknown workflow step ID: {step_id!r}")
    return matches[0]


def seed_current_workflow_step(state: TopLevelState) -> StateUpdate:
    """Initialize a newly entered occurrence from immutable RunConfig seeds."""

    step = current_workflow_step(state)
    if step is None:
        return {}
    index = int(state["workflow_step_index"])
    entered = int(state.get("entered_workflow_step_index", -1))
    if entered == index:
        return {}
    if entered != index - 1:
        raise ValueError(
            "workflow can enter only the next frozen step; runtime insertion or "
            "reordering is forbidden"
        )

    stage = step["stage"]
    seeds = validate_stage_outputs(state.get("stage_seeds"))
    cursor = dict(seeds.get(stage, {}))
    if step["allow_local_execution"]:
        cursor["execution_mode"] = "local"
    else:
        cursor.pop("execution_mode", None)
    outputs = dict(state.get("stage_outputs") or {})
    outputs[stage] = cursor
    return {
        "entered_workflow_step_index": index,
        "stage_outputs": validate_stage_outputs(outputs),
    }


def advance_workflow_step(
    state: TopLevelState,
    stage: str,
    *,
    outcome: Literal["completed", "skipped"],
    reason: str | None = None,
    deliveries: Sequence[ResourceDelivery] = (),
) -> StateUpdate:
    """Advance one frozen occurrence with an explicit terminal outcome."""

    step = current_workflow_step(state)
    if step is None:
        raise ValueError("cannot advance a terminal workflow")
    if step["stage"] != stage:
        raise ValueError(
            f"cannot settle {stage!r} while active workflow step is {step['stage']!r}"
        )
    index = int(state["workflow_step_index"])
    entered = int(state.get("entered_workflow_step_index", -1))
    if entered != index:
        raise ValueError("cannot complete a workflow step before it is entered")

    next_index = index + 1
    steps = validate_workflow_steps(state.get("workflow_steps"))
    next_stage: DomainStage = (
        "done" if next_index == len(steps) else steps[next_index]["stage"]
    )
    if outcome == "skipped" and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("a skipped workflow occurrence requires a reason")
    if outcome == "skipped" and deliveries:
        raise ValueError("a skipped workflow occurrence cannot deliver resources")
    terminal: WorkflowStepOutcome = {
        "workflow_step_id": step["step_id"],
        "stage": step["stage"],
        "outcome": outcome,
    }
    if reason is not None:
        terminal["reason"] = reason
    if deliveries:
        terminal["deliveries"] = list(deliveries)
    update: StateUpdate = {
        "workflow_step_index": next_index,
        "current_stage": next_stage,
        "supervision_state": ("completed" if next_stage == "done" else "running_stage"),
        "workflow_step_outcome": terminal,
    }
    if outcome == "skipped":
        update["skip_reasons"] = {
            **(state.get("skip_reasons") or {}),
            step["step_id"]: str(reason),
        }
    return update


def fail_workflow_step(
    state: TopLevelState,
    stage: str,
    reason: str,
) -> StateUpdate:
    """Stop the active occurrence with an explicit failed terminal outcome."""

    step = current_workflow_step(state)
    if step is None or step["stage"] != stage:
        raise ValueError("failed stage does not match the active workflow occurrence")
    if int(state.get("entered_workflow_step_index", -1)) != int(
        state["workflow_step_index"]
    ):
        raise ValueError("cannot fail a workflow step before it is entered")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a failed workflow occurrence requires a reason")
    terminal: WorkflowStepOutcome = {
        "workflow_step_id": step["step_id"],
        "stage": step["stage"],
        "outcome": "failed",
        "reason": reason,
    }
    return {
        "current_stage": step["stage"],
        "supervision_state": "failed_terminal",
        "last_error": reason,
        "workflow_step_outcome": terminal,
    }


def assembly_delivery(
    candidate_bundle_ref: Mapping[str, Any],
    primary_output_ref: Mapping[str, Any],
    *,
    delivery_mode: Literal["materialized", "reused"],
) -> ResourceDelivery:
    """Build the exact candidate relation owned by one assembly delivery."""

    return {
        "resource_type": "assembly",
        "delivery_mode": delivery_mode,
        "candidate_bundle_ref": dict(candidate_bundle_ref),
        "primary_output_ref": dict(primary_output_ref),
    }


def bind_attempt_to_current_workflow_step(
    state: TopLevelState,
    attempt: "FrozenAttemptSpec",
) -> "FrozenAttemptSpec":
    """Namespace one runner attempt by its immutable workflow occurrence."""

    step = current_workflow_step(state)
    if step is None or step["stage"] != attempt.stage:
        raise ValueError("frozen attempt stage does not match the active workflow step")
    existing_step_id = (attempt.metadata or {}).get("workflow_step_id")
    if existing_step_id is not None:
        if existing_step_id != step["step_id"]:
            raise ValueError("frozen attempt is bound to another workflow step")

    attempt_id = str(attempt.attempt_id)
    occurrence_prefix = f"{attempt.session_id}:{step['step_id']}:"
    if attempt_id.startswith(occurrence_prefix):
        local_attempt_id = attempt_id[len(occurrence_prefix) :]
        if not local_attempt_id:
            raise ValueError("frozen attempt has no occurrence-local attempt ID")
    else:
        session_step_prefix = f"{attempt.session_id}:step-"
        if attempt_id.startswith(session_step_prefix):
            raise ValueError("frozen attempt ID is namespaced to another workflow step")
        local_attempt_id = attempt_id
    session_prefix = f"{attempt.session_id}:"
    if local_attempt_id.startswith(session_prefix):
        local_attempt_id = local_attempt_id[len(session_prefix) :]
    if not local_attempt_id:
        raise ValueError("frozen attempt has no occurrence-local attempt ID")
    canonical_attempt_id = f"{occurrence_prefix}{local_attempt_id}"
    metadata: dict[str, Any] = {
        **dict(attempt.metadata or {}),
        "workflow_step_id": step["step_id"],
        "workflow_step_index": int(state["workflow_step_index"]),
    }
    return attempt.model_copy(
        update={"attempt_id": canonical_attempt_id, "metadata": metadata}
    )
