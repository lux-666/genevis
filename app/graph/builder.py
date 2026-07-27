"""Parent LangGraph builder."""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

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
from app.stages.function_annotation.builder import (
    build_stage as build_function_annotation_stage,
)
from app.stages.gene_annotation.builder import (
    build_stage as build_gene_annotation_stage,
)
from app.stages.genome_survey.builder import build_stage as build_genome_survey_stage
from app.stages.hic_scaffolding.builder import (
    build_stage as build_hic_scaffolding_stage,
)
from app.stages.inventory.builder import build_stage as build_inventory_stage
from app.stages.read_qc.builder import build_stage as build_read_qc_stage
from app.stages.repeat_annotation.builder import (
    build_stage as build_repeat_annotation_stage,
)
from app.stages.t2t.builder import build_stage as build_t2t_stage
from app.workflow import (
    STAGE_ORDER,
    TERMINAL_STAGE,
    next_requested_stage,
    unrequested_stages_from_current,
)


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    data_dir: str = "data",
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

    def operator_route_controller(state: TopLevelState) -> StateUpdate:
        session_id = state.get("session_id")
        runtime_data_dir = state.get("data_dir", data_dir)
        supervision = state.get("supervision_state")
        if session_id:
            from app.storage.ledger import Ledger

            ledger = Ledger(runtime_data_dir)
            for skipped_stage, reason in (state.get("skip_reasons") or {}).items():
                if skipped_stage in STAGE_ORDER:
                    ledger.record_stage_skipped(
                        session_id,
                        skipped_stage,
                        reason=str(reason),
                        idempotency_key=f"stage-skipped:{skipped_stage}",
                    )
            for trace_item in state.get("route_trace") or []:
                if isinstance(trace_item, str) and trace_item.endswith("_stage"):
                    completed_stage = trace_item[: -len("_stage")]
                    if completed_stage in STAGE_ORDER:
                        completed_cursor = (state.get("stage_outputs") or {}).get(
                            completed_stage, {}
                        )
                        completed_refs = ledger.references_from(
                            completed_cursor,
                            role_prefix=f"stage.{completed_stage}",
                        )
                        if (
                            supervision == "blocked_human"
                            and completed_stage == state.get("current_stage")
                        ):
                            continue
                        if (
                            supervision == "failed_terminal"
                            and completed_stage == state.get("current_stage")
                        ):
                            ledger.record_stage_failed(
                                session_id,
                                completed_stage,
                                error=state.get("last_error"),
                                refs=completed_refs,
                                idempotency_key=f"stage-failed:{completed_stage}",
                            )
                        else:
                            ledger.record_stage_completed(
                                session_id,
                                completed_stage,
                                refs=completed_refs,
                                idempotency_key=f"stage-completed:{completed_stage}",
                            )

            terminal_refs = ledger.references_from(
                state,
                role_prefix="terminal",
            )

            if supervision == "failed_terminal":
                ledger.record_session_terminal(
                    session_id,
                    outcome="failed",
                    stage=state.get("current_stage"),
                    error=state.get("last_error"),
                    refs=terminal_refs,
                    idempotency_key="session-terminal:failed",
                )
            elif (
                supervision == "completed"
                or state.get("current_stage") == TERMINAL_STAGE
            ):
                ledger.record_session_terminal(
                    session_id,
                    outcome="completed",
                    stage=state.get("current_stage"),
                    refs=terminal_refs,
                    idempotency_key="session-terminal:completed",
                )
            elif supervision != "blocked_human":
                current = state.get("current_stage", "inventory")
                requested = state.get("requested_stages")
                if (
                    requested
                    and current not in {"inventory", TERMINAL_STAGE}
                    and current not in requested
                ):
                    next_requested = next_requested_stage(current, requested)
                    for unrequested_stage in unrequested_stages_from_current(
                        current,
                        requested,
                    ):
                        ledger.record_stage_skipped(
                            session_id,
                            unrequested_stage,
                            reason="not_requested",
                            idempotency_key=f"stage-skipped:{unrequested_stage}",
                        )
                    if next_requested != TERMINAL_STAGE:
                        ledger.record_stage_entered(
                            session_id,
                            next_requested,
                            route_node="route_controller",
                            idempotency_key=f"stage-entered:{next_requested}",
                        )
                elif current in STAGE_ORDER:
                    ledger.record_stage_entered(
                        session_id,
                        current,
                        route_node="route_controller",
                        idempotency_key=f"stage-entered:{current}",
                    )

        if supervision in {"failed_terminal", "blocked_human"} and session_id:
            from app.reports.review_report import write_operator_report

            write_operator_report(
                runtime_data_dir,
                session_id,
                {**state, "checkpoint_persistence": backend},
            )
        return route_controller(state)

    graph.add_node("route_controller", operator_route_controller)
    graph.add_node("inventory_stage", build_inventory_stage())
    graph.add_node("read_qc_stage", build_read_qc_stage())
    graph.add_node("genome_survey_stage", build_genome_survey_stage())
    graph.add_node("assembly_stage", build_assembly_stage())
    graph.add_node("assembly_dedup_stage", build_assembly_dedup_stage())
    graph.add_node("assembly_review_stage", build_assembly_review_stage())
    graph.add_node("hic_scaffolding_stage", build_hic_scaffolding_stage())
    graph.add_node("t2t_stage", build_t2t_stage())
    graph.add_node("repeat_annotation_stage", build_repeat_annotation_stage())
    graph.add_node("gene_annotation_stage", build_gene_annotation_stage())
    graph.add_node("function_annotation_stage", build_function_annotation_stage())

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
            "t2t_stage": "t2t_stage",
            "repeat_annotation_stage": "repeat_annotation_stage",
            "gene_annotation_stage": "gene_annotation_stage",
            "function_annotation_stage": "function_annotation_stage",
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
        "t2t_stage",
        "repeat_annotation_stage",
        "gene_annotation_stage",
        "function_annotation_stage",
    ]:
        graph.add_edge(node_name, "route_controller")

    resolved_checkpointer = (
        bound_checkpointer(checkpointer)
        if checkpointer is not None
        else get_checkpointer(backend, data_dir=data_dir)
    )
    return graph.compile(checkpointer=resolved_checkpointer)
