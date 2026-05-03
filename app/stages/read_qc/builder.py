"""Read QC stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.read_qc.nodes import (
    collect_seqkit_artifacts,
    commit_read_qc,
    enter_read_qc,
    fail_read_qc,
    freeze_seqkit_attempt,
    make_fofn,
    maybe_run_length_filter,
    plan_length_filter,
    plan_seqkit_stats,
    resolve_raw_long_reads,
    run_seqkit_stats,
)
from app.stages.read_qc.routing import (
    route_after_collect,
    route_after_length_filter,
    route_after_make_fofn,
    route_after_plan,
    route_after_resolve,
    route_after_run,
)


def build_stage():
    """Build the read QC subgraph for SeqKit stats."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_read_qc)
    graph.add_node("resolve_raw_long_reads", resolve_raw_long_reads)
    graph.add_node("plan_seqkit_stats", plan_seqkit_stats)
    graph.add_node("freeze_seqkit_attempt", freeze_seqkit_attempt)
    graph.add_node("run_seqkit_stats", run_seqkit_stats)
    graph.add_node("collect_seqkit_artifacts", collect_seqkit_artifacts)
    graph.add_node("plan_length_filter", plan_length_filter)
    graph.add_node("maybe_run_length_filter", maybe_run_length_filter)
    graph.add_node("make_fofn", make_fofn)
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
        {"freeze": "freeze_seqkit_attempt", "filter": "plan_length_filter", "fail": "fail"},
    )
    graph.add_edge("freeze_seqkit_attempt", "run_seqkit_stats")
    graph.add_conditional_edges(
        "run_seqkit_stats",
        route_after_run,
        {"collect": "collect_seqkit_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_seqkit_artifacts",
        route_after_collect,
        {"filter": "plan_length_filter", "fail": "fail"},
    )
    graph.add_edge("plan_length_filter", "maybe_run_length_filter")
    graph.add_conditional_edges(
        "maybe_run_length_filter",
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
