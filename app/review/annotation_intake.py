"""Annotation intake planning at the assembly-review handoff."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.entrypoints.ask_interactive import _UserAbort, _prompt_confirm, _prompt_for_answer, _render_plan_summary
from app.entrypoints.ask_planner import (
    _auto_fill_inferred_defaults,
    _fill_missing_method_defaults,
    _method_selection_request,
    _needs_parameter_values,
    _normalize_selected_methods,
    _parameter_values_request,
    _recommended_stages_from_manifest,
    _validate_selected_methods,
    build_run_plan_summary,
    build_run_config_payload,
    merge_clarification_response,
)
from app.execution.artifacts import append_artifact_records
from app.schemas.artifacts import ArtifactMeta
from app.schemas.ask_plan import AskClarificationRequest, AskPlanState
from app.schemas.state import TopLevelState
from app.stages import STAGE_ORDER

ANNOTATION_INTAKE_PLAN = "annotation.intake_plan"
ANNOTATION_INTAKE_METHOD = "annotation.intake"
ASSEMBLY_REVIEW_STAGE = "assembly_review"
CONTINUE_DOWNSTREAM_DECISIONS = frozenset({"accept", "pass", "continue_low_confidence"})
ASSEMBLY_OPERATION_DECISIONS = frozenset({"purge", "polish", "rerun"})
TERMINATE_DECISIONS = frozenset({"stop_human_review"})
DOWNSTREAM_STAGES = tuple(STAGE_ORDER[STAGE_ORDER.index(ASSEMBLY_REVIEW_STAGE) + 1 :])


@dataclass(frozen=True)
class AnnotationIntakeResult:
    """State update plus durable artifact from annotation intake."""

    update: dict[str, Any]
    artifact_ref: str
    changed: bool
    final_requested_stages: list[str]


@dataclass(frozen=True)
class AnnotationIntakeSelection:
    """In-memory intake selection collected before resume side effects."""

    recommended_plan: AskPlanState
    final_plan: AskPlanState


def classify_assembly_review_decision(decision: str) -> str:
    """Classify assembly-review decisions for handoff routing."""
    if decision in CONTINUE_DOWNSTREAM_DECISIONS:
        return "continue_downstream"
    if decision in ASSEMBLY_OPERATION_DECISIONS:
        return "assembly_operation"
    if decision in TERMINATE_DECISIONS:
        return "terminate"
    return "other"


def should_collect_annotation_intake(stage: str, decision: str) -> bool:
    return stage == ASSEMBLY_REVIEW_STAGE and classify_assembly_review_decision(decision) == "continue_downstream"


def maybe_collect_annotation_intake(
    *,
    state: TopLevelState,
    resume_update: dict[str, Any],
    request: Any,
    source: str,
    interactive: bool = True,
) -> AnnotationIntakeResult | None:
    """Collect and persist annotation intake for interactive continue decisions."""
    if not should_collect_annotation_intake(request.stage, request.decision):
        return None

    merged_state = _merge_state_update(state, resume_update)
    selection = collect_annotation_intake_selection(
        state=merged_state,
        request=request,
        interactive=interactive,
    )
    if selection is None:
        return None
    return persist_annotation_intake_plan(
        state=merged_state,
        recommended_plan=selection.recommended_plan,
        final_plan=selection.final_plan,
        request=request,
        source=source,
    )


def collect_annotation_intake_selection(
    *,
    state: TopLevelState,
    request: Any,
    interactive: bool = True,
) -> AnnotationIntakeSelection | None:
    """Collect annotation intake in memory before resume writes durable records."""
    if not should_collect_annotation_intake(request.stage, request.decision):
        return None

    recommended = build_recommended_annotation_plan(state)
    final_plan = prompt_annotation_intake_plan(recommended) if interactive else recommended
    final_plan = _auto_fill_inferred_defaults(final_plan)
    return AnnotationIntakeSelection(recommended_plan=recommended, final_plan=final_plan)


def build_recommended_annotation_plan(state: TopLevelState) -> AskPlanState:
    """Build a downstream-only annotation intake recommendation."""
    prior_downstream = _prior_downstream_requested_stages(state)
    manifest_downstream = [
        stage for stage in _recommended_stages_from_manifest(str(state.get("input_manifest_ref") or ""))
        if stage in DOWNSTREAM_STAGES
    ]
    selected = prior_downstream if prior_downstream or state.get("requested_stages") is None else manifest_downstream
    if not selected:
        selected = manifest_downstream
    selected = _ordered_downstream(selected)

    plan = AskPlanState(
        session_id=str(state["session_id"]),
        data_dir=str(state.get("data_dir", "data")),
        request_text=None,
        entry_request={
            "session_id": state["session_id"],
            "data_dir": state.get("data_dir", "data"),
            "manifest": state.get("input_manifest_ref"),
            "source": "annotation_intake",
        },
        manifest_ref=str(state.get("input_manifest_ref") or ""),
        selected_stages=selected,
        resolved_execution_order=selected,
    )
    selected_methods = _fill_missing_method_defaults(plan, {})
    if selected_methods:
        _validate_selected_methods(selected_methods, plan)
        selected_methods = _normalize_selected_methods(selected_methods)
        plan = plan.model_copy(update={"selected_methods": selected_methods})
    return _auto_fill_inferred_defaults(plan)


def prompt_annotation_intake_plan(plan: AskPlanState) -> AskPlanState:
    """Render, accept, or edit a downstream annotation intake plan."""
    try:
        _render_plan_summary(build_run_plan_summary(plan), title="Annotation Intake Plan", preview=True)
        if _prompt_confirm("Accept this annotation intake plan?"):
            return plan.model_copy(update={"confirmed": True})

        print("\n--- Annotation Intake Replan ---")
        print("Only stages after assembly_review can be changed here.")
        plan = _collect_downstream_stage_selection(plan)
        plan = _collect_downstream_method_selection(plan)
        plan = _collect_downstream_parameters(plan)
        _render_plan_summary(build_run_plan_summary(plan), title="Annotation Intake Plan", preview=True)
        if _prompt_confirm("Proceed with this annotation intake plan?"):
            return plan.model_copy(update={"confirmed": True})
    except (_UserAbort, EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError("annotation intake cancelled") from exc
    else:
        raise RuntimeError("annotation intake cancelled")


def persist_annotation_intake_plan(
    *,
    state: TopLevelState,
    recommended_plan: AskPlanState,
    final_plan: AskPlanState,
    request: Any,
    source: str,
) -> AnnotationIntakeResult:
    """Persist annotation intake and return the checkpoint update."""
    session_id = str(state["session_id"])
    data_dir = Path(str(state.get("data_dir", "data")))
    prior_plan = _current_downstream_plan(state)
    recommended_payload = _plan_payload(recommended_plan)
    final_payload = _plan_payload(final_plan)
    changed = final_payload != recommended_payload

    requested_stages = rewrite_requested_stages_after_assembly_review(
        state.get("requested_stages"),
        final_plan.resolved_execution_order or [],
    )
    run_config_preview = build_run_config_payload(final_plan)
    update = _checkpoint_update_from_plan(state, final_plan, requested_stages, run_config_preview)

    payload = {
        "session_id": session_id,
        "stage": ASSEMBLY_REVIEW_STAGE,
        "artifact_type": ANNOTATION_INTAKE_PLAN,
        "source": source,
        "decision": {
            "stage": request.stage,
            "gate_id": getattr(request, "gate_id", None),
            "decision": request.decision,
            "candidate_id": getattr(request, "candidate_id", None),
            "final_assembly_ref": getattr(request, "final_assembly_ref", None),
        },
        "prior_plan": prior_plan,
        "recommended_plan": recommended_payload,
        "final_plan": final_payload,
        "final_requested_stages": requested_stages,
        "changed": changed,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    out_dir = data_dir / "artifacts" / ASSEMBLY_REVIEW_STAGE / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = out_dir / f"annotation_intake_plan_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    artifact = ArtifactMeta.from_file(
        path,
        stage=ASSEMBLY_REVIEW_STAGE,
        method_name=ANNOTATION_INTAKE_METHOD,
        artifact_type=ANNOTATION_INTAKE_PLAN,
        metadata={
            "source": source,
            "changed": changed,
            "final_requested_stages": requested_stages,
            "decision": request.decision,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])

    stage_outputs = dict(update.get("stage_outputs") or {})
    ar_out = dict(stage_outputs.get(ASSEMBLY_REVIEW_STAGE) or {})
    ar_out["annotation_intake_plan_ref"] = str(path)
    stage_outputs[ASSEMBLY_REVIEW_STAGE] = ar_out
    update["stage_outputs"] = stage_outputs

    Ledger(data_dir).record_event(
        session_id,
        "annotation_intake_planned",
        {
            "stage": ASSEMBLY_REVIEW_STAGE,
            "artifact_ref": str(path),
            "source": source,
            "changed": changed,
            "prior_requested_stages": prior_plan["requested_stages"],
            "recommended_stages": recommended_payload["resolved_execution_order"],
            "final_requested_stages": requested_stages,
            "decision": request.decision,
        },
    )

    return AnnotationIntakeResult(
        update=update,
        artifact_ref=str(path),
        changed=changed,
        final_requested_stages=requested_stages,
    )


def load_annotation_intake_plan(state: TopLevelState) -> dict[str, Any]:
    """Load and validate the annotation intake plan recorded at assembly_review.

    The intake plan is the start and plan-of-record for the annotation chain.
    Raises ``ValueError`` when the reference is missing or the file cannot be
    read as a JSON object so the gene_annotation entry node can fail before any
    gate runs.
    """
    ref = ((state.get("stage_outputs") or {}).get(ASSEMBLY_REVIEW_STAGE) or {}).get("annotation_intake_plan_ref")
    if not ref:
        raise ValueError("annotation.intake_plan missing: assembly_review.annotation_intake_plan_ref is not set")
    path = Path(str(ref))
    if not path.exists():
        raise ValueError(f"annotation.intake_plan missing: file not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"annotation.intake_plan invalid: cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("annotation.intake_plan invalid: top-level JSON is not an object")
    return payload


def rewrite_requested_stages_after_assembly_review(
    current_requested: list[str] | tuple[str, ...] | None,
    downstream_stages: list[str] | tuple[str, ...],
) -> list[str]:
    """Preserve the prefix through assembly_review and replace the downstream suffix."""
    downstream = _ordered_downstream(downstream_stages)
    if current_requested is None:
        prefix = [stage for stage in STAGE_ORDER if stage != "inventory" and STAGE_ORDER.index(stage) <= STAGE_ORDER.index(ASSEMBLY_REVIEW_STAGE)]
        return [*prefix, *downstream]

    requested = list(current_requested)
    if ASSEMBLY_REVIEW_STAGE in requested:
        idx = requested.index(ASSEMBLY_REVIEW_STAGE)
        prefix = requested[: idx + 1]
    else:
        prefix = [stage for stage in requested if stage not in DOWNSTREAM_STAGES]
        if ASSEMBLY_REVIEW_STAGE not in prefix:
            prefix.append(ASSEMBLY_REVIEW_STAGE)
    return [*prefix, *[stage for stage in downstream if stage not in prefix]]


def _collect_downstream_stage_selection(plan: AskPlanState) -> AskPlanState:
    req = AskClarificationRequest(
        status="clarification_required",
        kind="stage_selection",
        session_id=plan.session_id,
        ask_plan_ref="annotation_intake",
        manifest_ref=plan.manifest_ref,
        question="Which downstream annotation stages should run?",
        choices={"available_stages": list(DOWNSTREAM_STAGES), "suggested_stages": plan.selected_stages or []},
        required_fields=["selected_stages"],
        response_schema={"kind": "stage_selection", "selected_stages": plan.selected_stages or []},
        next_action={"kind": "interactive_annotation_intake"},
    )
    response = _prompt_for_answer(req)
    selected = _ordered_downstream(response.selected_stages or [])
    return plan.model_copy(
        update={
            "selected_stages": selected,
            "resolved_execution_order": selected,
            "selected_methods": None,
            "shared_parameters": plan.shared_parameters,
            "method_parameters": None,
            "confirmed": False,
        }
    )


def _collect_downstream_method_selection(plan: AskPlanState) -> AskPlanState:
    if not plan.resolved_execution_order:
        return plan.model_copy(update={"selected_methods": None, "method_parameters": None})
    req = _method_selection_request(plan, "annotation_intake")
    response = _prompt_for_answer(req)
    plan = merge_clarification_response(plan, response)
    selected = dict(plan.selected_methods or {})
    selected = {stage: methods for stage, methods in selected.items() if stage in DOWNSTREAM_STAGES}
    return plan.model_copy(update={"selected_methods": selected or None})


def _collect_downstream_parameters(plan: AskPlanState) -> AskPlanState:
    plan = _auto_fill_inferred_defaults(plan)
    if not _needs_parameter_values(plan):
        return plan
    req = _parameter_values_request(plan, "annotation_intake")
    response = _prompt_for_answer(req)
    return merge_clarification_response(plan, response)


def _checkpoint_update_from_plan(
    state: TopLevelState,
    plan: AskPlanState,
    requested_stages: list[str],
    run_config_preview: dict[str, Any],
) -> dict[str, Any]:
    update: dict[str, Any] = {"requested_stages": requested_stages}
    stage_outputs = {
        stage: dict(output)
        for stage, output in (state.get("stage_outputs") or {}).items()
        if stage not in DOWNSTREAM_STAGES
    }
    skip_reasons = {
        stage: reason
        for stage, reason in (state.get("skip_reasons") or {}).items()
        if stage not in DOWNSTREAM_STAGES
    }
    local_exec = set(run_config_preview.get("local_exec") or [])
    selected_methods = dict(plan.selected_methods or {})
    stage_parameters = dict(plan.stage_parameters or {})

    existing_downstream = state.get("stage_outputs") or {}
    for stage in DOWNSTREAM_STAGES:
        if stage not in selected_methods and stage not in requested_stages:
            continue
        # Preserve any existing downstream stage output (e.g. function_annotation
        # database_provenance seeded from run_config) and only overlay the
        # method-selection fields from the intake plan. Stages dropped from the
        # suffix are still omitted by the guard above.
        stage_out: dict[str, Any] = dict(existing_downstream.get(stage) or {})
        methods = selected_methods.get(stage)
        method_list = methods if isinstance(methods, list) else [methods] if methods else []
        if method_list:
            stage_out["selected_methods"] = method_list
            if len(method_list) == 1:
                stage_out["primary_method"] = method_list[0]
        if stage in stage_parameters:
            stage_out.update(stage_parameters[stage])
        if stage in local_exec:
            stage_out["execution_mode"] = "local"
        stage_outputs[stage] = stage_out

    if plan.shared_parameters is not None:
        update["shared_parameters"] = plan.shared_parameters.model_dump(exclude_none=True)

    update["stage_outputs"] = stage_outputs
    update["skip_reasons"] = skip_reasons
    return update


def _current_downstream_plan(state: TopLevelState) -> dict[str, Any]:
    return {
        "requested_stages": _prior_downstream_requested_stages(state),
        "stage_outputs": {
            stage: dict((state.get("stage_outputs") or {}).get(stage) or {})
            for stage in DOWNSTREAM_STAGES
            if (state.get("stage_outputs") or {}).get(stage)
        },
    }


def _plan_payload(plan: AskPlanState) -> dict[str, Any]:
    payload = build_run_plan_summary(plan)
    payload["run_config_preview"] = build_run_config_payload(plan)
    return payload


def _prior_downstream_requested_stages(state: TopLevelState) -> list[str]:
    requested = state.get("requested_stages")
    if requested is None:
        return list(DOWNSTREAM_STAGES)
    if ASSEMBLY_REVIEW_STAGE not in requested:
        return [stage for stage in requested if stage in DOWNSTREAM_STAGES]
    idx = list(requested).index(ASSEMBLY_REVIEW_STAGE)
    return _ordered_downstream(list(requested)[idx + 1 :])


def _ordered_downstream(stages: list[str] | tuple[str, ...]) -> list[str]:
    selected = set(stages)
    return [stage for stage in DOWNSTREAM_STAGES if stage in selected]


def _merge_state_update(state: TopLevelState, update: dict[str, Any]) -> TopLevelState:
    merged: dict[str, Any] = dict(state)
    for key, value in update.items():
        if key == "stage_outputs" and isinstance(value, dict):
            stage_outputs = dict(state.get("stage_outputs") or {})
            stage_outputs.update(value)
            merged[key] = stage_outputs
        else:
            merged[key] = value
    return merged  # type: ignore[return-value]
