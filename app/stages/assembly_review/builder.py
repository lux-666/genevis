"""Assembly review stage builder.

Builds the assembly review subgraph with candidate resolution,
metric planning, rule evaluation, human gate, and commit.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.assembly_review.nodes import (
    collect_quast_artifacts,
    commit_assembly_review,
    enter_assembly_review,
    evaluate_review_rules,
    fail_assembly_review,
    freeze_quast_attempts,
    human_review_gate,
    plan_review_metrics,
    resolve_candidates,
    run_quast_attempts,
)
from app.stages.assembly_review.routing import (
    route_after_plan_metrics,
    route_after_quast_collect,
    route_after_quast_freeze,
    route_after_quast_run,
    route_from_evaluation,
)


def build_stage():
    """Build the assembly review subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly_review)
    graph.add_node("resolve_candidates", resolve_candidates)
    graph.add_node("plan_metrics", plan_review_metrics)
    graph.add_node("freeze_quast", freeze_quast_attempts)
    graph.add_node("run_quast", run_quast_attempts)
    graph.add_node("collect_quast", collect_quast_artifacts)
    graph.add_node("evaluate_rules", evaluate_review_rules)
    graph.add_node("human_gate", human_review_gate)
    graph.add_node("commit", commit_assembly_review)
    graph.add_node("fail", fail_assembly_review)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_candidates")
    graph.add_edge("resolve_candidates", "plan_metrics")
    graph.add_conditional_edges(
        "plan_metrics",
        route_after_plan_metrics,
        {
            "freeze_quast": "freeze_quast",
            "evaluate_rules": "evaluate_rules",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "freeze_quast",
        route_after_quast_freeze,
        {
            "run_quast": "run_quast",
            "evaluate_rules": "evaluate_rules",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "run_quast",
        route_after_quast_run,
        {"collect_quast": "collect_quast", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_quast",
        route_after_quast_collect,
        {"evaluate_rules": "evaluate_rules", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "evaluate_rules",
        route_from_evaluation,
        {
            "commit": "commit",
            "human_gate": "human_gate",
            "fail": "fail",
        },
    )
    graph.add_edge("human_gate", END)
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
