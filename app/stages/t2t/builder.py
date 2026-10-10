"""LangGraph builder for the T2T telomere-finishing stage."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.lifecycle import CompiledMethodGraph
from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.t2t.nodes import (
    T2T_ALIGNMENT_METHOD,
    apply_t2t_patches,
    block_t2t,
    build_t2t_alignment_graph,
    commit_t2t,
    enter_t2t,
    fail_t2t,
    gate_t2t_review,
    prepare_t2t_reads,
    resolve_t2t_inputs,
    scan_assembly_telomeres,
    scan_read_telomeres,
    t2t_alignment_method_outcome,
    validate_patched_assembly,
    write_t2t_report,
)
from app.stages.t2t.parameters.workflow import (
    freeze_t2t_parameters,
    recommend_t2t_parameters,
    resolve_t2t_telomere_motif,
)
from app.stages.t2t.routing import (
    route_after_alignment,
    route_after_assembly_scan,
    route_after_enter,
    route_after_gate,
    route_after_motif_resolution,
    route_after_parameter_freeze,
    route_after_parameter_recommendation,
    route_after_patches,
    route_after_read_prepare,
    route_after_read_scan,
    route_after_report,
    route_after_resolve,
    route_after_validation,
)


def build_baseline_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Single-pass schema-3 executor for explicit deterministic baselines.

    Already-approved schema-3 cursors and saved recommendations retain their
    consumers. New model requests require an explicit recommendation mode.
    """
    graph = StateGraph(TopLevelState)
    graph.add_node("enter", enter_t2t)
    graph.add_node("resolve_inputs", resolve_t2t_inputs)
    graph.add_node("resolve_telomere_motif", resolve_t2t_telomere_motif)
    graph.add_node("recommend_parameters", recommend_t2t_parameters)
    graph.add_node("prepare_parameters", freeze_t2t_parameters)
    graph.add_node("scan_assembly", scan_assembly_telomeres)
    graph.add_node("prepare_reads", prepare_t2t_reads)
    graph.add_node("scan_reads", scan_read_telomeres)
    graph.add_node("alignment", build_t2t_alignment_graph())
    graph.add_node("apply_patches", apply_t2t_patches)
    graph.add_node("validate_patched", validate_patched_assembly)
    graph.add_node("report", write_t2t_report)
    graph.add_node("gate_review", gate_t2t_review)
    graph.add_node("block", block_t2t)
    graph.add_node("commit", commit_t2t)
    graph.add_node("fail", fail_t2t)
    graph.add_node(
        "settle_alignment_before_report",
        method_outcome_settlement_node(
            stage="t2t",
            method_name=T2T_ALIGNMENT_METHOD,
            outcome_policy=t2t_alignment_method_outcome,
        ),
    )
    graph.add_node(
        "settle_alignment_before_failure",
        method_outcome_settlement_node(
            stage="t2t",
            method_name=T2T_ALIGNMENT_METHOD,
            outcome_policy=t2t_alignment_method_outcome,
        ),
    )
    graph.add_node(
        "settle_alignment_before_patches",
        method_outcome_settlement_node(
            stage="t2t",
            method_name=T2T_ALIGNMENT_METHOD,
            outcome_policy=t2t_alignment_method_outcome,
        ),
    )

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter",
        route_after_enter,
        {
            "resolve": "resolve_inputs",
            "recommend": "recommend_parameters",
            "prepare_parameters": "prepare_parameters",
            "scan_assembly": "scan_assembly",
            "prepare_reads": "prepare_reads",
            "scan_reads": "scan_reads",
            "alignment": "alignment",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "resolve_inputs",
        route_after_resolve,
        {
            "resolve_motif": "resolve_telomere_motif",
            "recommend": "recommend_parameters",
            "prepare_reads": "prepare_reads",
            "report": "settle_alignment_before_report",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "resolve_telomere_motif",
        route_after_motif_resolution,
        {
            "recommend": "recommend_parameters",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "recommend_parameters",
        route_after_parameter_recommendation,
        {
            "block": "block",
            "prepare_parameters": "prepare_parameters",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "prepare_parameters",
        route_after_parameter_freeze,
        {
            "scan_assembly": "scan_assembly",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "scan_assembly",
        route_after_assembly_scan,
        {
            "prepare_reads": "prepare_reads",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "prepare_reads",
        route_after_read_prepare,
        {
            "scan_reads": "scan_reads",
            "report": "settle_alignment_before_report",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "scan_reads",
        route_after_read_scan,
        {"alignment": "alignment", "fail": "settle_alignment_before_failure"},
    )
    graph.add_conditional_edges(
        "alignment",
        route_after_alignment,
        {
            "apply_patches": "settle_alignment_before_patches",
            "report": "settle_alignment_before_report",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "apply_patches",
        route_after_patches,
        {
            "validate_patched": "validate_patched",
            "report": "report",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "validate_patched",
        route_after_validation,
        {"report": "report", "fail": "settle_alignment_before_failure"},
    )
    graph.add_conditional_edges(
        "report",
        route_after_report,
        {
            "gate_review": "gate_review",
            "commit": "commit",
            "fail": "settle_alignment_before_failure",
        },
    )
    graph.add_conditional_edges(
        "gate_review", route_after_gate, {"block": END, "commit": "commit"}
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)
    graph.add_edge("block", END)
    graph.add_edge("settle_alignment_before_patches", "apply_patches")
    graph.add_edge("settle_alignment_before_report", "report")
    graph.add_edge("settle_alignment_before_failure", "fail")
    return graph.compile()


def build_stage() -> CompiledMethodGraph:
    from app.stages.finishing.advisor import advisor_enabled
    from app.stages.finishing.workflow import build_stage as build_advisor_stage

    graph = StateGraph(TopLevelState)
    graph.add_node("advisor", build_advisor_stage("t2t"))
    graph.add_node("baseline", build_baseline_stage())
    graph.add_conditional_edges(
        START,
        lambda s: "advisor" if advisor_enabled(s, "t2t") else "baseline",
        ["advisor", "baseline"],
    )
    graph.add_edge("advisor", END)
    graph.add_edge("baseline", END)
    return graph.compile()
