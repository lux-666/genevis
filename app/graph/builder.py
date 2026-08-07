"""Parent LangGraph builder."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.control.projections import (
    ProgressProjectionInput,
    ProjectionBoundary,
    progress_projection_input,
)
from app.control.workflow_plan import current_workflow_step, workflow_step_by_id
from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    CheckpointerBackend,
    bound_checkpointer,
    get_checkpointer,
)
from app.graph.routing import route_controller, route_from_current_stage
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly.builder import build_stage as build_assembly_stage
from app.stages.assembly_dedup.builder import build_stage as build_assembly_dedup_stage
from app.stages.assembly_review.builder import (
    build_stage as build_assembly_review_stage,
)
from app.stages.base_polish.builder import build_stage as build_base_polish_stage
from app.stages.gapcloser.builder import build_stage as build_gapcloser_stage
from app.stages.genome_survey.builder import build_stage as build_genome_survey_stage
from app.stages.hic_scaffolding.builder import (
    build_stage as build_hic_scaffolding_stage,
)
from app.stages.inventory.builder import build_stage as build_inventory_stage
from app.stages.read_qc.builder import build_stage as build_read_qc_stage
from app.stages.t2t.builder import build_stage as build_t2t_stage
from app.workflow import TERMINAL_STAGE

if TYPE_CHECKING:
    from app.storage.frozen_inputs import FrozenInputBundle


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    data_dir: str = "data",
    verified_inputs: "FrozenInputBundle | None" = None,
    on_controller_transition: Callable[[ProgressProjectionInput], None] | None = None,
    projection_boundary: ProjectionBoundary | None = None,
) -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the thin parent graph.

    ``checkpointer`` overrides backend resolution when provided (used by
    tests). ``backend`` and ``data_dir`` are otherwise passed to the
    factory so a CLI run can choose sqlite or memory persistence.
    """
    graph = StateGraph(TopLevelState)
    progress_boundary = projection_boundary or ProjectionBoundary()

    def operator_route_controller(state: TopLevelState) -> StateUpdate:
        session_id = state.get("session_id")
        runtime_data_dir = state.get("data_dir", data_dir)
        supervision = state.get("supervision_state")
        route_update = route_controller(state)
        routed_stage = route_update.get(
            "current_stage",
            state.get("current_stage", "inventory"),
        )
        if routed_stage == TERMINAL_STAGE and supervision not in {
            "failed_terminal",
            "blocked_human",
        }:
            route_update["supervision_state"] = "completed"
        routed_state = cast(TopLevelState, {**state, **route_update})
        if session_id:
            from app.storage.ledger import Ledger

            ledger = Ledger(runtime_data_dir)
            for skipped_step_id, reason in (state.get("skip_reasons") or {}).items():
                try:
                    skipped_step = workflow_step_by_id(state, skipped_step_id)
                except ValueError:
                    continue
                ledger.record_stage_skipped(
                    session_id,
                    skipped_step["stage"],
                    workflow_step_id=skipped_step_id,
                    reason=str(reason),
                    idempotency_key=f"stage-skipped:{skipped_step_id}",
                )
            # Settle only the occurrence adjacent to the compact workflow
            # cursor. Replaying the whole route trace would bind an earlier
            # same-stage occurrence to the current occurrence's cursor refs and
            # can turn an idempotent resume into a LedgerConflictError.
            steps = state.get("workflow_steps") or []
            entered_index = state.get("entered_workflow_step_index")
            workflow_index = state.get("workflow_step_index")
            if (
                type(entered_index) is int
                and 0 <= entered_index < len(steps)
                and type(workflow_index) is int
            ):
                settled_step = steps[entered_index]
                settled_step_id = settled_step["step_id"]
                settled_stage = settled_step["stage"]
                settled_cursor = (state.get("stage_outputs") or {}).get(
                    settled_stage, {}
                )
                settled_refs = ledger.references_from(
                    settled_cursor,
                    role_prefix=f"stage.{settled_stage}",
                )
                if supervision == "failed_terminal" and workflow_index == entered_index:
                    ledger.record_stage_failed(
                        session_id,
                        settled_stage,
                        workflow_step_id=settled_step_id,
                        error=state.get("last_error"),
                        refs=settled_refs,
                        idempotency_key=f"stage-failed:{settled_step_id}",
                    )
                elif workflow_index == entered_index + 1:
                    completion_marker = f"{settled_step_id}:{settled_stage}_stage"
                    if completion_marker in (state.get("route_trace") or []):
                        ledger.record_stage_completed(
                            session_id,
                            settled_stage,
                            workflow_step_id=settled_step_id,
                            refs=settled_refs,
                            idempotency_key=f"stage-completed:{settled_step_id}",
                        )

            if supervision not in {
                "failed_terminal",
                "completed",
                "blocked_human",
            }:
                current = state.get("current_stage", "inventory")
                step = current_workflow_step(state)
                if step is not None:
                    ledger.record_stage_entered(
                        session_id,
                        current,
                        workflow_step_id=step["step_id"],
                        route_node="route_controller",
                        idempotency_key=f"stage-entered:{step['step_id']}",
                    )

        if on_controller_transition is not None:
            progress_boundary.run(
                "attached_progress",
                on_controller_transition,
                progress_projection_input(routed_state),
                disable_after_failure=True,
            )
        return route_update

    graph.add_node("route_controller", operator_route_controller)
    graph.add_node(
        "inventory_stage", build_inventory_stage(verified_inputs=verified_inputs)
    )
    graph.add_node("read_qc_stage", build_read_qc_stage())
    graph.add_node("genome_survey_stage", build_genome_survey_stage())
    graph.add_node("assembly_stage", build_assembly_stage())
    graph.add_node("assembly_dedup_stage", build_assembly_dedup_stage())
    graph.add_node("assembly_review_stage", build_assembly_review_stage())
    graph.add_node("hic_scaffolding_stage", build_hic_scaffolding_stage())
    graph.add_node("gapcloser_stage", build_gapcloser_stage())
    graph.add_node("t2t_stage", build_t2t_stage())
    graph.add_node("base_polish_stage", build_base_polish_stage())

    graph.add_edge(START, "route_controller")
    graph.add_conditional_edges(
        "route_controller",
        route_from_current_stage,
        {
            "inventory_stage": "inventory_stage",
            "read_qc_stage": "read_qc_stage",
            "genome_survey_stage": "genome_survey_stage",
            "assembly_stage": "assembly_stage",
            "assembly_dedup_stage": "assembly_dedup_stage",
            "assembly_review_stage": "assembly_review_stage",
            "hic_scaffolding_stage": "hic_scaffolding_stage",
            "gapcloser_stage": "gapcloser_stage",
            "t2t_stage": "t2t_stage",
            "base_polish_stage": "base_polish_stage",
            "end": END,
        },
    )

    for node_name in [
        "inventory_stage",
        "read_qc_stage",
        "genome_survey_stage",
        "assembly_stage",
        "assembly_dedup_stage",
        "assembly_review_stage",
        "hic_scaffolding_stage",
        "gapcloser_stage",
        "t2t_stage",
        "base_polish_stage",
    ]:
        graph.add_edge(node_name, "route_controller")

    resolved_checkpointer = (
        bound_checkpointer(checkpointer)
        if checkpointer is not None
        else get_checkpointer(backend, data_dir=data_dir)
    )
    return graph.compile(checkpointer=resolved_checkpointer)
