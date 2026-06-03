"""Function annotation stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.function_annotation.nodes import (
    collect_diamond_artifacts,
    commit_function_annotation,
    enter_function_annotation,
    fail_function_annotation,
    freeze_diamond_attempt,
    plan_function_methods,
    resolve_protein_inputs,
    run_diamond_attempt,
    validate_database_provenance,
)
from app.stages.function_annotation.routing import (
    route_after_diamond_collect,
    route_after_diamond_freeze,
    route_after_diamond_run,
    route_after_method_plan,
    route_after_provenance_validation,
    route_after_resolve,
)


def build_stage():
    """Build the function annotation subgraph with diamond execution."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_function_annotation)
    graph.add_node("resolve_protein_inputs", resolve_protein_inputs)
    graph.add_node("plan_function_methods", plan_function_methods)
    graph.add_node("validate_database_provenance", validate_database_provenance)
    graph.add_node("freeze_diamond_attempt", freeze_diamond_attempt)
    graph.add_node("run_diamond_attempt", run_diamond_attempt)
    graph.add_node("collect_diamond_artifacts", collect_diamond_artifacts)
    graph.add_node("commit", commit_function_annotation)
    graph.add_node("fail", fail_function_annotation)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_protein_inputs")
    graph.add_conditional_edges(
        "resolve_protein_inputs",
        route_after_resolve,
        {"methods": "plan_function_methods", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_function_methods",
        route_after_method_plan,
        {"provenance": "validate_database_provenance", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "validate_database_provenance",
        route_after_provenance_validation,
        {
            "freeze_diamond": "freeze_diamond_attempt",
            "run_diamond": "run_diamond_attempt",
            "collect_diamond": "collect_diamond_artifacts",
            "commit": "commit",
            "fail": "fail",
        },
    )
    graph.add_conditional_edges(
        "freeze_diamond_attempt",
        route_after_diamond_freeze,
        {"run_diamond": "run_diamond_attempt", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_diamond_attempt",
        route_after_diamond_run,
        {"collect_diamond": "collect_diamond_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_diamond_artifacts",
        route_after_diamond_collect,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)

    return graph.compile()
