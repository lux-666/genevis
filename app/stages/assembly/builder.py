"""Assembly stage builder."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.schemas.state import TopLevelState
from app.stages.assembly.nodes import (
    block_assembly_for_human,
    build_assembly_candidate_set,
    collect_flye_artifacts,
    commit_assembly_plan,
    enter_assembly,
    fail_assembly,
    freeze_flye_attempt,
    infer_input_profile,
    plan_candidate_attempts,
    resolve_extended_inputs,
    run_flye_attempt,
    select_primary_and_fallback,
    select_route,
)
from app.stages.assembly.routing import (
    route_after_candidate_set,
    route_after_flye_collect,
    route_after_flye_freeze,
    route_after_flye_run,
    route_after_infer_profile,
    route_after_plan,
    route_after_resolve,
    route_after_select,
    route_after_select_route,
)


def build_stage():
    """Build the assembly candidate-planning subgraph."""
    graph = StateGraph(TopLevelState)

    graph.add_node("enter", enter_assembly)
    graph.add_node("resolve_extended_inputs", resolve_extended_inputs)
    graph.add_node("infer_input_profile", infer_input_profile)
    graph.add_node("select_route", select_route)
    graph.add_node("build_assembly_candidate_set", build_assembly_candidate_set)
    graph.add_node("select_primary_and_fallback", select_primary_and_fallback)
    graph.add_node("plan_candidate_attempts", plan_candidate_attempts)
    graph.add_node("freeze_flye_attempt", freeze_flye_attempt)
    graph.add_node("run_flye_attempt", run_flye_attempt)
    graph.add_node("collect_flye_artifacts", collect_flye_artifacts)
    graph.add_node("commit", commit_assembly_plan)
    graph.add_node("fail", fail_assembly)
    graph.add_node("block", block_assembly_for_human)

    graph.add_edge(START, "enter")
    graph.add_edge("enter", "resolve_extended_inputs")
    graph.add_conditional_edges(
        "resolve_extended_inputs",
        route_after_resolve,
        {"infer_profile": "infer_input_profile", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "infer_input_profile",
        route_after_infer_profile,
        {"select_route": "select_route", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_route",
        route_after_select_route,
        {"build": "build_assembly_candidate_set", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "build_assembly_candidate_set",
        route_after_candidate_set,
        {"select": "select_primary_and_fallback", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "select_primary_and_fallback",
        route_after_select,
        {"plan": "plan_candidate_attempts", "block": "block", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "plan_candidate_attempts",
        route_after_plan,
        {"freeze_flye": "freeze_flye_attempt", "commit": "commit", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "freeze_flye_attempt",
        route_after_flye_freeze,
        {"run_flye": "run_flye_attempt", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "run_flye_attempt",
        route_after_flye_run,
        {"collect_flye": "collect_flye_artifacts", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "collect_flye_artifacts",
        route_after_flye_collect,
        {"commit": "commit", "fail": "fail"},
    )
    graph.add_edge("commit", END)
    graph.add_edge("fail", END)
    graph.add_edge("block", END)

    return graph.compile()
