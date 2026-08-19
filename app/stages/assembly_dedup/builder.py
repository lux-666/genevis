"""Assembly deduplication stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.assembly_dedup.nodes import (
    build_purge_dups_graph,
    commit_assembly_dedup,
    enter_assembly_dedup,
    fail_assembly_dedup,
    plan_purge_dups,
    purge_dups_method_outcome_policy,
    purge_dups_planner_outcome_policy,
    route_after_dedup_commit,
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
    graph.add_node(
        "settle_planner_method_outcomes",
        method_outcome_settlement_node(
            stage="assembly_dedup",
            method_name="assembly_dedup.purge_dups",
            outcome_policy=purge_dups_planner_outcome_policy,
        ),
    )
    graph.add_node(
        "settle_terminal_method_outcomes",
        method_outcome_settlement_node(
            stage="assembly_dedup",
            method_name="assembly_dedup.purge_dups",
            outcome_policy=purge_dups_method_outcome_policy,
        ),
    )

    graph.add_edge(START, "enter")
    graph.add_conditional_edges(
        "enter", route_after_enter, {"plan": "plan", "fail": "fail"}
    )
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "purge_dups": "purge_dups",
            "commit": "settle_planner_method_outcomes",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "purge_dups",
        route_after_purge_dups,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("settle_planner_method_outcomes", "commit")
    graph.add_conditional_edges(
        "commit",
        route_after_dedup_commit,
        {"done": END, "fail": "fail"},
    )
    graph.add_edge("fail", "settle_terminal_method_outcomes")
    graph.add_edge("settle_terminal_method_outcomes", END)

    return graph.compile()
