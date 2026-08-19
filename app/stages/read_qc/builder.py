"""Read QC stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.read_qc.constants import (
    LENGTH_FILTER_METHOD_NAME,
    LENGTH_FILTER_SKIP_METHOD_NAME,
    METHOD_NAME,
)
from app.stages.read_qc.method_application import (
    build_seqkit_length_filter_graph,
    build_seqkit_stats_graph,
)
from app.stages.read_qc.method_application.seqkit_length_filter import (
    seqkit_length_filter_outcome,
)
from app.stages.read_qc.method_application.seqkit_stats import seqkit_stats_outcome
from app.stages.read_qc.nodes import (
    commit_read_qc,
    enter_read_qc,
    fail_read_qc,
    length_filter_skip_outcome,
    plan_length_filter,
    plan_seqkit_stats,
    resolve_raw_long_reads,
)
from app.stages.read_qc.routing import (
    route_after_length_filter,
    route_after_plan,
    route_after_resolve,
    route_after_stats,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the read QC subgraph for SeqKit stats."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_read_qc)
    graph.add_node("resolve_raw_long_reads", resolve_raw_long_reads)
    graph.add_node("plan_seqkit_stats", plan_seqkit_stats)
    graph.add_node("seqkit_stats", build_seqkit_stats_graph())
    graph.add_node("plan_length_filter", plan_length_filter)
    graph.add_node("seqkit_length_filter", build_seqkit_length_filter_graph())
    graph.add_node(
        "settle_seqkit_stats_outcome",
        method_outcome_settlement_node(
            stage="read_qc",
            method_name=METHOD_NAME,
            outcome_policy=seqkit_stats_outcome,
        ),
    )
    graph.add_node(
        "settle_seqkit_length_filter_outcome",
        method_outcome_settlement_node(
            stage="read_qc",
            method_name=LENGTH_FILTER_METHOD_NAME,
            outcome_policy=seqkit_length_filter_outcome,
        ),
    )
    graph.add_node(
        "settle_length_filter_skip_outcome",
        method_outcome_settlement_node(
            stage="read_qc",
            method_name=LENGTH_FILTER_SKIP_METHOD_NAME,
            outcome_policy=length_filter_skip_outcome,
        ),
    )
    graph.add_node("commit", commit_read_qc)
    graph.add_node("fail", fail_read_qc)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_raw_long_reads")
    graph.add_conditional_edges(
        "resolve_raw_long_reads",
        route_after_resolve,
        {"plan": "plan_seqkit_stats", "fail": "settle_seqkit_stats_outcome"},
    )
    graph.add_conditional_edges(
        "plan_seqkit_stats",
        route_after_plan,
        {
            "stats": "seqkit_stats",
            "filter": "plan_length_filter",
            "fail": "settle_seqkit_stats_outcome",
        },
    )
    graph.add_conditional_edges(
        "seqkit_stats",
        route_after_stats,
        {"filter": "plan_length_filter", "fail": "settle_seqkit_stats_outcome"},
    )
    graph.add_edge("plan_length_filter", "seqkit_length_filter")
    graph.add_edge("seqkit_length_filter", "settle_seqkit_stats_outcome")
    graph.add_edge("settle_seqkit_stats_outcome", "settle_seqkit_length_filter_outcome")
    graph.add_edge(
        "settle_seqkit_length_filter_outcome", "settle_length_filter_skip_outcome"
    )
    graph.add_conditional_edges(
        "settle_length_filter_skip_outcome",
        route_after_length_filter,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
