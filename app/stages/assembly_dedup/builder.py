"""Assembly deduplication stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.execution.method_outcomes import method_outcome_settlement_node
from app.schemas.state import TopLevelState
from app.stages.assembly_dedup.advisor import (
    build_quast_graph,
    quast_outcome,
    recommend_dedup,
    record_attempt,
)
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
from app.stages.assembly_dedup.state import assembly_dedup_output


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
    graph.add_node("evaluate", build_quast_graph())
    graph.add_node("recommend", recommend_dedup)
    graph.add_node("assess", record_attempt)
    graph.add_node("commit", commit_assembly_dedup)
    graph.add_node("fail", fail_assembly_dedup)
    graph.add_node(
        "settle_planner_method_outcomes",
        method_outcome_settlement_node(
            stage="assembly_dedup",
            method_name="assembly_dedup.purge_dups",
            outcome_policy=lambda state: (
                purge_dups_method_outcome_policy(state)
                if assembly_dedup_output(state).get("recommendation_mode") == "advisor"
                else purge_dups_planner_outcome_policy(state)
            ),
        ),
    )
    graph.add_node(
        "settle_quast",
        method_outcome_settlement_node(
            stage="assembly_dedup",
            method_name="assembly_dedup.eval.quast",
            outcome_policy=quast_outcome,
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
            "evaluate": "evaluate",
            "recommend": "recommend",
            "block": END,
        },
    )
    graph.add_conditional_edges(
        "purge_dups",
        route_after_purge_dups,
        {"commit": "commit", "fail": "fail", "assess": "assess"},
    )
    graph.add_edge("assess", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        lambda state: (
            "fail"
            if assembly_dedup_output(state).get("status") == "failed"
            else "recommend"
        ),
        {"fail": "fail", "recommend": "recommend"},
    )
    graph.add_edge("recommend", END)
    graph.add_edge("settle_planner_method_outcomes", "settle_quast")
    graph.add_conditional_edges(
        "settle_quast",
        lambda state: (
            "done"
            if assembly_dedup_output(state).get("status") == "failed"
            else "commit"
        ),
        {"done": END, "commit": "commit"},
    )
    graph.add_conditional_edges(
        "commit",
        route_after_dedup_commit,
        {"done": END, "fail": "fail"},
    )
    graph.add_edge("fail", "settle_terminal_method_outcomes")
    graph.add_edge("settle_terminal_method_outcomes", "settle_quast")

    return graph.compile()
