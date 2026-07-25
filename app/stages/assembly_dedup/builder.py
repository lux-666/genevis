"""Assembly deduplication stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.schemas.state import TopLevelState
from app.stages.assembly_dedup.nodes import (
    build_purge_dups_graph,
    commit_assembly_dedup,
    enter_assembly_dedup,
    fail_assembly_dedup,
    plan_purge_dups,
)
from app.stages.assembly_dedup.routing import (
    route_after_enter,
    route_after_plan,
    route_after_purge_dups,
)


def build_stage() -> CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]:
    """Build the assembly deduplication subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly_dedup)
    graph.add_node("plan", plan_purge_dups)
    graph.add_node("purge_dups", build_purge_dups_graph())
    graph.add_node("commit", commit_assembly_dedup)
    graph.add_node("fail", fail_assembly_dedup)

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter", route_after_enter, {"plan": "plan", "fail": "fail"}
    )
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {"purge_dups": "purge_dups", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "purge_dups",
        route_after_purge_dups,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
