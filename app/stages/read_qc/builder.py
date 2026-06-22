"""Read QC stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.read_qc.method_application import (
    build_make_fofn_graph,
    build_seqkit_length_filter_graph,
    build_seqkit_stats_graph,
)
from app.stages.read_qc.nodes import (
    commit_read_qc,
    enter_read_qc,
    fail_read_qc,
    plan_length_filter,
    plan_seqkit_stats,
    resolve_raw_long_reads,
)
from app.stages.read_qc.routing import (
    route_after_length_filter,
    route_after_make_fofn,
    route_after_plan,
    route_after_resolve,
    route_after_stats,
)


def build_stage():
    """Build the read QC subgraph for SeqKit stats."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_read_qc)
    graph.add_node("resolve_raw_long_reads", resolve_raw_long_reads)
    graph.add_node("plan_seqkit_stats", plan_seqkit_stats)
    graph.add_node("seqkit_stats", build_seqkit_stats_graph())
    graph.add_node("plan_length_filter", plan_length_filter)
    graph.add_node("seqkit_length_filter", build_seqkit_length_filter_graph())
    graph.add_node("make_fofn", build_make_fofn_graph())
    graph.add_node("commit", commit_read_qc)
    graph.add_node("fail", fail_read_qc)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_raw_long_reads")
    graph.add_conditional_edges(
        "resolve_raw_long_reads",
        route_after_resolve,
        {"plan": "plan_seqkit_stats", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_seqkit_stats",
        route_after_plan,
        {"stats": "seqkit_stats", "filter": "plan_length_filter", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "seqkit_stats",
        route_after_stats,
        {"filter": "plan_length_filter", "fail": "fail"},
    )
    graph.add_edge("plan_length_filter", "seqkit_length_filter")
    graph.add_conditional_edges(
        "seqkit_length_filter",
        route_after_length_filter,
        {"fofn": "make_fofn", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "make_fofn",
        route_after_make_fofn,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
