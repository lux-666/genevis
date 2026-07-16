"""Parent LangGraph builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    CheckpointerBackend,
    get_checkpointer,
)
from app.graph.routing import route_controller, route_from_current_stage
from app.schemas.state import TopLevelState
from app.stages import STAGE_ORDER, TERMINAL_STAGE
from app.stages.assembly.builder import build_stage as build_assembly_stage
from app.stages.assembly_dedup.builder import build_stage as build_assembly_dedup_stage
from app.stages.assembly_review.builder import build_stage as build_assembly_review_stage
from app.stages.function_annotation.builder import build_stage as build_function_annotation_stage
from app.stages.gene_annotation.builder import build_stage as build_gene_annotation_stage
from app.stages.genome_survey.builder import build_stage as build_genome_survey_stage
from app.stages.hic_scaffolding.builder import build_stage as build_hic_scaffolding_stage
from app.stages.inventory.builder import build_stage as build_inventory_stage
from app.stages.read_qc.builder import build_stage as build_read_qc_stage
from app.stages.repeat_annotation.builder import build_stage as build_repeat_annotation_stage
from app.stages.t2t.builder import build_stage as build_t2t_stage


def build_graph(
    *,
    checkpointer=None,
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    data_dir: str = "data",
):
    """Build the thin parent graph.

    ``checkpointer`` overrides backend resolution when provided (used by
    tests). ``backend`` and ``data_dir`` are otherwise passed to the
    factory so a CLI run can choose sqlite or memory persistence.
    """
    graph = StateGraph(TopLevelState)

    def operator_route_controller(state: TopLevelState) -> dict:
        session_id = state.get("session_id")
        runtime_data_dir = state.get("data_dir", data_dir)
        supervision = state.get("supervision_state")
        if session_id:
            from app.control.ledger import Ledger

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
                        ledger.record_stage_completed(
                            session_id,
                            completed_stage,
                            idempotency_key=f"stage-completed:{completed_stage}",
                        )

            if supervision == "failed_terminal":
                ledger.record_session_terminal(
                    session_id,
                    outcome="failed",
                    stage=state.get("current_stage"),
                    error=state.get("last_error"),
                    idempotency_key="session-terminal:failed",
                )
            elif supervision == "completed" or state.get("current_stage") == TERMINAL_STAGE:
                ledger.record_session_terminal(
                    session_id,
                    outcome="completed",
                    stage=state.get("current_stage"),
                    idempotency_key="session-terminal:completed",
                )
            elif supervision != "blocked_human":
                current = state.get("current_stage", "inventory")
                requested = state.get("requested_stages")
                if requested and current not in {"inventory", TERMINAL_STAGE} and current not in requested:
                    start = STAGE_ORDER.index(current) if current in STAGE_ORDER else len(STAGE_ORDER)
                    next_requested = next(
                        (stage for stage in STAGE_ORDER[start + 1 :] if stage in requested),
                        TERMINAL_STAGE,
                    )
                    stop = STAGE_ORDER.index(next_requested) if next_requested in STAGE_ORDER else len(STAGE_ORDER)
                    for skipped_stage in STAGE_ORDER[start:stop]:
                        ledger.record_stage_skipped(
                            session_id,
                            skipped_stage,
                            reason="not_requested",
                            idempotency_key=f"stage-skipped:{skipped_stage}",
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

    return graph.compile(
        checkpointer=(
            checkpointer
            if checkpointer is not None
            else get_checkpointer(backend, data_dir=data_dir)
        )
    )
